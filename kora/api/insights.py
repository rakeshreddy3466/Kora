"""Read models for the dashboard: one row per file with everything a person needs to decide
about it, an overview, renditions with their evidence. All ACL-filtered like /v1/files."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..dedup import classes as C
from ..models import (Capsule, Content, DeletionItem, DeletionPlan, File, Lineage, Relation, Rendition, ReviewTask,
                      Source, Version)
from ..knowledge import notes
from ..queries import display_name
from ..reports import savings
from ..security.acl import can_read
from ..security.auth import Identity

OPEN = ("draft", "pending_approval", "approved", "executing", "quarantined")
LIVE_ITEM = ("planned", "quarantining", "quarantined")


def visible(ident: Identity, f: File) -> bool:
    return ident.has("auditor") or can_read(ident.acl_tokens, f.acl)


def inventory(s: Session, ident: Identity) -> list[dict]:
    files = [f for f in s.scalars(select(File).order_by(File.source_name, File.path)) if visible(ident, f)]
    shas = {f.sha256 for f in files if f.sha256}
    copies = dict(s.execute(select(File.sha256, func.count()).where(File.status == "active", File.sha256.in_(shas))
                            .group_by(File.sha256)).all())
    rel: dict[str, list[dict]] = defaultdict(list)
    for r in s.scalars(select(Relation).where(Relation.a_sha.in_(shas) | Relation.b_sha.in_(shas))):
        if r.kind in (C.REL_PRESENTED,):
            continue
        for me, other, role in ((r.a_sha, r.b_sha, "a"), (r.b_sha, r.a_sha, "b")):
            if me in shas:
                rel[me].append({"kind": r.kind, "role": role, "other": display_name(s, other), "score": round(r.score, 3)})
    vers = {v.content_sha: v for v in s.scalars(select(Version).where(Version.content_sha.in_(shas)))}
    lins = {l.id: l for l in s.scalars(select(Lineage).where(Lineage.id.in_({v.lineage_id for v in vers.values()})))}
    caps = {c.content_sha: c.valid for c in s.scalars(select(Capsule).where(Capsule.content_sha.in_(shas)))}
    rends: dict[str, Rendition] = {}
    for r in s.scalars(select(Rendition).where(Rendition.content_sha.in_(shas), Rendition.passed.is_(True))):
        if r.content_sha not in rends or r.size_bytes < rends[r.content_sha].size_bytes:
            rends[r.content_sha] = r
    plans = {}
    for it, p in s.execute(select(DeletionItem, DeletionPlan).join(DeletionPlan, DeletionPlan.id == DeletionItem.plan_id)
                           .where(DeletionPlan.status.in_(OPEN), DeletionItem.state.in_(LIVE_ITEM + ("blocked",)))):
        plans.setdefault(it.file_id, {"plan": p.id, "kind": p.kind, "status": p.status, "class": it.dup_class,
                                      "state": it.state})
    contents = {c.sha256: c for c in s.scalars(select(Content).where(Content.sha256.in_(shas)))}
    out = []
    for f in files:
        v = vers.get(f.sha256)
        lin = lins.get(v.lineage_id) if v else None
        c = contents.get(f.sha256)
        r = rends.get(f.sha256)
        out.append({
            "id": f.id, "source": f.source_name, "system": f.system, "site": f.site, "path": f.path,
            "name": f.filename, "type": f.detected_type, "size": f.size_bytes, "status": f.status,
            "state": f.ingest_state, "owner": f.owner_id, "legal_hold": f.legal_hold, "retention": f.retention_label,
            "sha": f.sha256, "copies": copies.get(f.sha256, 0),
            "relations": rel.get(f.sha256, [])[:8],
            "version": ({"lineage": lin.id, "doc_number": lin.doc_number, "revision": v.revision_label,
                         "is_head": v.is_head, "released": v.released, "variant": v.variant_of_id is not None,
                         "confidence": lin.ordering_confidence} if v and lin else None),
            "capsule": caps.get(f.sha256),
            "rendition_of": (c.meta or {}).get("rendition_of") if c else None,
            "compressible": ({"op": r.operation, "to": r.size_bytes, "saving": r.validation.get("saving_ratio")}
                             if r and r.blob_key and f.status == "active" else None),
            "plan": plans.get(f.id),
            "doc_number": (c.meta or {}).get("doc_number") if c else None,
            "duration_s": c.duration_s if c else None,
            "freshness": ({**fr, "review": ((c.meta or {}).get("freshness_review") or {}).get("decision")}
                          if (fr := notes.freshness(c)) else None),
        })
    return out


_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def overview(s: Session, ident: Identity, refresh: bool = False) -> dict:
    """Savings dry-runs every planner, so it is cached briefly."""
    with _lock:
        hit = _cache.get("savings")
        if refresh or hit is None or time.time() - hit[0] > 60:
            _cache["savings"] = (time.time(), savings(s))
        sv = _cache["savings"][1]
    plans = dict(s.execute(select(DeletionPlan.status, func.count()).group_by(DeletionPlan.status)).all())
    reviews = dict(s.execute(select(ReviewTask.kind, func.count()).where(ReviewTask.status == "open")
                             .group_by(ReviewTask.kind)).all())
    rel = dict(s.execute(select(Relation.kind, func.count()).group_by(Relation.kind)).all())
    groups = s.execute(select(File.sha256, func.count()).where(File.status == "active", File.sha256.is_not(None))
                       .group_by(File.sha256).having(func.count() > 1)).all()
    rel["EXACT_COPY"] = sum(n - 1 for _, n in groups)
    status = dict(s.execute(select(File.status, func.sum(File.size_bytes)).group_by(File.status)).all())
    realized = defaultdict(int)
    for it in s.scalars(select(DeletionItem).where(DeletionItem.state.in_(("quarantined", "purged")))):
        realized[it.dup_class] += it.bytes_reclaimable
    v = audit.verify(s)
    return {"savings": sv["summary"], "potential": sv["potential"], "inventory": sv["inventory"],
            "computed_at": _cache["savings"][0], "plans": plans, "open_reviews": reviews, "relations": rel,
            "bytes_by_status": {k: int(v2 or 0) for k, v2 in status.items()}, "realized_by_class": dict(realized),
            "audit": {"ok": v.ok, "events": v.events},
            "lineages": s.scalar(select(func.count()).select_from(Lineage)) or 0,
            "sources": [{"name": x.name, "system": x.system, "ai_use_allowed": bool((x.config or {}).get("ai_use_allowed"))}
                        for x in s.scalars(select(Source))]}


def renditions(s: Session, ident: Identity) -> list[dict]:
    out = []
    for r in s.scalars(select(Rendition).order_by(Rendition.created_at.desc())):
        files = [f for f in s.scalars(select(File).where(File.sha256 == r.content_sha)) if visible(ident, f)]
        rsha = r.blob_key or (r.validation or {}).get("rendition_sha")
        now = [f for f in s.scalars(select(File).where(File.sha256 == rsha)) if visible(ident, f)] if rsha else []
        if not files and not now:
            continue
        val = r.validation or {}
        name = files[0].filename if files else now[0].filename if now else display_name(s, r.content_sha)
        out.append({"id": r.id, "content": r.content_sha, "name": name, "op": r.operation,
                    "passed": r.passed, "original": val.get("original_bytes"), "size": r.size_bytes,
                    "saving": val.get("saving_ratio"), "checks": val.get("checks", []), "measured": val.get("measured", {}),
                    "previews": val.get("previews", []), "files": [f.id for f in files],
                    "applied": bool(now) or "blob_released_at" in val, "created_at": r.created_at.isoformat()})
    return out


def preview_owners(s: Session, key: str) -> list[str]:
    """Contents whose readers may see this preview: the original and the rendition that replaced it."""
    for r in s.scalars(select(Rendition)):
        for pv in (r.validation or {}).get("previews", []):
            if key in (pv.get("before"), pv.get("after")):
                return [x for x in (r.content_sha, r.blob_key or (r.validation or {}).get("rendition_sha")) if x]
    return []


# --------------------------------------------------------------------------- recordings
def _readable_sha(s: Session, ident: Identity, sha: str) -> bool:
    files = list(s.scalars(select(File).where(File.sha256 == sha)))
    if any(visible(ident, f) for f in files):
        return True
    cap = s.get(Capsule, sha)
    return bool(cap and can_read(ident.acl_tokens, cap.manifest.get("acl_snapshot", [])))


def recordings(s: Session, ident: Identity) -> list[dict]:
    from ..models import Unit
    out = []
    for c in s.scalars(select(Content).where(Content.detected_type == "video", Content.extracted.is_(True))):
        if not _readable_sha(s, ident, c.sha256):
            continue
        files = list(s.scalars(select(File).where(File.sha256 == c.sha256)))
        if not files or all(f.status != "active" for f in files) and (c.meta or {}).get("rendition_of") is None \
                and not s.get(Capsule, c.sha256):
            continue
        if (c.meta or {}).get("rendition_of"):
            continue                                    # shown under its original
        cap = s.get(Capsule, c.sha256)
        arts = cap.manifest.get("artifacts", []) if cap else []
        audio = sum(a.get("size", 0) for a in arts if a.get("role") == "audio")
        n_frames = s.scalar(select(func.count()).select_from(Unit).where(Unit.content_sha == c.sha256, Unit.kind == "frame"))
        n_segs = s.scalar(select(func.count()).select_from(Unit).where(Unit.content_sha == c.sha256, Unit.kind == "segment"))
        docs = [{"name": display_name(s, r.a_sha), "content": r.a_sha, "score": r.score}
                for r in s.scalars(select(Relation).where(Relation.kind == C.REL_PRESENTED, Relation.b_sha == c.sha256))]
        spk = sorted({u for u in s.scalars(select(Unit.speaker).where(Unit.content_sha == c.sha256, Unit.kind == "segment",
                                                                         Unit.speaker.is_not(None)).distinct())})
        out.append({"content": c.sha256, "name": next((f.filename for f in files), c.sha256[:12]),
                    "duration_s": c.duration_s, "size": c.size_bytes,
                    "status": next((f.status for f in files if f.status == "active"), files[0].status if files else "gone"),
                    "capsule": ({"valid": cap.valid, "bytes": cap.size_bytes, "audio_bytes": audio,
                                 "failed": [ch["name"] for ch in (cap.validation or {}).get("checks", [])
                                            if ch.get("hard") and not ch.get("passed")]} if cap else None),
                    "transcript": {"source": (c.media or {}).get("transcript_source"),
                                   "review": ((c.meta or {}).get("transcript_review") or {}).get("decision"),
                                   "engine": ((c.media or {}).get("asr") or {}).get("engine"),
                                   "languages": ((c.media or {}).get("asr") or {}).get("languages"),
                                   "segments": n_segs, "speakers": spk},
                    "frames": n_frames, "documents": docs, "rebuilt": (c.media or {}).get("rebuilt"),
                    "width": (c.media or {}).get("width"), "height": (c.media or {}).get("height")})
    return out


def recording(s: Session, ident: Identity, sha: str) -> dict | None:
    from ..models import Unit
    if not _readable_sha(s, ident, sha):
        return None
    c = s.get(Content, sha)
    if c is None or c.detected_type != "video":
        return None
    def slide_ref(sl):
        if not sl:
            return None
        cc = s.get(Content, sl.get("content"))
        v = s.scalar(select(Version).where(Version.content_sha == sl.get("content")))
        return {"content": sl.get("content"), "page": sl.get("ordinal"), "name": display_name(s, sl.get("content")),
                "doc_number": (cc.meta or {}).get("doc_number") if cc else None,
                "revision": v.revision_label if v else None, "current": bool(v and v.is_head)}
    frames = [{"id": u.id, "n": u.ordinal, "t": u.start_s, "end": u.end_s, "blob": u.blob_key, "text": (u.text or "")[:400],
               "slide": slide_ref((u.meta or {}).get("slide")), "described": bool((u.meta or {}).get("spec"))}
              for u in s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "frame").order_by(Unit.ordinal))]
    segs = [{"t": u.start_s, "end": u.end_s, "speaker": u.speaker, "text": u.text,
             "frame": (u.meta or {}).get("frame_ordinal")}
            for u in s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "segment").order_by(Unit.ordinal))]
    summary = next((r for r in recordings(s, ident) if r["content"] == sha), None)
    return {"summary": summary, "frames": frames, "segments": segs}


def vtt(s: Session, sha: str) -> str:
    from ..models import Unit

    def ts(t):
        t = max(0.0, t or 0.0)
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"
    out = ["WEBVTT", ""]
    for u in s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "segment").order_by(Unit.ordinal)):
        out += [f"{ts(u.start_s)} --> {ts(u.end_s)}", (f"<v {u.speaker}>" if u.speaker else "") + (u.text or ""), ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- live pipeline
STAGES = [("source.ingest_folder", "Copy in"), ("source.scan", "Scan"), ("file.hash", "Fingerprint"),
          ("content.extract", "Read"), ("content.relate", "Relate"), ("content.capsule", "Capsule"),
          ("content.index", "Index"), ("content.optimize", "Compress"), ("capsule.rebuild", "Recreate video")]


def _job_label(s: Session, j) -> str:
    p = j.payload or {}
    if p.get("source"):
        return p["source"]
    if p.get("name"):
        return p["name"]
    if p.get("file_id"):
        f = s.get(File, p["file_id"])
        if f is not None:
            return f.filename
    if p.get("sha"):
        return display_name(s, p["sha"])
    if p.get("item_id"):
        from ..models import DeletionItem
        it = s.get(DeletionItem, p["item_id"])
        f = s.get(File, it.file_id) if it is not None else None
        if f is not None:
            return f.filename
    if p.get("plan_id"):
        return f"plan {p['plan_id'][:8]}"
    return ""


def pipeline(s: Session) -> dict:
    from ..clock import now
    from ..models import Job
    t = now()
    counts: dict = defaultdict(lambda: defaultdict(int))
    for kind, status, n in s.execute(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)):
        counts[kind][status] = n
    known = {k for k, _ in STAGES}
    other = defaultdict(int)
    for kind, st in counts.items():
        if kind not in known:
            for k2, n in st.items():
                other[k2] += n
    stages = [{"kind": k, "label": lbl, **{x: counts[k].get(x, 0) for x in ("queued", "running", "succeeded", "dead")}}
              for k, lbl in STAGES]
    stages.append({"kind": "other", "label": "Plans and other", **{x: other.get(x, 0) for x in ("queued", "running", "succeeded", "dead")}})
    running = [{"id": j.id, "kind": j.kind, "what": _job_label(s, j), "attempt": j.attempts,
                "progress": (j.result or {}).get("progress"),
                "seconds": round((t - (j.updated_at or j.created_at)).total_seconds(), 1), "worker": (j.lease_owner or "")[:40]}
               for j in s.scalars(select(Job).where(Job.status == "running").order_by(Job.updated_at))]
    recent = [{"id": j.id, "kind": j.kind, "what": _job_label(s, j), "status": j.status,
               "seconds": round(((j.finished_at or j.updated_at) - j.created_at).total_seconds(), 1),
               "at": (j.finished_at or j.updated_at).isoformat()}
              for j in s.scalars(select(Job).where(Job.status.in_(("succeeded", "dead"))).order_by(Job.finished_at.desc()).limit(25))]
    dead = [{"id": j.id, "kind": j.kind, "what": _job_label(s, j), "attempts": j.attempts,
             "error": (j.last_error or "").strip().splitlines()[-1][:300] if j.last_error else ""}
            for j in s.scalars(select(Job).where(Job.status == "dead").order_by(Job.updated_at.desc()).limit(20))]
    files = dict(s.execute(select(File.ingest_state, func.count()).where(File.status == "active").group_by(File.ingest_state)).all())
    queued = s.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) or 0
    workers = len({j["worker"] for j in running if j["worker"]})
    sources = [{"name": x.name, "system": x.system, "root": (x.config or {}).get("root"),
                "files": s.scalar(select(func.count()).select_from(File).where(File.source_name == x.name, File.status == "active")) or 0}
               for x in s.scalars(select(Source).order_by(Source.name))]
    return {"at": t.isoformat(), "stages": stages, "running": running, "recent": recent, "dead": dead,
            "files_by_state": files, "queued": queued, "busy_workers": workers, "sources": sources}
