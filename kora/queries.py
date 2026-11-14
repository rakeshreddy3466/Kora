"""Shared read helpers."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Content, File, Unit
from .versioning.diff import UnitView

DOC_KINDS = ("page", "slide")
LIVE = ("active",)


def doc_units(s: Session, sha: str) -> list[Unit]:
    return list(s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind.in_(DOC_KINDS))
                          .order_by(Unit.ordinal)))


def unit_views(s: Session, sha: str) -> list[UnitView]:
    return [UnitView(u.ordinal, u.unit_hash, u.body_hash, u.text, u.phash) for u in doc_units(s, sha)]


def files_of(s: Session, sha: str, statuses: tuple[str, ...] | None = LIVE) -> list[File]:
    q = select(File).where(File.sha256 == sha)
    if statuses:
        q = q.where(File.status.in_(statuses))
    return list(s.scalars(q.order_by(File.id)))


def content_time(s: Session, sha: str):
    """Earliest known modification time across all copies (download copies are newer)."""
    times = [f.modified_at_source or f.created_at_source for f in files_of(s, sha, None)
             if (f.modified_at_source or f.created_at_source)]
    if times:
        return min(times)
    c = s.get(Content, sha)
    return c.created_at if c else None


def display_name(s: Session, sha: str) -> str:
    fs = files_of(s, sha, None)
    if not fs:
        return sha[:12]
    fs.sort(key=lambda f: (f.status != "active", len(f.filename)))
    return fs[0].filename
