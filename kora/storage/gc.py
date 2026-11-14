"""Garbage collection of blobs nothing references any more (e.g. pages materialised for a
what-if plan that was rolled back, or a capsule's old transcript after an erasure).
Blobs younger than `min_age_s` are never touched: an extraction job may have written
them and not yet committed the rows that reference them."""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Capsule, Content, DeletionItem, Rendition, SlideBlob, Unit
from . import get_blobstore


def referenced(s: Session) -> dict[str, set[str]]:
    hot: set[str] = set()
    for t, b in s.execute(select(Unit.thumb_key, Unit.blob_key)):
        hot |= {k for k in (t, b) if k}
    hot |= set(s.scalars(select(SlideBlob.blob_key)))
    hot |= {k for k in s.scalars(select(Rendition.blob_key)) if k}
    for v in s.scalars(select(Rendition.validation)):
        for pv in (v or {}).get("previews", []):
            hot |= {pv.get("before"), pv.get("after")} - {None}
    for cap in s.scalars(select(Capsule)):
        for a in cap.manifest.get("artifacts", []):
            hot |= {k for k in (a.get("blob"), a.get("slide_blob"), a.get("thumb")) if k}
    for c in s.scalars(select(Content).where(Content.detected_type == "video")):
        if (c.media or {}).get("audio_blob"):
            hot.add(c.media["audio_blob"])
        if ((c.media or {}).get("rebuilt") or {}).get("key"):
            hot.add(c.media["rebuilt"]["key"])
    for m in s.scalars(select(Unit.meta).where(Unit.kind == "frame")):
        if ((m or {}).get("spec") or {}).get("image"):
            hot.add(m["spec"]["image"])
    live = ("quarantining", "quarantined", "purging", "restoring")
    q = set(s.scalars(select(DeletionItem.victim_sha).where(DeletionItem.quarantine_tier == "quarantine",
                                                            DeletionItem.state.in_(live))))
    a = set(s.scalars(select(DeletionItem.victim_sha).where(DeletionItem.quarantine_tier == "archive",
                                                            DeletionItem.state != "restored")))
    return {"hot": hot, "quarantine": q, "archive": a}


def collect(s: Session, dry_run: bool = True, min_age_s: int = 3600) -> dict:
    store = get_blobstore()
    refs = referenced(s)
    out = {}
    for tier, keep in refs.items():
        orphans, freed = [], 0
        for key in list(store.iter_keys(tier)):
            if key in keep:
                continue
            path = getattr(store, "_path", None)
            if path and time.time() - path(tier, key).stat().st_mtime < min_age_s:
                continue
            size = store.size(tier, key)
            orphans.append(key)
            freed += size
            if not dry_run:
                store.delete(tier, key)
        out[tier] = {"orphans": len(orphans), "bytes": freed, "deleted": not dry_run}
    return out
