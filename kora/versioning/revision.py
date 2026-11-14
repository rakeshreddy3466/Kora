"""Revision labels and their ordering.

Schemes (only comparable within the same scheme):
  ericsson   document revisions: PA1 < PA2 < A < PB1 < B < ... < Z < AA
             P<letter><n> is a *preliminary* draft towards released <letter>
  rstate     product R-states: P1A < R1A < R1B < R2A (P = preliminary)
  numeric    v1 < v1.1 < v2 < 2.0.1
  filename   draft < (none) < final, with an optional numeric suffix:
             Team_Update < Team_Update_Final < Team_Update_Final_v2
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ERIC = re.compile(r"^(P)?([A-HJ-NP-Z]{1,2})(\d{1,3})?$")      # no I/O (avoid 1/0 confusion)
_RSTATE = re.compile(r"^(R|P)(\d{1,2})([A-Z]{1,2})(\d{0,2})$")
_NUM = re.compile(r"^v?(\d+(?:\.\d+){0,3})$", re.I)


def _letters(s: str) -> int:
    v = 0
    for ch in s:
        v = v * 26 + (ord(ch) - 64)
    return v


@dataclass(frozen=True)
class Revision:
    label: str
    scheme: str
    key: tuple
    released: bool | None

    def comparable(self, other: "Revision") -> bool:
        return self.scheme == other.scheme

    def __lt__(self, other: "Revision") -> bool:
        if not self.comparable(other):
            raise TypeError(f"cannot compare {self.scheme} with {other.scheme}")
        return self.key < other.key


def parse(label: str | None) -> Revision | None:
    if not label:
        return None
    raw = label.strip().upper().replace(" ", "")
    raw = re.sub(r"^(REV(ISION)?\.?|R\.)", "", raw) if not _RSTATE.match(raw) else raw
    if not raw:
        return None
    m = _RSTATE.match(raw)
    if m:
        pre, major, letter, minor = m.groups()
        released = pre == "R"
        return Revision(raw, "rstate", (int(major), _letters(letter), 1 if released else 0, int(minor or 0)), released)
    m = _ERIC.match(raw)
    if m:
        pre, letters, num = m.groups()
        if pre and num:                          # PA1: preliminary towards A
            return Revision(raw, "ericsson", (_letters(letters), 0, int(num)), False)
        if not pre and not num:                  # A: released
            return Revision(raw, "ericsson", (_letters(letters), 1, 0), True)
        if pre and not num and len(letters) == 1:   # "PA" alone: treat as PA0
            return Revision(raw, "ericsson", (_letters(letters), 0, 0), False)
    m = _NUM.match(raw)
    if m:
        parts = tuple(int(x) for x in m.group(1).split("."))
        parts = parts + (0,) * (4 - len(parts))
        return Revision(raw.lower(), "numeric", parts, None)
    return None


def from_filename_tokens(tokens: list[str]) -> Revision | None:
    """Build a weak ordering key from filename version tokens (see naming.py)."""
    if not tokens:
        return Revision("", "filename", (1, 0), None)
    finality, num = 1, 0
    label = "_".join(tokens)
    for t in tokens:
        t = t.lower()
        if t in ("draft", "wip"):
            finality = 0
        elif t in ("final", "approved", "signed"):
            finality = 2
        elif t.startswith("final") and t[5:].isdigit():
            finality, num = 2, int(t[5:])
        else:
            r = parse(t)
            if r and r.scheme == "numeric":
                num = r.key[0] * 1000 + r.key[1]
            elif r and r.scheme == "ericsson":
                return r
    return Revision(label, "filename", (finality, num), None)
