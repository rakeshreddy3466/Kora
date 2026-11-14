"""Knowledge Continuity Assessment for a departing employee.

The challenge rule - "delete only if a duplicate already exists" - becomes:
a departing owner's file is a deletion candidate only if an equivalent copy
(identical bytes or render-identical) survives in an *approved repository*
under an *active* owner, every active reader keeps access, and no hold or
retention rule applies. Everything else is triaged so knowledge is not lost:
unique documents and recordings get a proposed new owner, open action items
assigned to the person are surfaced, and lineages they own the head of are
handed over.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..dedup import classes as C
from ..knowledge import facts as factx
from ..models import Capsule, Content, File, Principal, Relation, Version
from ..queries import files_of
from ..security.auth import Identity
from . import planner


def _proposed_owner(s: Session, p: Principal) -> str | None:
    if p.manager_id and (m := s.get(Principal, p.manager_id)) and m.status == "active":
        return m.id
    for other in s.scalars(select(Principal).where(Principal.team == p.team, Principal.id != p.id,
                                                   Principal.status == "active")):
        return other.id
    return None


def assess(s: Session, ident: Identity, principal_id: str, create_plan: bool = True) -> dict:
    if not ident.has("analyst"):
        raise PermissionError("analyst role required")
    p = s.get(Principal, principal_id)
    if p is None:
        raise ValueError("unknown principal")
    owned = list(s.scalars(select(File).where(File.owner_id == principal_id, File.status == "active")))
    plan = planner.create(s, ident, "departure", {"owner": principal_id},
                          title=f"Departure of {p.display_name}") if create_plan and p.status != "active" else None
    by_file = {it.file_id: it for it in (plan.items if plan else [])}
    new_owner = _proposed_owner(s, p)

    buckets = defaultdict(list)
    for f in owned:
        it = by_file.get(f.id)
        rel_kinds = set(s.scalars(select(Relation.kind).where(or_(Relation.a_sha == f.sha256, Relation.b_sha == f.sha256))))
        entry = {"file_id": f.id, "path": f"{f.system}:{f.path}", "size_bytes": f.size_bytes, "type": f.detected_type}
        if it is not None:
            entry["class"] = it.dup_class
            entry["survivor"] = it.survivor_file_id
            if it.deletable:
                buckets["deletable_duplicates"].append(entry)
            else:
                entry["blocked_by"] = [c["name"] + ": " + c["detail"] for c in it.checks if c["hard"] and not c["passed"]]
                if any(c["name"] in ("legal_hold_clear", "retention_satisfied") and not c["passed"] for c in it.checks):
                    buckets["blocked_by_retention"].append(entry)
                else:
                    buckets["blocked_other"].append(entry)
            continue
        entry["proposed_owner"] = new_owner
        if f.detected_type == "video":
            cap = s.get(Capsule, f.sha256)
            entry["capsule_valid"] = bool(cap and cap.valid)
            entry["note"] = ("knowledge capsule valid" if cap and cap.valid
                             else "recording has no usable transcript: knowledge at risk")
            if not rel_kinds & {C.REL_MEDIA}:
                buckets["unique_recordings"].append(entry)
            else:
                buckets["shared_recordings"].append(entry)
        elif rel_kinds & {C.REL_PARTIAL, C.REL_SUBSET, C.REL_NEAR, C.REL_KNOWLEDGE}:
            entry["relations"] = sorted(rel_kinds)
            buckets["partially_shared"].append(entry)
        elif s.scalar(select(Version.id).where(Version.content_sha == f.sha256)) and \
                len(s.scalar(select(Version).where(Version.content_sha == f.sha256)).lineage.versions) > 1:
            v = s.scalar(select(Version).where(Version.content_sha == f.sha256))
            entry["lineage"] = v.lineage.key
            entry["revision"] = v.revision_label
            entry["is_head"] = v.is_head
            buckets["versioned_documents"].append(entry)
        else:
            buckets["unique_documents"].append(entry)

    # open actions / decisions that mention the person anywhere in the corpus,
    # grouped so the same slide copied into five decks is one dependency with five sources
    grouped: dict[str, dict] = {}
    for c in s.scalars(select(Content).where(Content.extracted.is_(True))):
        for fact in factx.extract(s, c.sha256):
            if principal_id in fact["persons"] and fact["type"] in ("action_item", "decision"):
                key = " ".join(fact["text"].lower().split())
                fs = files_of(s, c.sha256)
                src = {"content": c.sha256, "name": fs[0].filename if fs else c.sha256[:12],
                       "unit": fact["provenance"]["unit_kind"], "ordinal": fact["provenance"]["ordinal"],
                       "start_s": fact["provenance"]["start_s"], "speaker": fact["provenance"]["speaker"]}
                g = grouped.setdefault(key, {"type": fact["type"], "text": fact["text"], "sources": []})
                g["sources"].append(src)
    deps = sorted(grouped.values(), key=lambda g: (g["type"], g["text"]))
    from ..knowledge.notes import slides_of
    named = [p.display_name.split(" (")[0]] if p.display_name and not p.display_name.startswith("person-") else []
    slide_owner = []
    for d in slides_of(s, named):
        fs = files_of(s, d["content"])
        slide_owner.append({"content": d["content"], "name": fs[0].filename if fs else d["content"][:12],
                            "slides": sorted(d["slides"]), "proposed_owner": new_owner})
    heads = [v for v in s.scalars(select(Version).where(Version.is_head.is_(True)))
             if len(v.lineage.versions) > 1 and any(f.owner_id == principal_id for f in files_of(s, v.content_sha))]
    unresolved = [e for k in ("unique_documents", "unique_recordings", "versioned_documents", "partially_shared")
                  for e in buckets[k] if not e.get("proposed_owner")]
    summary = {
        "principal": principal_id, "display_name": p.display_name, "status": p.status,
        "files_owned": len(owned), "bytes_owned": sum(f.size_bytes for f in owned),
        "deletable_duplicates": len(buckets["deletable_duplicates"]),
        "deletable_bytes": sum(e["size_bytes"] for e in buckets["deletable_duplicates"]),
        "blocked_by_retention": len(buckets["blocked_by_retention"]),
        "blocked_other": len(buckets["blocked_other"]),
        "partially_shared": len(buckets["partially_shared"]),
        "unique_documents": len(buckets["unique_documents"]),
        "versioned_documents": len(buckets["versioned_documents"]),
        "unique_recordings": len(buckets["unique_recordings"]),
        "open_actions_and_decisions": len(deps),
        "slides_naming_them_responsible": sum(len(d["slides"]) for d in slide_owner),
        "lineage_heads_owned": len(heads),
        "unresolved_ownership": len(unresolved),
        "proposed_new_owner": new_owner,
    }
    report = {"summary": summary, "buckets": dict(buckets), "dependencies": deps, "slide_ownership": slide_owner,
              "lineage_heads": [{"lineage": v.lineage.key, "revision": v.revision_label} for v in heads],
              "plan_id": plan.id if plan else None}
    audit.append(s, ident.principal_id, "departure.assessed", "principal", principal_id,
                 {k: v for k, v in summary.items() if k not in ("display_name",)})   # no names in the immutable log
    return report
