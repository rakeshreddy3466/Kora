"""Personal-data detection and redaction (GDPR data minimisation, Art. 5(1)(c)).

Deterministic detectors, run locally (no text leaves the deployment):
  EMAIL, PHONE (Swedish and international), PNR (Swedish personnummer /
  samordningsnummer, Luhn-validated so random digit runs are not flagged),
  IBAN, IPV4, and NAME for known people (directory names + transcript speakers).
Used to (a) flag content for human privacy review, (b) redact text that leaves
KORA for AI use, (c) support erasure of a person from derived data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w/])(?:\+46|0046|\+\d{2,3})[\s\-]?\(?\d{1,3}\)?(?:[\s\-]?\d{2,4}){2,4}(?!\w)"
                    r"|(?<![\w/])07[02369][\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\w)")
_PNR = re.compile(r"(?<![\w\-:/])(?:19|20)?(\d{2})(\d{2})(\d{2})[\-+]?(\d{4})(?![\w\-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    value: str

    @property
    def masked(self) -> str:
        v = self.value
        return v[:2] + "…" + v[-2:] if len(v) > 6 else "…"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _pnr_valid(m: re.Match) -> bool:
    yy, mm, dd, tail = m.groups()
    month, day = int(mm), int(dd)
    if not (1 <= month <= 12):
        return False
    if not (1 <= day <= 31 or 61 <= day <= 91):           # 61-91: samordningsnummer
        return False
    return _luhn_ok(yy + mm + dd + tail)


def find(text: str, names: list[str] | None = None) -> list[Finding]:
    text = text or ""
    out: list[Finding] = []
    for m in _EMAIL.finditer(text):
        out.append(Finding("EMAIL", m.start(), m.end(), m.group(0)))
    for m in _PNR.finditer(text):
        if _pnr_valid(m):
            out.append(Finding("PNR", m.start(), m.end(), m.group(0)))
    for m in _PHONE.finditer(text):
        out.append(Finding("PHONE", m.start(), m.end(), m.group(0)))
    for m in _IBAN.finditer(text):
        out.append(Finding("IBAN", m.start(), m.end(), m.group(0)))
    for m in _IPV4.finditer(text):
        out.append(Finding("IPV4", m.start(), m.end(), m.group(0)))
    for name in sorted({n for n in (names or []) if n and len(n) >= 3}, key=len, reverse=True):
        for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text):
            out.append(Finding("NAME", m.start(), m.end(), m.group(0)))
    # drop overlaps, keep the longest / earliest
    out.sort(key=lambda f: (f.start, -(f.end - f.start)))
    kept, last_end = [], -1
    for f in out:
        if f.start >= last_end:
            kept.append(f)
            last_end = f.end
    return kept


def redact(text: str, names: list[str] | None = None, replacement: dict[str, str] | None = None) -> str:
    fs = find(text, names)
    if not fs:
        return text
    parts, pos = [], 0
    for f in fs:
        parts.append(text[pos:f.start])
        parts.append((replacement or {}).get(f.kind, f"[{f.kind}]"))
        pos = f.end
    parts.append(text[pos:])
    return "".join(parts)
