"""Offline speech recognition fallback (CMU PocketSphinx, model bundled in the
wheel - no network, no audio leaves the deployment).

Used only when a recording has no platform transcript (.vtt/.srt) and Whisper
is not installed. Accuracy is far below Whisper, so the transcript is marked
`asr:pocketsphinx` and KORA requires a human review before such a recording
may be distilled. The audio is split by voice-activity detection and the
segments are decoded in parallel processes.
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

RATE = 16000
CHUNK_S = 300


def _decode_chunk(args: tuple[str, float, float]) -> list[dict]:
    path, offset, length = args
    from pocketsphinx import Decoder, Endpointer
    ep = Endpointer(sample_rate=RATE)
    dec = Decoder(samprate=RATE, loglevel="FATAL")
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
                             "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", path, "-vn", "-ac", "1",
                             "-ar", str(RATE), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    fb = ep.frame_bytes
    segs, start = [], None
    elapsed = 0.0

    def close(end):
        if end is None or end < (start or 0):
            end = min(length, elapsed)                 # stream ended mid-speech: end at what we decoded
        dec.end_utt()
        h = dec.hyp()
        txt = h.hypstr.strip() if h is not None else ""
        if txt:
            segs.append({"start": round(offset + (start or 0), 2), "end": round(offset + end, 2),
                         "speaker": None, "text": txt})
    try:
        while True:
            frame = proc.stdout.read(fb)
            elapsed += len(frame) / 2 / RATE
            prev = ep.in_speech
            if len(frame) < fb:
                speech = ep.end_stream(frame) if frame else None
                if speech is not None:
                    if not prev:
                        dec.start_utt()
                        start = ep.speech_start
                    dec.process_raw(speech)
                    close(ep.speech_end)
                elif prev:
                    close(None)
                break
            speech = ep.process(frame)
            if speech is not None:
                if not prev:
                    dec.start_utt()
                    start = ep.speech_start
                dec.process_raw(speech)
                if not ep.in_speech:
                    close(ep.speech_end)
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)
    return segs


def transcribe(path: Path, duration: float, workers: int | None = None) -> list[dict]:
    try:
        import pocketsphinx  # noqa: F401
    except Exception:
        return []
    chunks = [(str(path), t, min(CHUNK_S, duration - t)) for t in range(0, int(duration) + 1, CHUNK_S) if duration - t > 0.5]
    workers = max(1, min(len(chunks), workers or (os.cpu_count() or 2)))
    if workers == 1:
        parts = [_decode_chunk(c) for c in chunks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(_decode_chunk, chunks))
    return [seg for p in parts for seg in p]
