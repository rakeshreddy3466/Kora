"""Recreate a watchable video from a capsule: every keyframe held for exactly
the time it was on screen, the retained Opus speech track underneath, and the
transcript as soft subtitles. Not the original pixels (webcam motion, cursor
and animations are gone) - a "slides + voice" rendition that preserves what
was said and what was shown, at a fraction of the size."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Capsule
from ..storage import get_blobstore


def _ts(t: float) -> str:
    h, r = divmod(max(0.0, t), 3600)
    m, sec = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".replace(".", ",")


def rebuild_video(s: Session, sha: str, out: Path) -> dict:
    cap = s.get(Capsule, sha)
    if cap is None:
        raise ValueError("no capsule for content")
    store = get_blobstore()
    arts = cap.manifest["artifacts"]
    frames = sorted((a for a in arts if a["role"] == "keyframe"), key=lambda a: a["t"])
    if not frames:
        raise ValueError("capsule has no keyframes")
    audio = next((a for a in arts if a["role"] == "audio"), None)
    transcript = next((a for a in arts if a["role"] == "transcript"), None)
    duration = cap.manifest["source"].get("duration_s") or frames[-1]["until"]
    with tempfile.TemporaryDirectory(prefix="kora-rebuild-") as td:
        tdp = Path(td)
        lines = []
        for i, f in enumerate(frames):
            name = f"f{i:05d}.jpg"
            store.write_to("hot", f["blob"], tdp / name)
            start = 0.0 if i == 0 else f["t"]
            end = frames[i + 1]["t"] if i + 1 < len(frames) else duration
            lines.append(f"file '{name}'\nduration {max(0.04, end - start):.3f}")
        lines.append(f"file 'f{len(frames) - 1:05d}.jpg'")
        (tdp / "list.txt").write_text("\n".join(lines) + "\n")
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file", "-y",
                "-f", "concat", "-safe", "1", "-i", "list.txt"]
        maps = ["-map", "0:v"]
        if audio:
            store.write_to("hot", audio["blob"], tdp / "audio.opus")
            args += ["-i", "audio.opus"]
            maps += ["-map", "1:a"]
        if transcript:
            import json
            segs = json.loads(store.get_bytes("hot", transcript["blob"]))["segments"]
            srt = []
            for n, sg in enumerate(segs, 1):
                who = f"{sg['speaker']}: " if sg.get("speaker") else ""
                srt += [str(n), f"{_ts(sg['start'])} --> {_ts(sg['end'])}", who + sg["text"], ""]
            (tdp / "subs.srt").write_text("\n".join(srt))
            args += ["-i", "subs.srt"]
            maps += ["-map", f"{2 if audio else 1}:s"]
        args += [*maps, "-vf", "fps=2,scale='min(1280,iw)':-2,format=yuv420p", "-c:v", "libx264",
                 "-preset", "veryfast", "-tune", "stillimage", "-crf", "30"]
        if audio:
            args += ["-c:a", "copy"]
        if transcript:
            args += ["-c:s", "mov_text"]
        args += ["-t", f"{duration:.3f}", "rebuilt.mp4"]
        r = subprocess.run(args, cwd=td, capture_output=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[-500:])
        out.write_bytes((tdp / "rebuilt.mp4").read_bytes())
    return {"keyframes": len(frames), "audio": bool(audio), "subtitles": bool(transcript),
            "duration_s": duration, "bytes": out.stat().st_size,
            "original_bytes": cap.manifest["source"]["size_bytes"]}
