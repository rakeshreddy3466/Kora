"""Deterministic fact extraction with provenance (no LLM required).

Decisions and action items are pattern-matched; people are matched against
the directory. Every fact points to the exact unit (page/slide or transcript
segment with timestamp and speaker) it came from. An LLM extractor can be
plugged in later, but must emit the same provenance fields.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Principal, Unit
from ..versioning.docnumber import find_docno

_DECISION = re.compile(r"\b(decision\s*:|we (?:have )?decided|we adopt|agreed to|it was agreed)\b", re.I)
_ACTION = re.compile(r"\b(action(?: item)?\s*:|action item|todo\s*:|to do\s*:|will (?:\w+ ){0,4}by\b|owner\s*:)", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def extract(s: Session, sha: str) -> list[dict]:
    people = {p.display_name.split(" (")[0]: p.id for p in s.scalars(select(Principal))}
    first_names = {}
    for name, pid in people.items():
        first_names.setdefault(name.split()[0], set()).add(pid)
    facts = []
    for u in s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind.in_(("page", "slide", "segment", "message")))
                       .order_by(Unit.kind, Unit.ordinal)):
        for sent in _SENT.split(u.text or ""):
            sent = sent.strip(" •-\t")
            if len(sent) < 8:
                continue
            kind = "decision" if _DECISION.search(sent) else "action_item" if _ACTION.search(sent) else None
            docno = find_docno(sent)
            if not kind and not docno:
                continue
            persons = sorted({pid for name, pid in people.items() if name in sent}
                             | {pid for fn, pids in first_names.items() if len(pids) == 1
                                and re.search(rf"\b{re.escape(fn)}\b", sent) for pid in pids})
            facts.append({
                "type": kind or "reference", "text": sent[:500], "persons": persons,
                "doc_numbers": [docno] if docno else [],
                "provenance": {"content": sha, "unit_kind": u.kind, "ordinal": u.ordinal,
                               "start_s": u.start_s, "end_s": u.end_s, "speaker": u.speaker,
                               "slide": (u.meta or {}).get("slide")}})
    return facts
