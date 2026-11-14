"""Survivor (canonical copy) selection.

When several copies are equivalent, which one stays? Deterministic ranking,
highest first, with the reasons recorded in the evidence report:
  1. system of record precedence (policy: SharePoint > Play > OneDrive > upload)
  2. owner is active (not departing/departed)
  3. stronger retention governance (record > regulatory > standard > transient)
  4. broader audience (number of active readers; "everyone" wins)
  5. original name (no "(1)" / "Copy of" marker)
  6. earliest creation (the original upload, not a later download)
  7. file id (stable tie-break)
Only active files in approved repositories can survive.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import File, Principal, Source
from ..security.acl import EVERYONE, readers
from ..versioning import naming
from .policy import get_policy

FAR = datetime(9999, 1, 1)


def _features(s: Session, f: File, pol) -> dict:
    owner = s.get(Principal, f.owner_id) if f.owner_id else None
    r = readers(s, f.acl)
    return {
        "precedence": pol["system_precedence"].get(f.system, 0),
        "owner_active": bool(owner and owner.status == "active"),
        "retention": pol["retention_strength"].get(f.retention_label, 1),
        "audience": 10 ** 6 if EVERYONE in r else len(r),
        "original_name": naming.parse(f.filename).copy_marker == 0,
        "created": f.created_at_source or FAR,
    }


def _key(feat: dict, f: File) -> tuple:
    return (feat["precedence"], feat["owner_active"], feat["retention"], feat["audience"],
            feat["original_name"], -feat["created"].timestamp(), f.id)


def choose(s: Session, files: list[File], extra_key=None) -> tuple[File | None, list[str]]:
    pol = get_policy()
    approved = {src.name for src in s.query(Source).filter(Source.approved_repository.is_(True))}
    elig = [f for f in files if f.status == "active" and f.source_name in approved]
    if not elig:
        return None, ["no active copy in an approved repository"]
    feats = {f.id: _features(s, f, pol) for f in elig}
    ranked = sorted(elig, key=lambda f: ((extra_key(f),) if extra_key else ()) + _key(feats[f.id], f), reverse=True)
    win = ranked[0]
    w = feats[win.id]
    reasons = [f"{win.system} (precedence {w['precedence']})"]
    if len(ranked) > 1:
        r = feats[ranked[1].id]
        if w["owner_active"] and not r["owner_active"]:
            reasons.append(f"owner {win.owner_id} is active")
        if w["retention"] > r["retention"]:
            reasons.append(f"stronger retention label '{win.retention_label}'")
        if w["audience"] > r["audience"]:
            reasons.append("broader audience" if w["audience"] < 10 ** 6 else "readable by everyone")
        if w["original_name"] and not r["original_name"]:
            reasons.append("original file name (no copy marker)")
        if w["created"] < r["created"]:
            reasons.append(f"earliest copy ({w['created']:%Y-%m-%d})")
    if extra_key:
        reasons.insert(0, "highest quality rendition")
    return win, reasons
