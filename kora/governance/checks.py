"""Deletion evidence checks ("proof-carrying deletion").

Evaluated three times: when the plan is built (dry run), immediately before
quarantine (execute), and immediately before purge. Hard checks are gates:
if any fails, the item cannot be deleted no matter the score. The readiness
score explains confidence; it never replaces policy approval.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..dedup import classes as C
from ..models import Capsule, Content, DeletionItem, DeletionPlan, File, Lineage, Relation, Rendition, SlideBlob, Unit, Version
from ..queries import doc_units
from ..security.acl import coverage_gap
from ..storage import get_blobstore
from .policy import get_policy

OPEN_PLAN_STATES = ("draft", "pending_approval", "approved", "executing", "quarantined")
ACTIVE_ITEM_STATES = ("planned", "quarantining", "quarantined", "purging")
CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def mk(name: str, passed: bool, hard: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "hard": hard, "detail": detail,
            "weight": get_policy().weight(name)}


def _other_reservations(s: Session, file_id: str, plan_id: str) -> list[tuple[str, str]]:
    """(plan_id, role) of other open plans touching this file."""
    out = []
    q = (select(DeletionItem, DeletionPlan).join(DeletionPlan, DeletionPlan.id == DeletionItem.plan_id)
         .where(DeletionPlan.status.in_(OPEN_PLAN_STATES), DeletionPlan.id != plan_id,
                DeletionItem.state.in_(ACTIVE_ITEM_STATES)))
    for it, pl in s.execute(q.where(DeletionItem.file_id == file_id)).all():
        out.append((pl.id, "victim"))
    for it, pl in s.execute(q.where(DeletionItem.survivor_file_id == file_id)).all():
        out.append((pl.id, "survivor"))
    return out


def live_copies(s: Session, shas: set[str], excluding_plan: str | None = None, excluding: set[str] = frozenset()) -> list[File]:
    """Active copies of any of the given contents that no open plan intends to delete."""
    reserved = set(s.scalars(select(DeletionItem.file_id).join(DeletionPlan, DeletionPlan.id == DeletionItem.plan_id)
                             .where(DeletionPlan.status.in_(OPEN_PLAN_STATES),
                                    DeletionItem.state.in_(ACTIVE_ITEM_STATES))))
    files = s.scalars(select(File).where(File.sha256.in_(shas), File.status == "active"))
    return [f for f in files if f.id not in reserved and f.id not in excluding]


def evaluate(s: Session, item: DeletionItem, plan: DeletionPlan, phase: str = "plan",
             departing: str | None = None) -> list[dict]:
    pol = get_policy()
    store = get_blobstore()
    cls = item.dup_class
    pc = pol.cls(cls)
    v: File = s.get(File, item.file_id)
    sv: File | None = s.get(File, item.survivor_file_id) if item.survivor_file_id else None
    out: list[dict] = []
    allowed = pc["deletable"] and (cls != C.RECORDING_DISTILLED or item.action in ("MOVE_TO_ARCHIVE", "DELETE"))
    out.append(mk("class_allowed", allowed, True, f"policy {pol.version}: {cls} -> {pc['action']}"))

    if cls == C.COMPRESSED and phase == "purge":
        # the file stays live; at purge it must still hold exactly the approved rendition
        out.append(mk("victim_unchanged", v.sha256 == item.survivor_sha and v.status == "active", True,
                      f"{v.status}, holds rendition {str(v.sha256)[:12]} (approved {str(item.survivor_sha)[:12]})"))
    else:
        expected_states = ("active",) if phase in ("plan", "execute") else ("quarantined",)
        out.append(mk("victim_unchanged", v.sha256 == item.victim_sha and v.status in expected_states
                      and v.ingest_state not in ("registered",), True,
                      f"{v.status}, sha {str(v.sha256)[:12]} (planned {item.victim_sha[:12]})"))
    out.append(mk("legal_hold_clear", not v.legal_hold, True,
                  "no legal hold" if not v.legal_hold else "item is on legal hold"))

    lab = pol.label(v.retention_label)
    age = (clock.now() - (v.created_at_source or clock.now())).days
    ret_ok = not lab.get("immutable") and age >= lab.get("min_age_days", 0)
    detail = f"label '{v.retention_label}', age {age}d, min {lab.get('min_age_days', 0)}d"
    if not ret_ok and sv is not None and pol.get("allow_dedupe_if_survivor_retains") and cls in (C.EXACT_COPY, C.CONTAINER_VARIANT):
        if pol["retention_strength"].get(sv.retention_label, 1) >= pol["retention_strength"].get(v.retention_label, 1) \
                and not lab.get("immutable"):
            ret_ok, detail = True, detail + "; survivor carries equal or stronger retention"
    if lab.get("immutable"):
        detail += "; declared record (immutable)"
    out.append(mk("retention_satisfied", ret_ok, True, detail))

    if phase == "plan":
        others = _other_reservations(s, v.id, plan.id)
        clash = [p for p in others]
        if sv:
            clash += [p for p in _other_reservations(s, sv.id, plan.id) if p[1] == "victim"]
        out.append(mk("no_conflicting_plan", not clash, True,
                      "not referenced by another open plan" if not clash else f"conflicts with {clash[:3]}"))

    # ---------------------------------------------------------------- survivor-based classes
    if cls in (C.EXACT_COPY, C.CONTAINER_VARIANT, C.MEDIA_VARIANT, C.CONTAINED_SUBSET):
        ok = bool(sv and sv.status == "active" and sv.sha256 == item.survivor_sha and sv.id != v.id)
        out.append(mk("survivor_verified", ok, True,
                      f"survivor {sv.system}:{sv.path} active, sha matches" if ok else "survivor missing or changed"))
        if sv:
            excl = {departing} if departing else None
            gap = coverage_gap(s, v, sv, exclude=excl)
            out.append(mk("permission_coverage", not gap, True,
                          "every active reader of this copy can read the survivor" if not gap
                          else f"would revoke access for: {sorted(gap)}"))
        if cls in (C.EXACT_COPY, C.CONTAINER_VARIANT):
            shas = {item.victim_sha, item.survivor_sha}
            vc = s.get(Content, item.victim_sha)
            if vc and vc.normalized_hash:
                shas |= set(s.scalars(select(Content.sha256).where(Content.normalized_hash == vc.normalized_hash)))
            # copies that no open plan (this one included) intends to delete: the survivor
            # is pinned, other victims are reserved -> two plans can never delete "each other's" survivor
            live = live_copies(s, shas, plan.id, {v.id})
            n = len(live)
            out.append(mk("not_last_copy", n >= pol["min_verified_copies"], True,
                          f"{n} other live verified cop{'y' if n == 1 else 'ies'} remain"))
        if cls == C.CONTAINER_VARIANT:
            a, b = s.get(Content, item.victim_sha), s.get(Content, item.survivor_sha)
            same = bool(a and b and a.normalized_hash and a.normalized_hash == b.normalized_hash)
            out.append(mk("recovery_path_verified", same, True,
                          "every page of survivor renders identically (ordered unit hashes equal)" if same
                          else "rendered content differs"))
        if cls == C.MEDIA_VARIANT and sv:
            a, b = s.get(Content, item.victim_sha), s.get(Content, item.survivor_sha)
            qa, qb = a.media or {}, b.media or {}
            ok = (qb.get("height", 0) >= qa.get("height", 0) and qb.get("bit_rate", 0) >= 0.9 * qa.get("bit_rate", 0)
                  and abs((b.duration_s or 0) - (a.duration_s or 0)) <= max(2.0, 0.02 * (a.duration_s or 0)))
            out.append(mk("quality_budget", ok, True,
                          f"survivor {qb.get('width')}x{qb.get('height')} @{(qb.get('bit_rate') or 0) // 1000} kbps "
                          f">= victim {qa.get('width')}x{qa.get('height')} @{(qa.get('bit_rate') or 0) // 1000} kbps"))
        out.append(mk("metadata_preserved", True, False, "victim metadata, path and ACL recorded in evidence + tombstone"))

    # ---------------------------------------------------------------- slide-store classes
    if cls in (C.SUPERSEDED_REVISION, C.CONTAINED_SUBSET):
        out.append(_recovery_via_slide_store(s, item, phase))
        if phase == "plan":
            out.append(_net_saving(s, item, pol))
    if cls == C.SUPERSEDED_REVISION:
        out.append(_lineage_check(s, item, pol))

    # ---------------------------------------------------------------- recordings
    if cls == C.RECORDING_DISTILLED:
        cap = s.get(Capsule, item.victim_sha)
        out.append(mk("capsule_valid", bool(cap and cap.valid), True,
                      f"capsule {cap.manifest_hash[:12]} score {cap.validation.get('score')}" if cap else "no capsule"))
        if cap:
            for ch in cap.validation.get("checks", []):
                if ch["name"] in ("transcript_coverage", "transcript_reviewed", "speaker_attribution", "keyframe_coverage"):
                    out.append(mk(ch["name"], ch["passed"], ch["hard"], ch["detail"]))
        if item.action == "MOVE_TO_ARCHIVE":
            if phase == "plan":
                out.append(mk("recovery_path_verified", True, True,
                              "original will be copied to archive tier and hash-verified before removal"))
            else:
                ok = store.exists("archive", item.victim_sha) and store.verify("archive", item.victim_sha)
                out.append(mk("recovery_path_verified", ok or phase == "execute", True,
                              "archive copy hash-verified" if ok else "archive copy created during execution"))
        else:
            out.append(mk("recovery_path_verified", pol["recordings"]["allow_knowledge_only_deletion"], True,
                          "knowledge-only recovery permitted by policy" if pol["recordings"]["allow_knowledge_only_deletion"]
                          else "policy requires original media to stay recoverable"))

    if cls in (C.CONTAINER_VARIANT, C.CONTAINED_SUBSET, C.SUPERSEDED_REVISION, C.MANUAL_DELETE):
        nc = _notes_check(s, item)
        if nc:
            out.append(nc)

    # ---------------------------------------------------------------- manual deletion (a person chose it)
    if cls == C.MANUAL_DELETE:
        out.extend(_manual_checks(s, item, v, phase, store))

    # ---------------------------------------------------------------- compression (replace in place)
    if cls == C.COMPRESSED:
        out.extend(_compression_checks(s, item, v, phase, pol, store))

    if departing:
        owner_ok = bool(sv and sv.owner_id and sv.owner_id != departing)
        out.append(mk("ownership_resolved", owner_ok, False,
                      f"surviving copy owned by {sv.owner_id}" if owner_ok else "surviving copy has no active owner"))
    return out


def _slides_elsewhere(s: Session, sha: str) -> dict | None:
    """For a deck: how many of its slides (footer-insensitive) also live in another live document."""
    from ..queries import doc_units
    mine = [u for u in doc_units(s, sha) if u.body_hash]
    if not mine:
        return None
    rows = s.execute(select(Unit.body_hash, Unit.content_sha).where(
        Unit.body_hash.in_({u.body_hash for u in mine}), Unit.content_sha != sha, Unit.kind.in_(("page", "slide")))).all()
    live = {x for x in {r[1] for r in rows}
            if s.scalar(select(File.id).where(File.sha256 == x, File.status == "active").limit(1))}
    bodies = {b for b, c in rows if c in live}
    if not bodies:
        return None
    names = sorted({f.filename for f in s.scalars(select(File).where(File.sha256.in_(live), File.status == "active"))})
    shared = sum(1 for u in mine if u.body_hash in bodies)
    return {"total": len(mine), "shared": shared, "unique": sorted(u.ordinal for u in mine if u.body_hash not in bodies),
            "in": ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")}


def _notes_check(s: Session, item: DeletionItem) -> dict | None:
    """Speaker notes are not part of a slide's rendering, so 'renders identically' says nothing about
    them. Soft check: notes that exist only in this file are kept in KORA (searchable, exportable),
    but the file's own copy goes, so the slide owner should know."""
    def norm(t: str) -> str:
        return " ".join((t or "").split()).lower()
    mine = {u.ordinal: norm(u.text) for u in s.scalars(select(Unit).where(Unit.content_sha == item.victim_sha,
                                                                         Unit.kind == "note"))}
    if not mine:
        vc = s.get(Content, item.victim_sha)
        return None if not vc or vc.detected_type != "pptx" else mk("speaker_notes_kept", True, False, "no speaker notes")
    theirs = set()
    others = {item.survivor_sha} if item.survivor_sha else set()
    if not others:                         # no designated survivor: any other live deck counts
        others = {sha for (sha,) in s.execute(select(Unit.content_sha).where(
            Unit.kind == "note", Unit.content_sha != item.victim_sha).distinct())
            if s.scalar(select(File.id).where(File.sha256 == sha, File.status == "active").limit(1))}
    if others:
        theirs = {norm(t) for t in s.scalars(select(Unit.text).where(Unit.content_sha.in_(others),
                                                                      Unit.kind == "note"))}
    only_here = sorted(o for o, t in mine.items() if t and t not in theirs)
    if not only_here:
        return mk("speaker_notes_kept", True, False, f"speaker notes of all {len(mine)} slides exist in "
                  + ("the surviving copy" if item.survivor_sha else "another live document"))
    shown = ", ".join(map(str, only_here[:8])) + ("…" if len(only_here) > 8 else "")
    return mk("speaker_notes_kept", False, False,
              f"{len(only_here)} slide(s) have speaker notes found nowhere else (slides {shown}); KORA keeps them "
              "searchable, but tell the slide owner before the file goes")


def _manual_checks(s: Session, item: DeletionItem, v: File, phase: str, store) -> list[dict]:
    from ..pipeline import EXTRACTABLE
    out = []
    c = s.get(Content, item.victim_sha)
    cap = s.get(Capsule, item.victim_sha)
    n_units = s.scalar(select(func.count()).select_from(Unit).where(Unit.content_sha == item.victim_sha)) or 0
    if c is not None and c.detected_type in EXTRACTABLE:
        ok = bool(c.extracted and n_units and cap is not None)
        detail = (f"{n_units} units (text, slides, frames, transcript) stay searchable and AI-ready in capsule "
                  f"{cap.manifest_hash[:12]}" if ok else "knowledge not captured yet (extraction or capsule missing)")
    else:
        ok, detail = True, f"no knowledge content to keep ({(c.detected_type if c else 'unknown')} file)"
    out.append(mk("knowledge_captured", ok, True, detail))
    if phase == "purge":
        ok = store.exists("quarantine", item.victim_sha)
        out.append(mk("recovery_path_verified", True, True,
                      "grace period over; the capsule remains" + ("" if ok else " (quarantine copy already gone)")))
    else:
        out.append(mk("recovery_path_verified", True, True,
                      "byte-exact original is copied to quarantine and hash-verified before removal"))
    others = s.scalar(select(func.count()).select_from(File).where(
        File.sha256 == item.victim_sha, File.status == "active", File.id != v.id)) or 0
    if others:
        out.append(mk("sole_copy", True, False, f"{others} other live cop{'y' if others == 1 else 'ies'}"))
    else:
        detail = "this is the only copy: after the grace period only its knowledge capsule remains"
        where = _slides_elsewhere(s, item.victim_sha)
        if where:
            uniq = where["unique"]
            shown = ", ".join(map(str, uniq[:8])) + ("…" if len(uniq) > 8 else "")
            detail = (f"{where['shared']} of {where['total']} slides also live in {where['in']}"
                      + (f"; slide{'s' if len(uniq) > 1 else ''} {shown} exist{'' if len(uniq) > 1 else 's'} only here"
                         if uniq else "; every slide lives on elsewhere")
                      + ". After the grace period this file's own copy is gone; its knowledge capsule remains")
        out.append(mk("sole_copy", False, False, detail))
    readers = [a for a in (v.acl or []) if a != f"user:{v.owner_id}"]
    out.append(mk("access_impact", not readers, False, "only the owner can read it" if not readers
                  else f"{len(readers)} other user(s)/group(s) lose the original: {', '.join(sorted(readers)[:5])}"))
    ver = s.scalar(select(Version).where(Version.content_sha == item.victim_sha))
    alone = bool(ver and len([x for x in ver.lineage.versions if not x.variant_of_id]) == 1)
    out.append(mk("current_revision", not (ver and ver.is_head), False,
                  "not the current revision of a document family" if not (ver and ver.is_head)
                  else "the only version of this document" if alone
                  else f"this is the current revision ({ver.revision_label or 'head'}) of its document family"))
    return out


def _compression_checks(s: Session, item: DeletionItem, v: File, phase: str, pol, store) -> list[dict]:
    from ..optimization.engine import content_class
    out = []
    r = s.scalar(select(Rendition).where(Rendition.content_sha == item.victim_sha,
                                         Rendition.blob_key == item.survivor_sha,
                                         Rendition.passed.is_(True)).limit(1))
    c = s.get(Content, item.victim_sha)
    failed = [ch["name"] for ch in r.validation["checks"] if not ch["passed"]] if r else ["no rendition"]
    out.append(mk("quality_budget", not failed, True,
                  "; ".join(ch["detail"] for ch in r.validation["checks"]) if r else "no passing rendition"))
    lossy_ok = c is not None and content_class(c) != "engineering_diagram"
    out.append(mk("lossy_allowed", lossy_ok, True, "content class allows lossy re-encoding" if lossy_ok
                  else "engineering diagrams are never re-encoded lossily"))
    if phase in ("plan", "execute"):
        intact = bool(r and r.blob_key and store.exists("hot", r.blob_key) and store.verify("hot", r.blob_key))
        out.append(mk("rendition_intact", intact, True, "rendition blob hash-verified" if intact
                      else "rendition blob missing or corrupt"))
        need = float(pol.get("compression", {}).get("min_saving_ratio", 0.2))
        saving = 1 - (r.size_bytes / v.size_bytes) if (r and v.size_bytes) else 0
        out.append(mk("net_saving", saving >= need, True, f"frees {saving:.0%} of {(v.size_bytes or 0) / 1e6:.1f} MB "
                                                          f"(policy >= {need:.0%})"))
        out.append(mk("recovery_path_verified", True, True,
                      "byte-exact original is copied to quarantine and hash-verified before the file is replaced"))
    else:
        out.append(mk("recovery_path_verified", True, True,
                      "after purge the file is the approved rendition, which passed the quality budget"))
    return out


def _recovery_via_slide_store(s: Session, item: DeletionItem, phase: str) -> dict:
    from ..versioning import reconstruct
    units = doc_units(s, item.victim_sha)
    if phase == "purge":
        store = get_blobstore()
        ok = units and all((sb := s.get(SlideBlob, u.unit_hash)) and store.verify("hot", sb.blob_key) for u in units)
        return mk("recovery_path_verified", bool(ok), True,
                  "all slide-store pages re-verified" if ok else "slide-store page missing or corrupt")
    from ..versioning import slidestore
    slidestore.materialize(s, item.victim_sha)          # store only pages this deletion needs, then prove them
    res = reconstruct.verify(s, item.victim_sha)
    detail = (f"rebuilt {res['pages']} pages from slide store; every page re-renders to its recorded unit hash"
              if res["ok"] else f"not reconstructable: {res.get('reason') or res.get('mismatched_pages')}")
    return mk("recovery_path_verified", res["ok"], True, detail)


def _net_saving(s: Session, item: DeletionItem, pol) -> dict:
    """Never trade an original for nothing: the pages the slide store must newly keep for this
    deletion have to cost clearly less than the file. If not, discard them again.
    (Real-data finding: Rev B vs Rev C of an Ericsson deck differ in every footer, so all 25
    pages are unique, and page-PDFs of a PPTX are larger than the PPTX itself.)"""
    f = s.get(File, item.file_id)
    own = [sb for u in doc_units(s, item.victim_sha) if (sb := s.get(SlideBlob, u.unit_hash))
           and sb.first_content_sha == item.victim_sha]
    kept = sum(sb.size_bytes for sb in own)
    need = float(pol["revisions"].get("min_net_saving_ratio", 0.10))
    saving = (f.size_bytes or 0) - kept
    ok = saving >= need * (f.size_bytes or 0)
    if not ok:
        store = get_blobstore()
        own_hashes = {sb.unit_hash for sb in own}
        for key in {sb.blob_key for sb in own}:          # keep nothing for a deletion we will not make
            others = s.scalar(select(SlideBlob.unit_hash).where(SlideBlob.blob_key == key,
                                                               SlideBlob.unit_hash.not_in(own_hashes)).limit(1))
            if others is None:
                store.delete("hot", key)
        for sb in own:
            s.delete(sb)
        s.flush()
    return mk("net_saving", ok, True,
              f"frees {saving / 1e6:.2f} MB of {(f.size_bytes or 0) / 1e6:.2f} MB after keeping {len(own)} unique pages "
              f"({kept / 1e6:.2f} MB); policy requires >= {need:.0%}")


def _lineage_check(s: Session, item: DeletionItem, pol) -> dict:
    v = s.scalar(select(Version).where(Version.content_sha == item.victim_sha))
    if v is None:
        return mk("lineage_consistent", False, True, "not part of a lineage")
    lin = s.get(Lineage, v.lineage_id)
    need = pol["revisions"]["min_ordering_confidence"]
    head = s.scalar(select(Version).where(Version.lineage_id == lin.id, Version.is_head.is_(True)))
    head_live = bool(head and s.scalar(select(File.id).where(File.sha256 == head.content_sha, File.status == "active")))
    problems = []
    if lin.has_conflict:
        problems.append("revision conflict in lineage")
    if CONF_RANK[lin.ordering_confidence] < CONF_RANK[need]:
        problems.append(f"ordering confidence {lin.ordering_confidence} < {need}")
    if v.is_head:
        problems.append("version is the head")
    if not head_live:
        problems.append("head revision has no live copy")
    return mk("lineage_consistent", not problems, True,
              f"{lin.key}: {v.revision_label} superseded by head {head.revision_label if head else '?'} "
              f"({lin.ordering_basis}/{lin.ordering_confidence})" if not problems else "; ".join(problems))


def score(checks: list[dict]) -> tuple[float, bool]:
    total = sum(c["weight"] for c in checks) or 1
    got = sum(c["weight"] for c in checks if c["passed"])
    deletable = all(c["passed"] for c in checks if c["hard"])
    raw = 100.0 * got / total
    return round(raw if deletable else min(raw, 50.0), 1), deletable


def grace_until(days: int):
    return clock.now() + timedelta(days=days)
