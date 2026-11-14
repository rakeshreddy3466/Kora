"""Plan lifecycle and the two-phase, reversible deletion executor.

  draft --submit--> pending_approval --approve x N--> approved --execute--> executing
        --(all items processed)--> quarantined --(grace period)--> purge --> purged
                                          \\--restore--> restored
  reject / cancel are terminal. Approvals bind to the evidence hash; the
  creator can never approve their own plan (separation of duties) and each
  approver counts once.

Per-item saga (each step idempotent, each crash-resumable via the job queue):
  1. re-evaluate every hard check against current state      (TOCTOU guard)
  2. record intent: item -> quarantining
  3. copy victim bytes to quarantine tier (or archive tier) and verify hash
  4. conditional removal at the source: the connector re-hashes and removes
     only if bytes still equal the planned hash, leaving a pointer stub
  5. item -> quarantined, file -> quarantined, audit
Purge re-checks again (legal hold may have arrived; survivor may have gone),
deletes the quarantine copy only when no other open item references it, and
leaves a tombstone (stub + DB row + audit event) - never a silent gap.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, clock, db
from ..connectors import ConflictError, get_action_connector, get_connector
from ..jobs import enqueue, handler
from .. import EXTRACTOR_VERSION
from ..models import Content, DeletionItem, DeletionPlan, File, Principal, Rendition, Source, Unit
from ..security.auth import Identity
from ..storage import get_blobstore
from . import checks
from .policy import get_policy

log = logging.getLogger(__name__)


class PlanStateError(Exception):
    pass


def _plan(s: Session, plan_id: str, expected_version: int | None = None) -> DeletionPlan:
    p = s.scalars(select(DeletionPlan).where(DeletionPlan.id == plan_id).with_for_update()).first()
    if p is None:
        raise PlanStateError("plan not found")
    if expected_version is not None and p.row_version != expected_version:
        raise PlanStateError(f"plan changed (version {p.row_version} != {expected_version}); reload and retry")
    return p


def _require(p: DeletionPlan, *states: str) -> None:
    if p.status not in states:
        raise PlanStateError(f"plan is {p.status}; expected {' or '.join(states)}")


def submit(s: Session, ident: Identity, plan_id: str, expected_version: int | None = None) -> DeletionPlan:
    p = _plan(s, plan_id, expected_version)
    _require(p, "draft")
    if p.created_by != ident.principal_id and not ident.has("admin"):
        raise PermissionError("only the creator can submit")
    if not any(it.deletable for it in p.items):
        raise PlanStateError("nothing deletable in this plan")
    p.status, p.updated_at = "pending_approval", clock.now()
    audit.append(s, ident.principal_id, "plan.submitted", "plan", p.id, {"evidence_hash": p.evidence["evidence_hash"]})
    return p


def approve(s: Session, ident: Identity, plan_id: str, comment: str = "", expected_version: int | None = None) -> DeletionPlan:
    if not ident.has("approver"):
        raise PermissionError("approver role required")
    p = _plan(s, plan_id, expected_version)
    _require(p, "pending_approval")
    if ident.principal_id == p.created_by:
        raise PermissionError("separation of duties: the plan creator cannot approve it")
    approver = s.get(Principal, ident.principal_id)
    if approver is None or approver.status != "active":
        raise PermissionError("approver must be an active principal")
    if any(a["principal"] == ident.principal_id for a in p.approvals):
        raise PlanStateError("already approved by this principal")
    p.approvals = [*p.approvals, {"principal": ident.principal_id, "at": clock.now().isoformat(),
                                  "comment": comment[:500], "evidence_hash": p.evidence["evidence_hash"]}]
    if len(p.approvals) >= p.required_approvals:
        p.status = "approved"
    p.updated_at = clock.now()
    audit.append(s, ident.principal_id, "plan.approved", "plan", p.id,
                 {"approvals": len(p.approvals), "required": p.required_approvals,
                  "evidence_hash": p.evidence["evidence_hash"]})
    return p


def reject(s: Session, ident: Identity, plan_id: str, reason: str) -> DeletionPlan:
    if not ident.has("approver"):
        raise PermissionError("approver role required")
    p = _plan(s, plan_id)
    _require(p, "pending_approval", "approved")
    p.status, p.updated_at = "rejected", clock.now()
    _release(p)
    audit.append(s, ident.principal_id, "plan.rejected", "plan", p.id, {"reason": reason[:500]})
    return p


def cancel(s: Session, ident: Identity, plan_id: str) -> DeletionPlan:
    p = _plan(s, plan_id)
    _require(p, "draft", "pending_approval", "approved")
    if p.created_by != ident.principal_id and not ident.has("admin"):
        raise PermissionError("only the creator or an admin can cancel")
    p.status, p.updated_at = "cancelled", clock.now()
    _release(p)
    audit.append(s, ident.principal_id, "plan.cancelled", "plan", p.id, {})
    return p


def _release(p: DeletionPlan) -> None:
    for it in p.items:
        if it.state == "planned":
            it.state = "cancelled"


def execute(s: Session, ident: Identity, plan_id: str) -> DeletionPlan:
    if not ident.has("operator"):
        raise PermissionError("operator role required")
    p = _plan(s, plan_id)
    _require(p, "approved")
    if any(a["evidence_hash"] != p.evidence["evidence_hash"] for a in p.approvals):
        raise PlanStateError("approvals do not match current evidence; re-approval required")
    pol = get_policy()
    p.status = "executing"
    p.quarantine_until = checks.grace_until(pol["quarantine_days"])
    n = 0
    for it in p.items:
        if it.state == "planned" and it.deletable:
            enqueue(s, "plan.quarantine_item", {"item_id": it.id}, key=f"quarantine:{it.id}", priority=20, max_attempts=8)
            n += 1
    audit.append(s, ident.principal_id, "plan.execute_started", "plan", p.id,
                 {"items": n, "quarantine_until": p.quarantine_until.isoformat()})
    if n == 0:
        p.status = "quarantined"
    return p


def purge(s: Session, ident: Identity, plan_id: str) -> DeletionPlan:
    if not ident.has("operator"):
        raise PermissionError("operator role required")
    p = _plan(s, plan_id)
    _require(p, "quarantined")
    if p.quarantine_until and clock.now() < p.quarantine_until:
        raise PlanStateError(f"grace period runs until {p.quarantine_until.isoformat()}")
    p.status = "purging"
    attempt = clock.now().isoformat()
    for it in p.items:
        if it.state == "quarantined":
            it.error = None
            enqueue(s, "plan.purge_item", {"item_id": it.id}, key=f"purge:{it.id}:{attempt}", priority=20,
                    max_attempts=8)
    audit.append(s, ident.principal_id, "plan.purge_started", "plan", p.id, {})
    return p


def restore(s: Session, ident: Identity, plan_id: str, item_ids: list[str] | None = None) -> DeletionPlan:
    if not ident.has("operator"):
        raise PermissionError("operator role required")
    p = _plan(s, plan_id)
    _require(p, "quarantined", "purging")
    if p.status == "purging":
        p.status = "quarantined"
    n = 0
    for it in p.items:
        if it.state == "quarantined" and (not item_ids or it.id in item_ids):
            enqueue(s, "plan.restore_item", {"item_id": it.id, "by": ident.principal_id},
                    key=f"restore:{it.id}:{clock.now().isoformat()}", priority=10, max_attempts=8)
            n += 1
    audit.append(s, ident.principal_id, "plan.restore_requested", "plan", p.id, {"items": n})
    return p


# --------------------------------------------------------------------------- item handlers
def _finish_if_done(s: Session, plan_id: str) -> None:
    """Derive the plan status from its items' states."""
    p = _plan(s, plan_id)
    items = p.items
    before = p.status
    if p.status == "executing":
        if not any(it.state in ("planned", "quarantining") and it.deletable for it in items):
            p.status = "quarantined"
    elif p.status == "purging":
        if not any(it.state == "quarantined" and not it.error for it in items):
            p.status = "quarantined" if any(it.state == "quarantined" for it in items) else "purged"
    elif p.status == "quarantined":
        if not any(it.state in ("quarantined", "quarantining", "restoring") for it in items):
            if any(it.state == "purged" for it in items):
                p.status = "purged"
            elif any(it.state == "restored" for it in items):
                p.status = "restored"
    if p.status != before:
        counts: dict[str, int] = {}
        for it in items:
            counts[it.state] = counts.get(it.state, 0) + 1
        audit.append(s, "system", f"plan.{p.status}", "plan", p.id, {"items": counts})
    p.updated_at = clock.now()


def _survivor_live(s: Session, it: DeletionItem, check_victim: bool = True) -> str | None:
    """Check victim and survivor at their sources right now (cheap eTag/stat; the
    connector re-hashes the victim again at removal). Returns an error or None."""
    if check_victim:
        v = s.get(File, it.file_id)
        st = get_connector(s.get(Source, v.source_name)).stat(v.path)
        if st is None:
            return "victim no longer exists at source"
        if st.etag != v.source_etag:
            return "victim changed at source since the plan was evidenced"
    if not it.survivor_file_id:
        return None
    sv = s.get(File, it.survivor_file_id)
    conn = get_connector(s.get(Source, sv.source_name))
    st = conn.stat(sv.path)
    if st is None:
        return "survivor no longer exists at source"
    if st.etag != sv.source_etag:
        return "survivor changed at source since it was verified"
    return None


def _block(s: Session, it: DeletionItem, reason: str, checks_list: list | None = None) -> None:
    it.state, it.error, it.updated_at = "blocked", reason[:2000], clock.now()
    it.deletable = False
    if checks_list is not None:
        it.checks = checks_list
    # the full reason (may contain file names) stays in the erasable item row; the immutable log gets the category
    audit.append(s, "system", "item.blocked", "deletion_item", it.id,
                 {"plan": it.plan_id, "reason": reason.split(":")[0][:80]})


def _stub(it: DeletionItem, v: File, sv: File | None) -> dict:
    return {"kora_stub": 1, "item_id": it.id, "plan_id": it.plan_id, "original_name": v.filename,
            "sha256": v.sha256, "size_bytes": v.size_bytes, "reason": it.dup_class, "recovery": it.recovery_class,
            "canonical": ({"system": sv.system, "site": sv.site, "path": sv.path} if sv else None),
            "capsule": v.sha256, "quarantined_at": clock.now().isoformat(),
            "restore": "ask your KORA operator to restore this item during the grace period"}


@handler("plan.quarantine_item")
def quarantine_item(ctx) -> None:
    item_id = ctx.payload["item_id"]
    store = get_blobstore()
    # 1+2: re-check and record intent
    with db.session_scope() as s:
        it = s.get(DeletionItem, item_id)
        if it.state in ("quarantined", "purged", "restored", "blocked"):
            return
        p = s.get(DeletionPlan, it.plan_id)
        if p.status != "executing":
            return
        s.scalars(select(Content).where(Content.sha256 == it.victim_sha).with_for_update()).first()
        if it.state == "planned":
            departing = p.scope.get("owner") if p.kind == "departure" else None
            fresh = checks.evaluate(s, it, p, "execute", departing)
            _, ok = checks.score(fresh)
            err = None if not ok else _survivor_live(s, it)
            if not ok or err:
                failed = [c["name"] for c in fresh if c["hard"] and not c["passed"]]
                _block(s, it, err or f"checks failed at execution: {failed}", fresh)
                _finish_if_done(s, it.plan_id)
                return
            it.checks, it.state, it.updated_at = fresh, "quarantining", clock.now()
            audit.append(s, "system", "item.quarantining", "deletion_item", it.id, {"plan": it.plan_id})
        v = s.get(File, it.file_id)
        sv = s.get(File, it.survivor_file_id) if it.survivor_file_id else None
        src = s.get(Source, v.source_name)
        s.flush()
        s.expunge_all()
    # 3: copy to quarantine / archive tier, verify
    tier = "archive" if it.action == "MOVE_TO_ARCHIVE" else "quarantine"
    conn = get_connector(src)
    if not store.exists(tier, it.victim_sha):
        with conn.open(v.path) as fh:
            ref = store.put_stream(tier, fh)
        if ref.key != it.victim_sha:
            store.delete(tier, ref.key)
            with ctx.tx() as s:
                _block(s, s.get(DeletionItem, item_id), "source bytes changed before quarantine copy")
                _finish_if_done(s, it.plan_id)
            return
    if not store.verify(tier, it.victim_sha):
        raise RuntimeError(f"{tier} copy failed verification; will retry")
    if it.action == "REPLACE_WITH_RENDITION":
        return _replace_with_rendition(ctx, item_id, it, src, v.path, store)
    # 4: conditional removal at source (connector re-hashes; refuses if changed)
    try:
        stub_path = get_action_connector(src).replace_with_stub(v.path, it.victim_sha, _stub(it, v, sv))
    except ConflictError as e:
        with ctx.tx() as s:
            _block(s, s.get(DeletionItem, item_id), f"source conflict: {e}")
            _finish_if_done(s, it.plan_id)
        return
    # 5: commit
    with ctx.tx() as s:
        it2 = s.get(DeletionItem, item_id)
        f = s.get(File, it2.file_id)
        it2.state, it2.quarantine_tier, it2.stub_path, it2.updated_at = "quarantined", tier, stub_path, clock.now()
        f.status = "quarantined"
        audit.append(s, "system", "item.quarantined", "deletion_item", it2.id,
                     {"plan": it2.plan_id, "file": f.id, "system": f.system, "sha256": f.sha256,
                      "tier": tier, "class": it2.dup_class, "bytes": it2.bytes_reclaimable})
        _finish_if_done(s, it2.plan_id)


def _swap_bytes(src, path: str, store, tier: str, key: str, expected_sha: str) -> None:
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "swap.bin"
        store.write_to(tier, key, tmp)                     # verifies the hash on the way out
        with open(tmp, "rb") as fh:
            get_action_connector(src).replace_content(path, expected_sha, fh, key)


def _point_file_at(s: Session, f: File, sha: str, size: int, like: Content | None, derived_from: str | None) -> None:
    if s.get(Content, sha) is None:
        meta = {"rendition_of": derived_from} if derived_from else {}
        s.add(Content(sha256=sha, size_bytes=size, detected_type=like.detected_type if like else f.detected_type,
                      meta=meta))
        s.flush()
    st = get_connector(s.get(Source, f.source_name)).stat(f.path)
    f.sha256, f.size_bytes, f.source_etag = sha, size, st.etag if st else f.source_etag
    c = s.get(Content, sha)
    if c.extracted and c.extractor_version == EXTRACTOR_VERSION:
        f.ingest_state = "ready"
    else:
        f.ingest_state = "hashed"
        enqueue(s, "content.extract", {"sha": sha, "file_id": f.id}, key=f"extract:{sha}:v{EXTRACTOR_VERSION}")


def _replace_with_rendition(ctx, item_id: str, it: DeletionItem, src, path: str, store) -> None:
    """Compression saga: original is already verified in quarantine; swap the bytes at the
    source only if they are still the planned original, then point the file at the rendition."""
    try:
        _swap_bytes(src, path, store, "hot", it.survivor_sha, it.victim_sha)
    except ConflictError as e:
        with ctx.tx() as s:
            _block(s, s.get(DeletionItem, item_id), f"source conflict: {e}")
            _finish_if_done(s, it.plan_id)
        return
    with ctx.tx() as s:
        it2 = s.get(DeletionItem, item_id)
        f = s.get(File, it2.file_id)
        r_size = s.scalar(select(Rendition.size_bytes).where(Rendition.blob_key == it2.survivor_sha).limit(1))
        _point_file_at(s, f, it2.survivor_sha, r_size, s.get(Content, it2.victim_sha), it2.victim_sha)
        it2.state, it2.quarantine_tier, it2.stub_path, it2.updated_at = "quarantined", "quarantine", None, clock.now()
        audit.append(s, "system", "item.replaced", "deletion_item", it2.id,
                     {"plan": it2.plan_id, "file": f.id, "system": f.system, "from_sha256": it2.victim_sha,
                      "to_sha256": it2.survivor_sha, "class": it2.dup_class, "bytes": it2.bytes_reclaimable})
        _finish_if_done(s, it2.plan_id)


@handler("plan.purge_item")
def purge_item(ctx) -> None:
    item_id = ctx.payload["item_id"]
    store = get_blobstore()
    with ctx.tx() as s:
        it = s.get(DeletionItem, item_id)
        if it.state != "quarantined":
            return
        p = s.get(DeletionPlan, it.plan_id)
        departing = p.scope.get("owner") if p.kind == "departure" else None
        fresh = [c for c in checks.evaluate(s, it, p, "purge", departing) if c["name"] != "no_conflicting_plan"]
        _, ok = checks.score(fresh)
        err = None if not ok else _survivor_live(s, it, check_victim=it.action == "REPLACE_WITH_RENDITION")
        if not ok or err:
            failed = [c["name"] for c in fresh if c["hard"] and not c["passed"]]
            it.error = err or f"purge blocked: {failed}"
            it.checks = fresh
            audit.append(s, "system", "item.purge_blocked", "deletion_item", it.id, {"reason": it.error})
            # stays quarantined (still restorable); the operator decides
            _finish_if_done(s, it.plan_id)
            return
        f = s.get(File, it.file_id)
        if it.quarantine_tier == "quarantine":
            others = s.scalar(select(DeletionItem.id).where(
                DeletionItem.victim_sha == it.victim_sha, DeletionItem.id != it.id,
                DeletionItem.quarantine_tier == "quarantine",
                DeletionItem.state.in_(("quarantining", "quarantined", "restoring"))).limit(1))
            if others is None:
                store.delete("quarantine", it.victim_sha)
        if it.stub_path:
            try:
                get_action_connector(s.get(Source, f.source_name)).annotate_stub(
                    it.stub_path, {"purged_at": clock.now().isoformat(), "tombstone": True})
            except Exception as e:  # stub annotation is best-effort; tombstone also lives in DB + audit
                log.warning("could not annotate stub: %s", e)
        it.state, it.checks, it.updated_at = "purged", fresh, clock.now()
        if it.action == "REPLACE_WITH_RENDITION":
            _release_rendition(s, it, store)                # file stays live, now holding the rendition
        else:
            f.status = "purged"
        audit.append(s, "system", "item.purged", "deletion_item", it.id,
                     {"tombstone": {"file": f.id, "system": f.system, "sha256": f.sha256,
                                    "size": f.size_bytes, "class": it.dup_class, "recovery": it.recovery_class,
                                    "survivor": it.survivor_file_id, "plan": it.plan_id,
                                    "policy_hash": p.policy_hash}})
        _finish_if_done(s, it.plan_id)


def _release_rendition(s: Session, it: DeletionItem, store) -> None:
    """The source now holds the rendition bytes; KORA's hot copy is redundant once no live copy of
    the original and no pending replacement still needs it. The Rendition row (its measurements) stays."""
    pending = s.scalar(select(DeletionItem.id).where(DeletionItem.survivor_sha == it.survivor_sha,
                                                     DeletionItem.action == "REPLACE_WITH_RENDITION",
                                                     DeletionItem.id != it.id,
                                                     DeletionItem.state.in_(("planned", "quarantining"))).limit(1))
    orig_live = s.scalar(select(File.id).where(File.sha256 == it.victim_sha, File.status == "active").limit(1))
    if pending is None and orig_live is None:
        for r in s.scalars(select(Rendition).where(Rendition.blob_key == it.survivor_sha)):
            r.blob_key = None
            r.validation = {**r.validation, "blob_released_at": clock.now().isoformat(), "rendition_sha": it.survivor_sha}
        if not s.scalar(select(Unit.id).where((Unit.blob_key == it.survivor_sha) | (Unit.thumb_key == it.survivor_sha)).limit(1)):
            store.delete("hot", it.survivor_sha)


@handler("plan.restore_item")
def restore_item(ctx) -> None:
    item_id = ctx.payload["item_id"]
    store = get_blobstore()
    with db.session_scope() as s:
        it = s.get(DeletionItem, item_id)
        if it.state not in ("quarantined", "restoring"):
            return
        it.state = "restoring"
        f = s.get(File, it.file_id)
        src = s.get(Source, f.source_name)
        tier, sha, path, stub = it.quarantine_tier, it.victim_sha, f.path, it.stub_path
        action, rendition_sha = it.action, it.survivor_sha
        s.expunge(src)
    if action == "REPLACE_WITH_RENDITION":
        _swap_bytes(src, path, store, tier, sha, rendition_sha)   # conditional: only over the approved rendition
    else:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "restore.bin"
            store.write_to(tier, sha, tmp)                     # verifies hash
            with open(tmp, "rb") as fh:
                get_action_connector(src).restore(path, fh, sha, stub)
    with ctx.tx() as s:
        it = s.get(DeletionItem, item_id)
        f = s.get(File, it.file_id)
        if action == "REPLACE_WITH_RENDITION":
            orig = s.get(Content, sha)
            _point_file_at(s, f, sha, orig.size_bytes, orig, None)
        conn = get_connector(s.get(Source, f.source_name))
        st = conn.stat(f.path)
        f.status, f.source_etag = "active", st.etag if st else f.source_etag
        it.state, it.stub_path, it.updated_at = "restored", None, clock.now()
        if tier == "quarantine":
            others = s.scalar(select(DeletionItem.id).where(
                DeletionItem.victim_sha == sha, DeletionItem.id != it.id, DeletionItem.quarantine_tier == "quarantine",
                DeletionItem.state.in_(("quarantining", "quarantined", "restoring"))).limit(1))
            if others is None:
                store.delete("quarantine", sha)
        audit.append(s, ctx.payload.get("by", "system"), "item.restored", "deletion_item", it.id,
                     {"plan": it.plan_id, "file": f.id})
        _finish_if_done(s, it.plan_id)
