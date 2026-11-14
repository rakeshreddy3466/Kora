"""ACL evaluation. ACL entries: "everyone", "user:<id>", "group:<name>".

Used for: permission-aware search, and the deletion *permission coverage*
check (everyone who can read the copy being deleted must be able to read the
survivor, otherwise deduplication silently revokes access)."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import File, Principal

EVERYONE = "everyone"


def readers(s: Session, acl: list[str], *, active_only: bool = True,
            exclude: set[str] | None = None) -> set[str]:
    """Expand an ACL to the set of principal ids (or {"everyone"})."""
    if EVERYONE in (acl or []):
        return {EVERYONE}
    users: set[str] = set()
    groups = {a.split(":", 1)[1] for a in acl or [] if a.startswith("group:")}
    users |= {a.split(":", 1)[1] for a in acl or [] if a.startswith("user:")}
    if groups:
        for p in s.scalars(select(Principal)):
            if set(p.groups or ()) & groups:
                users.add(p.id)
    if active_only:
        known = {p.id: p.status for p in s.scalars(select(Principal).where(Principal.id.in_(users)))}
        users = {u for u in users if known.get(u, "active") == "active"}
    if exclude:
        users -= exclude
    return users


def coverage_gap(s: Session, victim: File, survivor: File, exclude: set[str] | None = None) -> set[str]:
    """Active readers of `victim` who would lose access if only `survivor` remained."""
    v = readers(s, victim.acl, exclude=exclude)
    sv = readers(s, survivor.acl, active_only=False)
    if EVERYONE in sv:
        return set()
    if EVERYONE in v:
        return {EVERYONE}
    return v - sv


def can_read(tokens: set[str], acl: list[str]) -> bool:
    return bool(tokens & set(acl or []))
