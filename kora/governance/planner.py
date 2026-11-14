"""Deletion planner: turns the relationship graph into evidence-carrying plans.

Plan kinds
  duplicates   EXACT_COPY + CONTAINER_VARIANT clusters (union-find over
               identical bytes and render-identical contents)
  revisions    SUPERSEDED_REVISION per lineage retention rule
  subsets      CONTAINED_SUBSET (excerpts whose every slide lives in a larger deck)
  media        MEDIA_VARIANT clusters (same recording, lower quality copies)
  recordings   RECORDING_DISTILLED (original -> archive tier once capsule validated)
  departure    duplicates restricted to a departing owner's files whose
               surviving copy belongs to someone active
A plan is always created as a *draft dry run*: nothing changes at the source
until it is approved by the required number of approvers and executed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, clock
from ..capsule import builder as capsule_builder
from ..dedup import classes as C
from ..models import (Content, DeletionItem, DeletionPlan, File, Lineage, Principal, Relation, Rendition, SlideBlob, Source,
                      Version)
from ..queries import doc_units, files_of
from ..security.auth import Identity
from ..util import canonical_json, sha256_bytes
from . import checks, survivor
from .policy import get_policy

KINDS = ("duplicates", "revisions", "subsets", "media", "recordings", "compress", "manual", "departure")


class PlanError(Exception):
    pass


@dataclass
class Spec:
    victim: File
    survivor: File | None
    cls: str
    reasons: list[str] = field(default_factory=list)
    note: str = ""
    rendition: object | None = None          # COMPRESSED: the Rendition that replaces the bytes in place


# --------------------------------------------------------------------------- candidate builders
def _scope_ok(f: File, scope: dict) -> bool:
    if scope.get("sources") and f.source_name not in scope["sources"]:
        return False
    if scope.get("owner") and f.owner_id != scope["owner"]:
        return False
    if scope.get("file_ids") and f.id not in scope["file_ids"]:
        return False
    return True


def _clusters(s: Session) -> list[list[File]]:
    files = [f for f in s.scalars(select(File).where(File.status == "active", File.sha256.is_not(None)))]
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)
    by_sha = defaultdict(list)
    for f in files:
        by_sha[f.sha256].append(f)
        find(f.sha256)
    for r in s.scalars(select(Relation).where(Relation.kind == C.REL_CONTAINER)):
        if r.a_sha in by_sha and r.b_sha in by_sha:
            union(r.a_sha, r.b_sha)
    groups = defaultdict(list)
    for sha, fs in by_sha.items():
        groups[find(sha)].extend(fs)
    return [g for g in groups.values() if len(g) > 1]


def _duplicates(s: Session, scope: dict) -> list[Spec]:
    specs = []
    for cluster in _clusters(s):
        sv, reasons = survivor.choose(s, cluster)
        if sv is None:
            continue
        for f in cluster:
            if f.id == sv.id or not _scope_ok(f, scope):
                continue
            cls = C.EXACT_COPY if f.sha256 == sv.sha256 else C.CONTAINER_VARIANT
            specs.append(Spec(f, sv, cls, reasons))
    return specs


def _keep_set(lin: Lineage, reps: list[Version], pol) -> set[str]:
    keep = {reps[-1].id}
    rp = pol["revisions"]
    n_prev = int(rp.get("keep_last_n_unreleased", 1))
    if all(v.revision_scheme in ("ericsson", "rstate") for v in reps):
        if rp.get("keep", "head_and_released") == "head_and_released":
            keep |= {v.id for v in reps if v.released}
        last_rel = max((v.ordinal for v in reps if v.released), default=0)
        # preliminaries after the last release are work in progress: keep the most recent n
        wip = [v for v in reps if not v.released and v.ordinal > last_rel and not v.is_head]
        keep |= {v.id for v in wip[-n_prev:]} if n_prev else set()
    else:
        keep |= {v.id for v in reps[:-1][-n_prev:]} if n_prev else set()
    return keep


def _revisions(s: Session, scope: dict) -> list[Spec]:
    pol = get_policy()
    specs = []
    q = select(Lineage)
    if scope.get("lineage_ids"):
        q = q.where(Lineage.id.in_(scope["lineage_ids"]))
    for lin in s.scalars(q):
        reps = sorted((v for v in lin.versions if not v.variant_of_id), key=lambda v: v.ordinal)
        if len(reps) < 2:
            continue
        keep = _keep_set(lin, reps, pol)
        head_files = files_of(s, reps[-1].content_sha)
        head_sv, _ = survivor.choose(s, head_files) if head_files else (None, [])
        for v in reps:
            if v.id in keep:
                continue
            for f in files_of(s, v.content_sha):
                if _scope_ok(f, scope):
                    specs.append(Spec(f, head_sv, C.SUPERSEDED_REVISION,
                                      [f"{v.revision_label or 'unlabelled'} superseded; head is {reps[-1].revision_label}"],
                                      note=v.diff_from_parent.get("summary", "") if v.diff_from_parent else ""))
    return specs


def _subsets(s: Session, scope: dict) -> list[Spec]:
    specs = []
    by_subset = defaultdict(list)
    for r in s.scalars(select(Relation).where(Relation.kind == C.REL_SUBSET)):
        by_subset[r.a_sha].append(r)
    for sub_sha, rels in by_subset.items():
        if s.scalar(select(Version.id).where(Version.content_sha == sub_sha)):
            continue                                            # versioned -> revisions planner decides
        cands = []
        for r in rels:
            cands += [(f, bool((r.details or {}).get("exact"))) for f in files_of(s, r.b_sha)]
        if not cands:
            continue
        exact = {f.id for f, e in cands if e}
        sv, reasons = survivor.choose(s, [f for f, _ in cands], extra_key=lambda f: f.id in exact)
        if sv is None:
            continue
        reasons = [x for x in reasons if x != "highest quality rendition"]
        reasons.insert(0, "contains every slide byte-identically" if sv.id in exact else "contains every slide body")
        for f in files_of(s, sub_sha):
            if _scope_ok(f, scope):
                specs.append(Spec(f, sv, C.CONTAINED_SUBSET, reasons))
    return specs


def _media(s: Session, scope: dict) -> list[Spec]:
    specs = []
    adj = defaultdict(set)
    for r in s.scalars(select(Relation).where(Relation.kind == C.REL_MEDIA)):
        adj[r.a_sha].add(r.b_sha)
        adj[r.b_sha].add(r.a_sha)
    seen = set()
    for start in list(adj):
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(adj[x] - comp)
        seen |= comp
        files = [f for sha in comp for f in files_of(s, sha)]
        quality = {sha: ((s.get(Content, sha).media or {}).get("height", 0),
                         (s.get(Content, sha).media or {}).get("bit_rate", 0)) for sha in comp}
        sv, reasons = survivor.choose(s, files, extra_key=lambda f: quality[f.sha256])
        if sv is None:
            continue
        for f in files:
            if f.id != sv.id and _scope_ok(f, scope):
                specs.append(Spec(f, sv, C.MEDIA_VARIANT if f.sha256 != sv.sha256 else C.EXACT_COPY, reasons))
    return specs


def _recordings(s: Session, scope: dict) -> list[Spec]:
    specs = []
    for c in s.scalars(select(Content).where(Content.detected_type == "video", Content.extracted.is_(True))):
        capsule_builder.build(s, c.sha256)                       # always plan against a fresh capsule
        for f in files_of(s, c.sha256):
            if _scope_ok(f, scope):
                specs.append(Spec(f, None, C.RECORDING_DISTILLED, ["validated knowledge capsule replaces hot copy"]))
    return specs


def _compress(s: Session, scope: dict) -> list[Spec]:
    """Files whose content has a rendition that passed its measured quality budget.
    Run this after the dedup/revision plans: compress what survives, never what is about to go."""
    from ..optimization import engine as opt
    specs = []
    shas = sorted(set(s.scalars(select(Rendition.content_sha).where(Rendition.passed.is_(True)))))
    for sha in shas:
        r = opt.best(s, sha)
        if r is None:
            continue
        v = r.validation
        why = [f"{r.operation}: {v['original_bytes'] / 1e6:.1f} MB -> {r.size_bytes / 1e6:.1f} MB "
               f"({v['saving_ratio']:.0%} smaller)"] + [c["detail"] for c in v["checks"]]
        for f in files_of(s, sha):
            if _scope_ok(f, scope):
                specs.append(Spec(f, None, C.COMPRESSED, why, rendition=r))
    return specs


def _manual(s: Session, scope: dict) -> list[Spec]:
    """Files a person selected. For each, KORA first looks for the reason a machine would have
    given (another copy, a newer revision, a containing deck, a better recording) because that
    reason carries a stronger recovery path. Only if none exists is it a MANUAL_DELETE, whose
    recovery is the knowledge capsule."""
    ids = list(dict.fromkeys(scope.get("file_ids") or []))
    selected = set(ids)
    auto: dict[str, Spec] = {}
    for builder in (_duplicates, _media, _subsets, _revisions):
        for sp in builder(s, {"file_ids": ids}):
            if sp.survivor is not None and sp.survivor.id in selected:
                continue                                    # its survivor is being deleted too
            auto.setdefault(sp.victim.id, sp)
    specs = []
    for fid in ids:
        f = s.get(File, fid)
        if f is None or f.status != "active" or not f.sha256:
            continue
        if fid in auto:
            sp = auto[fid]
            sp.reasons = ["selected manually"] + list(sp.reasons)
            specs.append(sp)
            continue
        others = [o for o in files_of(s, f.sha256, ("active",)) if o.id not in selected]
        if others:
            sv, reasons = survivor.choose(s, others)
            if sv is not None:
                specs.append(Spec(f, sv, C.EXACT_COPY, ["selected manually", "identical copy stays"] + reasons))
                continue
        specs.append(Spec(f, None, C.MANUAL_DELETE,
                          ["selected manually; no other copy, newer revision or containing document exists"]))
    return specs


# --------------------------------------------------------------------------- plan assembly
def _bytes_reclaimable(s: Session, spec: Spec, action: str) -> int:
    size = spec.victim.size_bytes or 0
    if spec.cls == C.COMPRESSED:
        return max(0, size - spec.rendition.size_bytes)
    if spec.cls in (C.SUPERSEDED_REVISION, C.CONTAINED_SUBSET):
        # slide-store pages first introduced by this content are the only extra bytes KORA keeps
        retained = sum(sb.size_bytes for u in doc_units(s, spec.victim.sha256)
                       if (sb := s.get(SlideBlob, u.unit_hash)) and sb.first_content_sha == spec.victim.sha256)
        return max(0, size - retained)
    return size                                                   # archive moves still free hot storage


def create(s: Session, ident: Identity, kind: str, scope: dict | None = None, title: str | None = None) -> DeletionPlan:
    if kind not in KINDS:
        raise PlanError(f"unknown plan kind {kind}")
    scope = dict(scope or {})
    pol = get_policy()
    if kind == "manual":
        ids = scope.get("file_ids") or []
        if not ids or not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise PlanError("manual plans need scope.file_ids (the files a person selected)")
        if len(ids) > int(pol.get("manual", {}).get("max_files", 500)):
            raise PlanError("too many files in one manual plan")
        owns_all = all((f := s.get(File, x)) is not None and f.owner_id == ident.principal_id for x in ids)
        if not (ident.has("analyst") or (owns_all and pol.get("manual", {}).get("owners_may_request", True))):
            raise PermissionError("analyst role required, or the requester must own every selected file")
    elif not ident.has("analyst"):
        raise PermissionError("analyst role required")
    departing = None
    if kind == "departure":
        departing = scope.get("owner")
        p = s.get(Principal, departing) if departing else None
        if p is None:
            raise PlanError("departure plans need scope.owner = an existing principal")
        if p.status == "active":
            raise PlanError(f"{p.id} is not marked as departing")
        specs = [sp for sp in _duplicates(s, scope)
                 if sp.survivor and sp.survivor.owner_id != departing
                 and s.get(Source, sp.survivor.source_name).approved_repository]
    else:
        specs = {"duplicates": _duplicates, "revisions": _revisions, "subsets": _subsets, "media": _media,
                 "recordings": _recordings, "compress": _compress, "manual": _manual}[kind](s, scope)

    plan = DeletionPlan(kind=kind, title=title or f"{kind} plan", scope=scope, created_by=ident.principal_id,
                        status="draft", policy_version=pol.version, policy_hash=pol.hash)
    s.add(plan)
    s.flush()
    items = []
    for sp in specs:
        pc = pol.cls(sp.cls)
        action = pc["action"]
        recovery = pc["recovery"]
        if sp.cls == C.RECORDING_DISTILLED and pol["recordings"]["allow_knowledge_only_deletion"]:
            action, recovery = "DELETE", "knowledge_only"
        it = DeletionItem(plan_id=plan.id, file_id=sp.victim.id, survivor_file_id=sp.survivor.id if sp.survivor else None,
                          dup_class=sp.cls, action=action, recovery_class=recovery, victim_sha=sp.victim.sha256,
                          survivor_sha=(sp.rendition.blob_key if sp.cls == C.COMPRESSED
                                        else sp.survivor.sha256 if sp.survivor else None),
                          bytes_reclaimable=_bytes_reclaimable(s, sp, action), state="planned")
        s.add(it)
        items.append((it, sp))
    s.flush()
    for it, sp in items:
        it.checks = checks.evaluate(s, it, plan, "plan", departing)
        it.readiness, it.deletable = checks.score(it.checks)
        it.bytes_reclaimable = _bytes_reclaimable(s, sp, it.action)   # after materialisation: net of pages kept
        if not it.deletable:
            it.state = "blocked"
    s.flush()
    finalize_evidence(s, plan, {it.id: sp for it, sp in items})
    audit.append(s, ident.principal_id, "plan.created", "plan", plan.id,
                 {"kind": kind, "items": len(items), "deletable": sum(1 for it, _ in items if it.deletable),
                  "policy": pol.version, "policy_hash": pol.hash, "evidence_hash": plan.evidence["evidence_hash"]})
    return plan


def _loc(f: File | None) -> dict | None:
    if f is None:
        return None
    return {"file_id": f.id, "system": f.system, "site": f.site, "path": f.path, "owner": f.owner_id,
            "size_bytes": f.size_bytes, "sha256": f.sha256, "acl": f.acl, "retention_label": f.retention_label,
            "legal_hold": f.legal_hold}


def finalize_evidence(s: Session, plan: DeletionPlan, specs: dict | None = None) -> None:
    pol = get_policy()
    specs = specs or {}
    rows = []
    by_class: dict[str, dict] = defaultdict(lambda: {"items": 0, "deletable": 0, "bytes": 0})
    for it in sorted(plan.items, key=lambda i: (i.dup_class, i.file_id)):
        sp = specs.get(it.id)
        v, sv = s.get(File, it.file_id), s.get(File, it.survivor_file_id) if it.survivor_file_id else None
        rows.append({"item_id": it.id, "class": it.dup_class, "action": it.action, "recovery": it.recovery_class,
                     "recovery_meaning": C.RECOVERY.get(it.recovery_class, ""),
                     "victim": _loc(v), "survivor": _loc(sv),
                     "survivor_reasons": sp.reasons if sp else None, "note": sp.note if sp else None,
                     "checks": it.checks, "readiness": it.readiness, "deletable": it.deletable,
                     "bytes_reclaimable": it.bytes_reclaimable})
        b = by_class[it.dup_class]
        b["items"] += 1
        if it.deletable:
            b["deletable"] += 1
            b["bytes"] += it.bytes_reclaimable
    deletable = [it for it in plan.items if it.deletable]
    plan.required_approvals = max([pol.cls(it.dup_class)["approvals"] for it in deletable] or [1])
    w = sum(max(1, it.bytes_reclaimable) for it in plan.items) or 1
    plan.readiness = round(sum(it.readiness * max(1, it.bytes_reclaimable) for it in plan.items) / w, 1) if plan.items else 0
    blocked_reasons: dict[str, int] = defaultdict(int)
    for it in plan.items:
        for ch in it.checks:
            if ch["hard"] and not ch["passed"]:
                blocked_reasons[ch["name"]] += 1
    body = {
        "plan_id": plan.id, "kind": plan.kind, "created_by": plan.created_by,
        "generated_at": clock.now().isoformat(),
        "policy": {"version": pol.version, "hash": pol.hash, "quarantine_days": pol["quarantine_days"]},
        "summary": {"items": len(plan.items), "deletable": len(deletable), "blocked": len(plan.items) - len(deletable),
                    "bytes_reclaimable": sum(it.bytes_reclaimable for it in deletable),
                    "by_class": dict(by_class), "blocked_by": dict(blocked_reasons),
                    "required_approvals": plan.required_approvals, "readiness": plan.readiness},
        "items": rows,
    }
    body["evidence_hash"] = sha256_bytes(canonical_json({k: v for k, v in body.items() if k != "generated_at"}))
    plan.evidence = body
    plan.updated_at = clock.now()
