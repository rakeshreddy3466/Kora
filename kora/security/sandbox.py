"""Run untrusted-file parsers in a constrained child process.

The child gets: a scrubbed environment (no DB URL, no keys, no cloud creds),
its own session/process group, rlimits on address space, CPU time, file size
and open files, and a wall-clock timeout that kills the whole group (incl.
ffmpeg grandchildren). A parser crash or exploit in MuPDF/FFmpeg therefore
cannot reach KORA's database or secrets, and cannot hang the worker.
"""
from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path

from ..config import get_settings

PKG_ROOT = str(Path(__file__).resolve().parents[2])


class SandboxError(Exception):
    pass


MEDIA_TASKS = {"video", "optimize_video"}


def limits_for(task: str, st) -> tuple[int, int]:
    """(wall-clock seconds, CPU seconds) for one sandboxed task.

    Recordings run speech models on several threads, so they get a longer wall time; CPU time is
    allowed for every core, because the CPU limit exists to stop a runaway or an orphan, not a busy model."""
    wall = max(st.sandbox_timeout_s, st.media_timeout_s) if task in MEDIA_TASKS else st.sandbox_timeout_s
    return wall, wall * max(1, os.cpu_count() or 1)


def _limits(mem_mb: int, cpu_s: int):
    def apply():  # runs in the child between fork and exec
        limits = [(resource.RLIMIT_AS, mem_mb * 1024 * 1024), (resource.RLIMIT_CPU, cpu_s),
                  (resource.RLIMIT_FSIZE, 8 * 1024 ** 3), (resource.RLIMIT_NOFILE, 256), (resource.RLIMIT_CORE, 0)]
        for res, val in limits:
            try:                                  # macOS refuses some limits (e.g. RLIMIT_AS); keep the rest
                soft, hard = resource.getrlimit(res)
                cap = val if hard == resource.RLIM_INFINITY else min(val, hard)
                resource.setrlimit(res, (cap, hard if hard != resource.RLIM_INFINITY else cap))
            except (ValueError, OSError):
                pass
        try:                                      # Linux: die with the worker instead of running on as an orphan
            import ctypes
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)   # PR_SET_PDEATHSIG
        except Exception:                         # elsewhere the CPU rlimit still bounds an orphan
            pass
    return apply


def run(task: str, args: dict, out_dir: Path) -> dict:
    """Execute kora.extraction.entry:<task> on args; artifacts are written to out_dir."""
    st = get_settings()
    request = {"task": task, "args": args, "out_dir": str(out_dir),
               "options": {"max_pages": st.max_pages, "ocr": st.ocr, "pptx_renderer": st.pptx_renderer,
                           "capsule_audio": st.capsule_audio, "asr": st.asr,
                           "whisper_model": st.whisper_model, "diarization": st.diarization,
                           "sherpa_dir": str(Path(st.model_cache_dir or Path(st.data_dir) / "models") / "sherpa")}}
    if st.sandbox == "inline":
        from ..extraction import entry
        from ..jobs import PermanentError
        from .filetype import UnsafeFile
        try:
            return entry.dispatch(request)
        except (entry.PermanentInput, UnsafeFile) as e:
            raise PermanentError(f"{task}: {e}") from e

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(out_dir), "LANG": "C.UTF-8",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "TMPDIR": str(out_dir)}
    # pre-downloaded models only (`kora fetch-models`): the sandbox never fetches anything
    env.update({"HF_HOME": st.model_cache_dir or str(Path(st.data_dir) / "models"), "HF_HUB_OFFLINE": "1"})
    # the protocol channel is a private dup of stdout; fd 1 itself is pointed at stderr so that native
    # libraries (MuPDF, ffmpeg bindings) printing warnings can never corrupt the JSON reply
    code = ("import os,sys,json; sys.path.insert(0, %r); proto=os.fdopen(os.dup(1),'w'); os.dup2(2,1); "
            "from kora.extraction import entry; "
            "req=json.load(sys.stdin); res=entry.dispatch(req); proto.write(json.dumps(res)); proto.flush()" % PKG_ROOT)
    wall, cpu = limits_for(task, st)
    proc = subprocess.Popen([sys.executable, "-s", "-E", "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(out_dir),
                            preexec_fn=_limits(st.sandbox_mem_mb, cpu),
                            start_new_session=True)
    try:
        out, err = proc.communicate(json.dumps(request).encode(), timeout=wall)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise SandboxError(f"{task}: timed out after {wall}s")
    if proc.returncode != 0:
        tail = err.decode(errors="replace")[-2000:]
        if "PermanentInput" in tail or "UnsafeFile" in tail:
            from ..jobs import PermanentError
            raise PermanentError(f"{task}: {tail.strip().splitlines()[-1] if tail.strip() else 'failed'}")
        raise SandboxError(f"{task}: exit {proc.returncode}: {tail}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise SandboxError(f"{task}: invalid output") from e
