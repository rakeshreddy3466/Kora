"""WebVTT / SRT parsing with speaker attribution.

Handles Teams/Stream style voice tags (<v Alice Andersson>text</v>),
"Speaker: text" prefixes, cue identifiers, NOTE/STYLE blocks and inline tags.
"""
from __future__ import annotations

import re
from pathlib import Path

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_CUE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")
_VOICE = re.compile(r"<v(?:\.[^ >]+)?\s+([^>]+)>")
_TAGS = re.compile(r"<[^>]+>")
_PREFIX = re.compile(r"^([A-Z][\w.'\-]*(?:\s[A-Z][\w.'\-]*){0,3}):\s+(.+)$")


def _ts(s: str) -> float:
    m = _TIME.match(s)
    if not m:
        raise ValueError(f"bad timestamp {s!r}")
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000


def parse(text: str) -> list[dict]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    segs: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        m = _CUE.match(lines[idx])
        if not m:
            continue
        try:
            start, end = _ts(m.group(1)), _ts(m.group(2))
        except ValueError:
            continue
        body = " ".join(lines[idx + 1:]).strip()
        speaker = None
        vm = _VOICE.search(body)
        if vm:
            speaker = vm.group(1).strip()
        clean = _TAGS.sub("", body).strip()
        if speaker is None:
            pm = _PREFIX.match(clean)
            if pm:
                speaker, clean = pm.group(1), pm.group(2)
        if clean and end >= start:
            segs.append({"start": round(start, 3), "end": round(end, 3), "speaker": speaker, "text": clean})
    segs.sort(key=lambda s: s["start"])
    return segs


def parse_file(path: Path, max_bytes: int = 20_000_000) -> list[dict]:
    data = path.read_bytes()[:max_bytes]
    return parse(data.decode("utf-8", errors="replace"))


def _fmt(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def to_vtt(segments) -> str:
    """(start, end, speaker, text) tuples -> WebVTT that parse() reads back losslessly."""
    out = ["WEBVTT", ""]
    for start, end, speaker, text in segments:
        body = " ".join(str(text).split())
        if not body:
            continue
        out.append(f"{_fmt(start)} --> {_fmt(max(end, start))}")
        out.append(f"<v {speaker}>{body}" if speaker else body)
        out.append("")
    return "\n".join(out)
