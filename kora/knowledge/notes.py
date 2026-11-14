"""Speaker notes in slide decks: who answers for a slide, and until when it is valid.

Real decks carry a line such as "Slide responsible: <name>   Best Before: April 2020" in the
speaker notes. KORA treats that line as three things:

* personal data: the name identifies an employee. Notes become their own units (kind "note"),
  so they go through the same privacy review, AI redaction, access export and erasure as
  transcripts and chat (GDPR Art. 5(1)(c), 15, 17). The raw notes are moved out of the slide's
  metadata, so there is exactly one copy to export or erase.
* ownership: the named person answers for the slide, so a leaver assessment lists the slides
  that need a new owner.
* freshness: a slide past its Best Before date is flagged in search, in AI context packs and on
  the Files page, and offered to a person for retirement. Nothing is deleted automatically.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Content, Posting, Unit
from ..fingerprint.text import text_hash

VERSION = 1

_PERSON_LABEL = re.compile(
    r"(?i)(?<!\w)(?:slide[ \t]+)?(?:responsible|owner|author|presenter|contact[ \t]+person|contact|"
    r"ansvarig|kontaktperson|presentatör)[ \t]*:[ \t]*")
_DATE_LABEL = re.compile(
    r"(?i)(?<!\w)(?:best[ \t\-]*before|valid[ \t]+(?:until|through|to)|expiry(?:[ \t]+date)?|expires(?:[ \t]+on)?|"
    r"review[ \t]+(?:by|date)|bäst[ \t]+före|giltig[ \t]+till)[ \t]*:?[ \t]*")
_ANY_LABEL = re.compile(_PERSON_LABEL.pattern[4:] + "|" + _DATE_LABEL.pattern[4:], re.IGNORECASE)
_PARTICLE = r"(?:van|von|de|da|der|den|del|af|al|el|la|le|di|du|dos|das|bin|ibn)"
_NAME = re.compile(rf"[A-ZÀ-ÖØ-Þ][\w'’\-]*\.?(?:[ ]+(?:{_PARTICLE}[ ]+)*[A-ZÀ-ÖØ-Þ][\w'’\-]*\.?){{0,3}}")
_NOT_A_NAME = {"tbd", "tba", "n/a", "na", "none", "unknown", "me", "you", "team", "all", "the", "same"}

_MONTHS = {}
for _i, _names in enumerate([
        ("jan", "january", "januari"), ("feb", "february", "februari"), ("mar", "march", "mars"),
        ("apr", "april"), ("may", "maj"), ("jun", "june", "juni"), ("jul", "july", "juli"),
        ("aug", "august", "augusti"), ("sep", "sept", "september"), ("oct", "october", "okt", "oktober"),
        ("nov", "november"), ("dec", "december")], start=1):
    for _n in _names:
        _MONTHS[_n] = _i
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))


def _end_of_month(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_date(text: str) -> date | None:
    """The last day a dated label still holds: 'April 2020' -> 2020-04-30, 'Q2 2021' -> 2021-06-30."""
    t = (text or "").strip().lower()
    try:
        if m := re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", t):             # day first (European)
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := re.match(rf"(\d{{1,2}})(?:st|nd|rd|th)?[ \t]+({_MON})\.?,?[ \t]+(\d{{4}})\b", t):
            return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
        if m := re.match(rf"({_MON})\.?[ \t]+(\d{{1,2}})(?:st|nd|rd|th)?,?[ \t]+(\d{{4}})\b", t):
            return date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        if m := re.match(rf"({_MON})\.?,?[ \t]+(\d{{4}})\b", t):
            return _end_of_month(int(m[2]), _MONTHS[m[1]])
        if m := re.match(r"(\d{4})[-/.](\d{1,2})\b", t):
            return _end_of_month(int(m[1]), int(m[2]))
        if m := re.match(r"(\d{1,2})[-/.](\d{4})\b", t):
            return _end_of_month(int(m[2]), int(m[1]))
        if m := re.match(r"q([1-4])[ \t\-]*(\d{4})\b", t) or re.match(r"(\d{4})[ \t\-]*q([1-4])\b", t):
            q, y = (int(m[1]), int(m[2])) if t.startswith("q") else (int(m[2]), int(m[1]))
            return _end_of_month(y, q * 3)
        if m := re.match(r"(\d{4})\b", t):
            return date(int(m[1]), 12, 31)
    except ValueError:
        return None
    return None


@dataclass
class NoteInfo:
    people: list[str] = field(default_factory=list)
    best_before: date | None = None
    best_before_text: str | None = None


def _value_after(text: str, end: int) -> str:
    """The value of a label: up to the end of the line, a tab, or the next label."""
    rest = text[end:]
    cut = len(rest)
    for stop in ("\n", "\t", "\r"):
        i = rest.find(stop)
        if i >= 0:
            cut = min(cut, i)
    nxt = _ANY_LABEL.search(rest[:cut])
    if nxt and nxt.start() > 0:
        cut = nxt.start()
    return rest[:cut].strip(" \t,;|-–")


def parse(notes: str) -> NoteInfo:
    info = NoteInfo()
    text = notes or ""
    for m in _PERSON_LABEL.finditer(text):
        value = _value_after(text, m.end())
        for piece in re.split(r"\s*(?:,|;|/|&|\band\b|\boch\b)\s*", value):
            nm = _NAME.match(piece.strip())
            if not nm:
                continue
            name = nm.group(0).strip().rstrip(".")
            if name.lower() in _NOT_A_NAME or len(name) < 3 or name in info.people:
                continue
            info.people.append(name)
    for m in _DATE_LABEL.finditer(text):
        value = _value_after(text, m.end())
        d = parse_date(value)
        if d and (info.best_before is None or d < info.best_before):
            info.best_before, info.best_before_text = d, value[:40]
    return info


def apply(s: Session, sha: str) -> dict:
    """(Re)build the note units of one deck from its slides; idempotent."""
    c = s.get(Content, sha)
    if c is None or c.detected_type != "pptx":
        return {}
    previous = {u.ordinal: u.text for u in s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "note"))}
    old = select(Unit.id).where(Unit.content_sha == sha, Unit.kind == "note")
    s.execute(delete(Posting).where(Posting.unit_id.in_(old)))
    s.execute(delete(Unit).where(Unit.content_sha == sha, Unit.kind == "note"))
    slides = list(s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "slide").order_by(Unit.ordinal)))
    n_notes, people, earliest, earliest_text = 0, set(), None, None
    dates: dict[str, int] = {}
    for u in slides:
        meta = dict(u.meta or {})
        notes = meta.pop("notes", None)
        if notes is None:                                   # already moved: keep the note unit's text (it may be
            notes = previous.get(u.ordinal)                 # pseudonymised after an erasure)
        if not (notes or "").strip():
            u.meta = {**meta, "has_notes": False}
            continue
        info = parse(notes)
        n_notes += 1
        people |= set(info.people)
        bb = info.best_before.isoformat() if info.best_before else None
        if bb:
            dates[bb] = dates.get(bb, 0) + 1
        if info.best_before and (earliest is None or info.best_before < earliest):
            earliest, earliest_text = info.best_before, info.best_before_text
        s.add(Unit(content_sha=sha, kind="note", ordinal=u.ordinal, text=notes[:5000], text_hash=text_hash(notes),
                   meta={"slide": u.ordinal, "people": info.people, "best_before": bb,
                         "best_before_text": info.best_before_text, "hidden_slide": bool(meta.get("hidden"))}))
        # one copy only: the notes now live in the note unit (exported/erased with it)
        u.meta = {**meta, "has_notes": True, "best_before": bb}
    summary = {"v": VERSION, "slides": len(slides), "slides_with_notes": n_notes, "people_named": len(people),
               "best_before": earliest.isoformat() if earliest else None, "best_before_text": earliest_text,
               "slide_dates": dates}
    c.meta = {**(c.meta or {}), "speaker_notes": summary}
    s.flush()
    return summary


def is_past(best_before: str | None, today: date | None = None) -> bool:
    if not best_before:
        return False
    try:
        return date.fromisoformat(best_before) < (today or date.today())
    except ValueError:
        return False


def freshness(c: Content | None) -> dict | None:
    """What a reader should know about a deck's validity, or None if it carries no date."""
    sn = ((c.meta or {}) if c else {}).get("speaker_notes") or {}
    if not sn.get("best_before"):
        return None
    past = sum(n for d, n in (sn.get("slide_dates") or {}).items() if is_past(d))
    return {"best_before": sn["best_before"], "label": sn.get("best_before_text"),
            "past": is_past(sn["best_before"]), "slides_past": past, "slides_dated": sum((sn.get("slide_dates") or {}).values()),
            "slides": sn.get("slides", 0)}


def people_named(s: Session) -> list[str]:
    """Everyone named as responsible in any speaker notes (for redaction and privacy review)."""
    out = set()
    for meta in s.scalars(select(Unit.meta).where(Unit.kind == "note")):
        out |= set((meta or {}).get("people") or [])
    return sorted(out)


def slides_of(s: Session, names: list[str]) -> list[dict]:
    """Slides whose notes name one of these people as responsible (leaver hand-over)."""
    from ..privacy.dsar import name_pattern
    pats = [name_pattern(n) for n in names if n]
    out: dict[str, dict] = {}
    for u in s.scalars(select(Unit).where(Unit.kind == "note")):
        if any(pt.fullmatch(p) for p in (u.meta or {}).get("people") or [] for pt in pats):
            out.setdefault(u.content_sha, {"content": u.content_sha, "slides": []})["slides"].append(u.ordinal)
    return list(out.values())


def backfill(s: Session) -> int:
    """Decks extracted before notes were handled: give them note units once, index them and open the
    privacy and freshness reviews they imply."""
    from .. import hitl
    from ..search.index import index_content
    todo = [c.sha256 for c in s.scalars(select(Content).where(Content.detected_type == "pptx",
                                                              Content.extracted.is_(True)))
            if ((c.meta or {}).get("speaker_notes") or {}).get("v") != VERSION]
    for sha in todo:
        apply(s, sha)
        index_content(s, sha)
        hitl.generate(s, sha)
    return len(todo)
