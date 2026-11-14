"""Recording -> keyframes, transcript, compact audio and a temporal fingerprint.

Every FFmpeg call: argument list (no shell), -nostdin, and
-protocol_whitelist file so a crafted container/playlist cannot make FFmpeg
fetch URLs (SSRF) or read other local files via concat/hls tricks.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from ..fingerprint.phash import hamming, phash
from . import transcript
from .entry import PermanentInput

FF = ["-nostdin", "-hide_banner", "-protocol_whitelist", "file"]
CHANGE_THRESHOLD = 1.2  # mean abs grey-level difference (0-255) that counts as "the screen changed"
MAX_GAP_S = 120          # force a keyframe at least this often
SIGNATURE_POINTS = 10


def _run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def probe(path: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
              "-show_format", "-show_streams", str(path)], timeout=120)
    if r.returncode != 0:
        raise PermanentInput(f"ffprobe failed: {r.stderr.decode(errors='replace')[-300:]}")
    info = json.loads(r.stdout or b"{}")
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise PermanentInput("no video stream")
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise PermanentInput("unknown duration")
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    return {"duration": dur, "width": int(v.get("width", 0)), "height": int(v.get("height", 0)),
            "vcodec": v.get("codec_name"), "fps": round(float(num) / float(den or 1), 3) if float(den or 1) else None,
            "bit_rate": int(fmt.get("bit_rate") or 0), "has_audio": a is not None,
            "acodec": a.get("codec_name") if a else None,
            "audio_bit_rate": int(a.get("bit_rate") or 0) if a else 0,
            "creation_time": (fmt.get("tags") or {}).get("creation_time"),
            "title": (fmt.get("tags") or {}).get("title")}


def _change_points(path: Path, duration: float) -> list[int]:
    """Pass 1: decode at 1 fps, 160x90 grey; pick seconds where the picture changed and then settled.

    Slides of one template differ only in text, which FFmpeg's scene score barely sees;
    a mean-absolute-difference on a small grey raster does."""
    import numpy as np
    w, h = 160, 90
    proc = subprocess.Popen(["ffmpeg", *FF, "-i", str(path), "-an", "-vf", f"fps=1,scale={w}:{h},format=gray",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    picks, last_kept, prev, pending = [], None, None, None
    n = 0
    try:
        while True:
            buf = proc.stdout.read(w * h)
            if len(buf) < w * h:
                break
            cur = np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
            if last_kept is None:
                picks.append(n)
                last_kept = cur
            else:
                changed = float(np.mean(np.abs(cur - last_kept))) > CHANGE_THRESHOLD
                stable = prev is not None and float(np.mean(np.abs(cur - prev))) < CHANGE_THRESHOLD / 2
                if changed and stable:
                    picks.append(n - 1)           # first sample that already showed the new picture
                    last_kept = cur
                elif n - picks[-1] >= MAX_GAP_S:
                    picks.append(n)
                    last_kept = cur
            prev = cur
            n += 1
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)
    return picks


def keyframes(path: Path, out: Path, duration: float) -> list[dict]:
    kdir = out / "kf"
    kdir.mkdir(exist_ok=True)
    picks = _change_points(path, duration)
    files: list[tuple[Path, float]] = []
    for chunk_start in range(0, len(picks), 200):           # keep filter expressions bounded
        chunk = picks[chunk_start:chunk_start + 200]
        expr = "+".join(f"eq(n\\,{i})" for i in chunk)
        pattern = kdir / f"c{chunk_start:05d}-%04d.jpg"
        r = _run(["ffmpeg", *FF, "-i", str(path), "-an", "-vf", f"fps=1,select='{expr}',scale='min(1280,iw)':-2",
                  "-fps_mode", "vfr", "-q:v", "4", str(pattern)])
        if r.returncode != 0:
            raise PermanentInput(f"keyframe extraction failed: {r.stderr.decode(errors='replace')[-300:]}")
        got = sorted(kdir.glob(f"c{chunk_start:05d}-*.jpg"))
        files += list(zip(got, [float(i) for i in chunk]))
    frames, last = [], None
    for f, t in files:
        with Image.open(f) as im:
            ph = phash(im)
        if last is not None and hamming(ph, last) == 0 and frames and t - frames[-1]["t"] < MAX_GAP_S:
            f.unlink()
            continue
        last = ph
        frames.append({"t": round(t, 3), "file": str(f.relative_to(out)), "phash": ph})
    for i, fr in enumerate(frames):
        fr["ordinal"] = i + 1
        fr["end"] = frames[i + 1]["t"] if i + 1 < len(frames) else round(duration, 3)
    return frames


def temporal_signature(path: Path, out: Path, duration: float) -> list[str]:
    sig = []
    for k in range(SIGNATURE_POINTS):
        t = duration * (k + 0.5) / SIGNATURE_POINTS
        dst = out / f"sig-{k}.jpg"
        r = _run(["ffmpeg", *FF, "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an",
                  "-vf", "scale=320:-2", "-q:v", "5", "-y", str(dst)], timeout=120)
        if r.returncode == 0 and dst.exists():
            with Image.open(dst) as im:
                sig.append(phash(im))
            dst.unlink()
    return sig


def extract_audio(path: Path, out: Path) -> str | None:
    dst = out / "audio.opus"
    r = _run(["ffmpeg", *FF, "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
              "-b:a", "16k", "-application", "voip", "-y", str(dst)])
    return dst.name if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0 else None


def ocr(img: Path) -> str:
    try:
        r = subprocess.run(["tesseract", str(img), "stdout", "--psm", "3", "-l", "eng"],
                           capture_output=True, timeout=60)
        return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _sherpa(path: Path, opts: dict):
    try:
        from . import asr_sherpa
        return asr_sherpa.transcribe(path, opts)
    except Exception:                                   # missing models or library: fall through
        return None


def whisper_transcribe(audio: Path, model_name: str = "small") -> list[dict] | None:
    try:
        from faster_whisper import WhisperModel  # optional
    except Exception:
        return None
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception:                                   # model not downloaded (sandbox is offline)
        return None
    segments, _info = model.transcribe(str(audio), vad_filter=True)
    return [{"start": round(s.start, 3), "end": round(s.end, 3), "speaker": None, "text": s.text.strip()}
            for s in segments if s.text.strip()]


def extract(path: Path, out: Path, opts: dict, sidecars: list[Path]) -> dict:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not installed")
    from .progress import report
    report(out, "finding slide changes")
    info = probe(path)
    frames = keyframes(path, out, info["duration"])
    if opts.get("ocr", "auto") == "auto" and shutil.which("tesseract"):
        for i, fr in enumerate(frames):
            report(out, "reading text on slide pictures (OCR)", i, len(frames))
            fr["ocr"] = ocr(out / fr["file"])
    report(out, "extracting the speech track")
    audio = extract_audio(path, out) if info["has_audio"] and opts.get("capsule_audio", True) else None
    opts = {**opts, "progress_dir": str(out)}
    segments, source = [], None
    for sc in sidecars:
        try:
            segments = transcript.parse_file(sc)
        except Exception:
            segments = []
        if segments:
            source = f"sidecar:{sc.suffix.lstrip('.')}"
            break
    asr_info = None
    if not segments and audio and opts.get("asr", "auto") != "off":
        w = whisper_transcribe(out / audio, opts.get("whisper_model", "small"))
        sh = None if w else _sherpa(path, opts)
        if w:
            segments, source = w, "asr:whisper"
        elif sh:
            (segments, asr_info), source = sh, "asr:whisper-onnx"
        else:
            from .asr import transcribe
            w = transcribe(path, info["duration"])
            if w:
                segments, source = w, "asr:pocketsphinx"
    return {"type": "video", "probe": info, "frames": frames, "audio": audio,
            "segments": segments, "transcript_source": source, "asr_info": asr_info,
            "temporal_signature": (report(out, "computing the video fingerprint"),
                                   temporal_signature(path, out, info["duration"]))[1]}
