"""Filename normalisation: separate the *document identity* part of a name from
copy markers and version tokens.

  "Network_Architecture_A (1).pdf"  -> stem "network architecture", copy=1, tokens ["A"]
  "Copy of Team Update - Copy.pdf"  -> stem "team update", copy=True
  "Team_Update_Final_v2.pdf"        -> stem "team update", tokens ["final", "v2"]
  "Onboarding_v2.pptx"              -> stem "onboarding", tokens ["v2"]
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePath

_DOWNLOAD = re.compile(r"\s*\((\d{1,3})\)$")                        # " (1)" browser download suffix
_COPY = [re.compile(p, re.I) for p in (r"^copy of\s+", r"\s*-\s*copy(\s*\(\d+\))?$", r"[_\s]copy\d*$",
                                       r"\s*-\s*kopia$", r"_conflict(ed)?[_\- ]?copy.*$")]
_SEP = re.compile(r"[_\-\s.]+")
# trailing version-ish tokens, peeled from the right
_TOKEN = re.compile(
    r"(?:^|[_\-\s])("
    r"v\d+(?:\.\d+)*|ver\d+|rev[_\-\s]?[a-z]{0,2}\d*|r\d+[a-z]\d*|p[a-z]\d{1,2}|[a-hj-np-z]"
    r"|final\d*|draft\d*|wip|approved|signed|latest|new|old|\d{3,4}p|hd|4k"
    r"|\d{4}[-_]?\d{2}[-_]?\d{2}"
    r")$", re.I)
_NOISE = {"latest", "new", "old", "hd", "4k"}


@dataclass
class NameInfo:
    original: str
    stem_key: str
    copy_marker: int = 0
    tokens: list[str] = field(default_factory=list)
    date_token: str | None = None


def parse(filename: str) -> NameInfo:
    name = PurePath(filename).name
    stem = PurePath(name).stem if "." in name else name
    stem = unicodedata.normalize("NFKC", stem).strip()
    copy = 0
    m = _DOWNLOAD.search(stem)
    if m:
        copy = int(m.group(1))
        stem = stem[:m.start()]
    for rx in _COPY:
        if rx.search(stem):
            copy = copy or 1
            stem = rx.sub("", stem)
    tokens: list[str] = []
    date = None
    for _ in range(4):
        m = _TOKEN.search(stem)
        if not m or m.start(1) == 0:
            break
        tok = m.group(1)
        stem = stem[:m.start()].rstrip("_- .")
        low = tok.lower()
        if re.fullmatch(r"\d{4}[-_]?\d{2}[-_]?\d{2}", tok):
            date = tok
            continue
        if low.startswith("rev"):
            tok = re.sub(r"^rev[_\-\s]?", "", tok, flags=re.I) or tok
        if low in _NOISE or re.fullmatch(r"\d{3,4}p", low):
            continue
        tokens.insert(0, tok)
    key = _SEP.sub(" ", stem.lower()).strip()
    return NameInfo(name, key or name.lower(), copy, tokens, date)
