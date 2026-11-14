"""API-key authentication and role-based authorisation.

Keys are random 32-byte tokens shown once; only SHA-256(key) is stored.
Roles:
  viewer    search, read capsules/findings (always ACL-filtered)
  analyst   scan sources, create deletion plans, run departure assessments
  approver  approve/reject plans (never their own: separation of duties)
  operator  execute, purge, restore approved plans
  auditor   read audit log, verify chain, see all plans
  dpo       data protection officer: decides data-subject requests (GDPR Art. 15/17)
  admin     manage sources, principals and keys
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import ApiKey, Principal
from ..util import sha256_bytes

ROLES = {"viewer", "analyst", "approver", "operator", "auditor", "dpo", "admin"}
PREFIX = "kora_"


@dataclass(frozen=True)
class Identity:
    principal_id: str
    roles: frozenset[str]
    groups: tuple[str, ...] = field(default_factory=tuple)

    def has(self, role: str) -> bool:
        return role in self.roles or "admin" in self.roles

    @property
    def acl_tokens(self) -> set[str]:
        return {"everyone", f"user:{self.principal_id}", *(f"group:{g}" for g in self.groups)}


SYSTEM = Identity("system", frozenset({"admin"}))


def create_key(s: Session, principal_id: str, roles: list[str]) -> str:
    bad = set(roles) - ROLES
    if bad:
        raise ValueError(f"unknown roles: {sorted(bad)}")
    if s.get(Principal, principal_id) is None:
        raise ValueError(f"unknown principal {principal_id}")
    raw = PREFIX + secrets.token_urlsafe(32)
    s.add(ApiKey(principal_id=principal_id, key_hash=sha256_bytes(raw.encode()), roles=sorted(roles)))
    return raw


def authenticate(s: Session, raw_key: str | None) -> Identity | None:
    if not raw_key or not raw_key.startswith(PREFIX) or len(raw_key) > 200:
        return None
    digest = sha256_bytes(raw_key.encode())
    row = s.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
    # constant-time compare on the digest even though lookup is by index
    if row is None or row.revoked or not hmac.compare_digest(row.key_hash, digest):
        return None
    p = s.get(Principal, row.principal_id)
    if p is None or p.status == "departed":
        return None
    row.last_used_at = clock.now()
    return Identity(p.id, frozenset(row.roles), tuple(p.groups or ()))
