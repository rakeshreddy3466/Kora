"""Append-only, tamper-evident audit log.

Every event carries hash = sha256(canonical(body incl. prev_hash)) and
mac = HMAC(audit_key, hash). Editing, deleting or re-ordering any event breaks
the chain; rewriting the whole chain requires the MAC key, which the database
never sees. The head row is locked while appending so writers serialise and
the chain cannot fork. Events are written in the same transaction as the
action they describe, so an action without its audit record cannot commit.
"""
from __future__ import annotations

import hmac as _hmac
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .config import get_settings
from .models import AuditEvent, AuditHead
from .util import canonical_json, hmac_hex, sha256_bytes

GENESIS = "0" * 64


def _body(seq: int, at, actor: str, action: str, subject_type: str, subject_id: str, details: dict,
          prev: str) -> dict:
    return {"seq": seq, "at": at.isoformat(), "actor": actor, "action": action,
            "subject_type": subject_type, "subject_id": subject_id, "details": details, "prev": prev}


def append(s: Session, actor: str, action: str, subject_type: str, subject_id: str,
           details: dict | None = None) -> AuditEvent:
    details = details or {}
    head = s.scalars(select(AuditHead).where(AuditHead.id == 1).with_for_update()).first()
    if head is None:
        head = AuditHead(id=1, seq=0, hash=GENESIS)
        s.add(head)
        s.flush()
    seq = head.seq + 1
    at = clock.now()
    # round-trip details through JSON so what we hash is exactly what we store
    import json
    details = json.loads(canonical_json(details))
    body = _body(seq, at, actor, action, subject_type, subject_id, details, head.hash)
    h = sha256_bytes(canonical_json(body))
    ev = AuditEvent(seq=seq, at=at, actor=actor, action=action, subject_type=subject_type,
                    subject_id=subject_id, details=details, prev_hash=head.hash, hash=h,
                    mac=hmac_hex(get_settings().audit_mac_key, h.encode()))
    s.add(ev)
    head.seq, head.hash = seq, h
    s.flush()
    return ev


@dataclass
class VerifyResult:
    ok: bool
    events: int
    head_hash: str
    first_bad_seq: int | None = None
    reason: str | None = None


def verify(s: Session) -> VerifyResult:
    key = get_settings().audit_mac_key
    prev = GENESIS
    expected_seq = 1
    n = 0
    for ev in s.scalars(select(AuditEvent).order_by(AuditEvent.seq)).yield_per(1000):
        n += 1
        if ev.seq != expected_seq:
            return VerifyResult(False, n, prev, ev.seq, f"gap: expected seq {expected_seq}")
        if ev.prev_hash != prev:
            return VerifyResult(False, n, prev, ev.seq, "prev_hash mismatch")
        body = _body(ev.seq, ev.at, ev.actor, ev.action, ev.subject_type, ev.subject_id, ev.details, ev.prev_hash)
        h = sha256_bytes(canonical_json(body))
        if h != ev.hash:
            return VerifyResult(False, n, prev, ev.seq, "content hash mismatch (event modified)")
        if not _hmac.compare_digest(hmac_hex(key, h.encode()), ev.mac):
            return VerifyResult(False, n, prev, ev.seq, "MAC mismatch (chain rewritten without key)")
        prev = ev.hash
        expected_seq += 1
    head = s.get(AuditHead, 1)
    if head and (head.hash != prev or head.seq != expected_seq - 1):
        return VerifyResult(False, n, prev, head.seq, "head does not match last event (truncation)")
    return VerifyResult(True, n, prev)
