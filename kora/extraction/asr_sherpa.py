"""Offline speech-to-text and speaker turns with sherpa-onnx (runs in the sandbox, no network).

  * Whisper (multilingual, int8 ONNX) per speech chunk; language detected per chunk, so a
    meeting that switches between Swedish and English is handled.
  * Speaker turns ("who spoke when") with pyannote segmentation + a speaker-embedding model.
    Speakers come out as anonymous labels (Speaker 1, Speaker 2, ...). Voice embeddings live
    only in memory for the clustering and are never stored. Attaching a real name to a label
    is a human decision (review task), never automatic.

Models are fetched once with `kora fetch-models --sherpa` into <model_cache>/sherpa:
    sherpa-onnx-whisper-small/{small-encoder.int8.onnx, small-decoder.int8.onnx, small-tokens.txt}
    sherpa-onnx-pyannote-segmentation-3-0/model.onnx
    nemo_en_titanet_small.onnx
    silero_vad.onnx
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
MAX_CHUNK_S = 28.0          # Whisper's window is 30 s


def model_dir(opts: dict) -> Path | None:
    d = opts.get("sherpa_dir") or os.environ.get("KORA_SHERPA_DIR")
    if not d:
        return None
    p = Path(d)
    return p if (p / "sherpa-onnx-whisper-small" / "small-tokens.txt").is_file() else None


def _load_audio(path: Path) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("audio decode failed")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def _turns(samples: np.ndarray, d: Path, threads: int) -> list[tuple[float, float, int]]:
    import sherpa_onnx
    seg = d / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    emb = d / "nemo_en_titanet_small.onnx"
    if not (seg.is_file() and emb.is_file()):
        return []
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)), num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.8),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    res = sd.process(samples).sort_by_start_time()
    turns = [(float(r.start), float(r.end), int(r.speaker)) for r in res]
    # a "speaker" with only a few seconds of speech is almost always the same voice split off
    # (laughter, a cough, a microphone change): fold it into the speaker before it
    talk: dict[int, float] = {}
    for st, en, sp in turns:
        talk[sp] = talk.get(sp, 0.0) + en - st
    total = sum(talk.values()) or 1.0
    minor = {sp for sp, t in talk.items() if t < max(15.0, 0.04 * total)}
    out, last = [], None
    for st, en, sp in turns:
        if sp in minor and last is not None:
            sp = last
        out.append((st, en, sp))
        last = sp
    return out


def _vad_chunks(samples: np.ndarray, d: Path) -> list[tuple[float, float, int]]:
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(d / "silero_vad.onnx")
    cfg.silero_vad.min_silence_duration = 0.4
    cfg.silero_vad.max_speech_duration = MAX_CHUNK_S
    cfg.sample_rate = SR
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=MAX_CHUNK_S + 10)
    out, win = [], cfg.silero_vad.window_size
    for i in range(0, len(samples), win):
        vad.accept_waveform(samples[i:i + win])
        while not vad.empty():
            st = vad.front.start / SR
            out.append((st, st + len(vad.front.samples) / SR, -1))
            vad.pop()
    vad.flush()
    while not vad.empty():
        st = vad.front.start / SR
        out.append((st, st + len(vad.front.samples) / SR, -1))
        vad.pop()
    return out


def _merge(turns: list[tuple[float, float, int]], gap: float = 0.6) -> list[tuple[float, float, int]]:
    """Join consecutive pieces of the same speaker, then cut into Whisper-sized chunks."""
    merged: list[list] = []
    for s, e, spk in turns:
        if merged and merged[-1][2] == spk and s - merged[-1][1] <= gap and e - merged[-1][0] <= MAX_CHUNK_S:
            merged[-1][1] = e
        else:
            merged.append([s, e, spk])
    out = []
    for s, e, spk in merged:
        while e - s > MAX_CHUNK_S:
            out.append((s, s + MAX_CHUNK_S, spk))
            s += MAX_CHUNK_S
        if e - s >= 0.4:
            out.append((s, e, spk))
    return out


def transcribe(path: Path, opts: dict) -> tuple[list[dict], dict] | None:
    d = model_dir(opts)
    if d is None:
        return None
    import sherpa_onnx
    threads = max(1, min(4, os.cpu_count() or 1))
    from .progress import report
    pdir = Path(opts["progress_dir"]) if opts.get("progress_dir") else None
    samples = _load_audio(path)
    if not len(samples):
        return None
    diarize = opts.get("diarization", "anonymous") != "off"
    report(pdir, "finding who spoke when (speaker turns)" if diarize else "finding speech")
    turns = _turns(samples, d, threads) if diarize else []
    chunks = _merge(turns) if turns else _merge(_vad_chunks(samples, d))
    w = d / "sherpa-onnx-whisper-small"
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(w / "small-encoder.int8.onnx"), decoder=str(w / "small-decoder.int8.onnx"),
        tokens=str(w / "small-tokens.txt"), language="", task="transcribe", num_threads=threads)
    order: dict[int, int] = {}
    segs = []

    def decode(r, s, e):
        st = r.create_stream()
        st.accept_waveform(SR, samples[int(s * SR):int(e * SR)])
        r.decode_stream(st)
        return " ".join((st.result.text or "").split()), (getattr(st.result, "lang", "") or "").strip("<|>") or None
    for i, (s, e, spk) in enumerate(chunks):
        if i % 3 == 0:
            report(pdir, "transcribing speech (Whisper, offline)", i, len(chunks))
        text, lang = decode(rec, s, e)
        if not text or (text[0] in "([" and text[-1] in ")]") or text.strip(".… ") == "":
            continue                                      # "(static)", "[BLANK_AUDIO]", "..."
        label = None
        if spk >= 0:
            order.setdefault(spk, len(order) + 1)
            label = f"Speaker {order[spk]}"
        segs.append({"start": round(s, 3), "end": round(e, 3), "speaker": label, "text": text, "lang": lang})
    # language per chunk is a guess on 30 s of audio; a heavily accented English sentence can come
    # out "translated" into Swedish. Languages under a quarter of the speech are re-decoded in the
    # recording's main language.
    from collections import defaultdict as _dd
    dur = _dd(float)
    for x in segs:
        if x["lang"]:
            dur[x["lang"]] += x["end"] - x["start"]
    if dur:
        total_d = sum(dur.values())
        main_lang = max(dur, key=dur.get)
        keep = {lg for lg, d in dur.items() if d >= 0.25 * total_d}
        redo = [x for x in segs if x["lang"] and x["lang"] not in keep]
        if redo:
            report(pdir, f"re-reading {len(redo)} passages in {main_lang}")
            forced = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(w / "small-encoder.int8.onnx"), decoder=str(w / "small-decoder.int8.onnx"),
                tokens=str(w / "small-tokens.txt"), language=main_lang, task="transcribe", num_threads=threads)
            for x in redo:
                x["text"], _ = decode(forced, x["start"], x["end"])
                x["lang"], x["redecoded"] = main_lang, True
        segs = [x for x in segs if x["text"]]
    from collections import Counter
    langs = Counter(x["lang"] for x in segs if x["lang"])
    info = {"engine": "sherpa-onnx whisper-small int8", "speakers": len(order),
            "diarization": "anonymous" if turns else "off", "languages": dict(langs)}
    return (segs, info) if segs else None
