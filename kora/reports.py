"""Storage savings, measured - not assumed.

potential   dry-run of every planner inside a SAVEPOINT that is rolled back,
            so the numbers come from exactly the logic that would execute
realized    bytes of items actually quarantined / purged
overhead    bytes KORA itself keeps (capsules, slide store, thumbnails,
            archive and quarantine tiers) - subtracted to get net savings
recordings  per-recording measured reduction + an extrapolation to the
            challenge's "2 h / ~1 GB" scenario from the measured per-minute
            capsule cost
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .governance import planner
from .models import Capsule, Content, DeletionItem, DeletionPlan, File, SlideBlob, Unit
from .queries import display_name
from .security.auth import SYSTEM
from .storage import get_blobstore

MB = 1024 * 1024


def inventory(s: Session) -> dict:
    rows = s.execute(select(File.system, File.detected_type, func.count(), func.sum(File.size_bytes))
                     .where(File.status == "active").group_by(File.system, File.detected_type)).all()
    total = int(sum(r[3] or 0 for r in rows))
    distinct = int(s.scalar(select(func.sum(Content.size_bytes)).where(
        Content.sha256.in_(select(File.sha256).where(File.status == "active")))) or 0)
    return {"active_files": sum(r[2] for r in rows), "active_bytes": total, "distinct_content_bytes": distinct,
            "by_system_type": [{"system": r[0], "type": r[1], "files": r[2], "bytes": int(r[3] or 0)} for r in rows]}


def potential(s: Session) -> dict:
    out = {}
    for kind in ("duplicates", "revisions", "subsets", "media", "recordings", "compress"):
        sp = s.begin_nested()
        try:
            p = planner.create(s, SYSTEM, kind, title=f"dry-run {kind}")
            items = []
            for it in p.items:
                hard_fail = {c["name"] for c in it.checks if c["hard"] and not c["passed"]}
                items.append((it, hard_fail))
            out[kind] = {
                "deletable_items": sum(1 for it, h in items if not h),
                "deletable_bytes": sum(it.bytes_reclaimable for it, h in items if not h),
                # blocked only because another open plan already holds the item: counts once that plan finishes
                "pending_other_plan_bytes": sum(it.bytes_reclaimable for it, h in items if h == {"no_conflicting_plan"}),
                "blocked_items": sum(1 for it, h in items if h and h != {"no_conflicting_plan"}),
                "blocked_by": p.evidence["summary"]["blocked_by"],
                "_files": {it.file_id: it.bytes_reclaimable for it, h in items if not h or h == {"no_conflicting_plan"}},
            }
        finally:
            sp.rollback()
    return out


def realized(s: Session) -> dict:
    agg = defaultdict(lambda: {"quarantined_bytes": 0, "purged_bytes": 0, "archived_bytes": 0, "items": 0})
    for it in s.scalars(select(DeletionItem).where(DeletionItem.state.in_(("quarantined", "purged")))):
        a = agg[it.dup_class]
        a["items"] += 1
        if it.quarantine_tier == "archive":
            a["archived_bytes"] += it.bytes_reclaimable
        elif it.state == "purged":
            a["purged_bytes"] += it.bytes_reclaimable
        else:
            a["quarantined_bytes"] += it.bytes_reclaimable
    return dict(agg)


def overhead(s: Session) -> dict:
    store = get_blobstore()
    caps = int(s.scalar(select(func.sum(Capsule.size_bytes))) or 0)
    slides = int(s.scalar(select(func.sum(SlideBlob.size_bytes))) or 0)
    return {"hot_tier_bytes": store.tier_bytes("hot"), "archive_tier_bytes": store.tier_bytes("archive"),
            "quarantine_tier_bytes": store.tier_bytes("quarantine"), "capsule_bytes": caps,
            "slide_store_bytes": slides}


def recordings(s: Session) -> dict:
    rows = []
    per_min = []
    for c in s.scalars(select(Content).where(Content.detected_type == "video")):
        cap = s.get(Capsule, c.sha256)
        if not cap:
            continue
        audio = sum(a.get("size", 0) for a in cap.manifest["artifacts"] if a["role"] == "audio")
        text = sum(a.get("size", 0) for a in cap.manifest["artifacts"] if a["role"] == "transcript")
        frames = sum(a.get("size", 0) for a in cap.manifest["artifacts"] if a["role"] == "keyframe")
        n_frames = sum(1 for a in cap.manifest["artifacts"] if a["role"] == "keyframe")
        mins = (c.duration_s or 0) / 60
        rows.append({"recording": display_name(s, c.sha256), "valid_capsule": cap.valid, "minutes": round(mins, 2),
                     "original_bytes": c.size_bytes, "capsule_bytes": cap.size_bytes,
                     "transcript_bytes": text, "keyframe_bytes": frames, "keyframes": n_frames, "audio_bytes": audio,
                     "reduction": round(1 - cap.size_bytes / c.size_bytes, 4) if c.size_bytes else 0,
                     "reduction_without_audio": round(1 - (cap.size_bytes - audio) / c.size_bytes, 4) if c.size_bytes else 0})
        if cap.valid and mins:
            per_min.append((text / mins, frames / max(1, n_frames), audio / mins))
    scenario = None
    if per_min:
        t, f, a = (sum(x[i] for x in per_min) / len(per_min) for i in range(3))
        # 2 h meeting, ~1 GB, assume one slide change every 2 minutes (60 keyframes)
        text_b, frame_b, audio_b = t * 120, f * 60, a * 120
        orig = 1024 * MB
        scenario = {"assumption": "2 h recording, ~1 GB, 60 slide changes; per-minute costs measured on valid capsules",
                    "transcript_bytes": int(text_b), "keyframe_bytes": int(frame_b), "audio_bytes": int(audio_b),
                    "capsule_without_audio_bytes": int(text_b + frame_b),
                    "capsule_with_audio_bytes": int(text_b + frame_b + audio_b),
                    "reduction_without_audio": round(1 - (text_b + frame_b) / orig, 4),
                    "reduction_with_audio": round(1 - (text_b + frame_b + audio_b) / orig, 4)}
    return {"recordings": rows, "scenario_2h_1gb": scenario}


def savings(s: Session) -> dict:
    inv = inventory(s)
    pot = potential(s)
    ovh = overhead(s)
    files = {k: v.pop("_files") for k, v in pot.items()}
    removed = {}
    for k in ("duplicates", "revisions", "subsets", "media"):
        for fid, b in files.get(k, {}).items():
            removed[fid] = max(removed.get(fid, 0), b)        # a file can only be removed once
    archived_f = {fid: b for fid, b in files.get("recordings", {}).items() if fid not in removed}
    # compression applies to what survives: never count a file that is removed or archived anyway
    compressed_f = {fid: b for fid, b in files.get("compress", {}).items() if fid not in removed and fid not in archived_f}
    deleted, archived, compressed = sum(removed.values()), sum(archived_f.values()), sum(compressed_f.values())
    base = inv["active_bytes"] or 1
    return {"inventory": inv, "potential": pot, "realized": realized(s), "overhead": ovh,
            "summary": {"active_bytes": inv["active_bytes"],
                        "bytes_removable": deleted,                  # copies, variants, superseded drafts, excerpts
                        "bytes_movable_to_archive": archived,        # recordings replaced by validated capsules
                        "bytes_saved_by_compression": compressed,    # survivors replaced by budgeted renditions
                        "hot_bytes_freeable": deleted + archived + compressed,
                        "kora_overhead_bytes": ovh["capsule_bytes"],
                        "removable_ratio": round(deleted / base, 4),
                        "compression_ratio": round(compressed / base, 4),
                        "hot_reduction_ratio": round((deleted + archived + compressed - ovh["capsule_bytes"]) / base, 4),
                        "net_reduction_ratio": round((deleted + compressed - ovh["capsule_bytes"]) / base, 4),
                        "note": "each file counted once (removal > archive > compression); archive moves free hot "
                                "storage but the bytes still exist in the archive tier, so net excludes them"},
            "recordings": recordings(s)}


def measured_ratios(s: Session) -> dict:
    """Savings ratios measured on this instance's own files (used by the scenario calculator).
    Falls back to the figures measured on Ericsson's sample files when a ratio has no data here."""
    from .models import Rendition as R
    out = {}
    for op, key in (("video.transcode", "video_compression"), ("pdf.recompress", "pdf_compression")):
        rs = [r for r in s.scalars(select(R).where(R.operation == op, R.passed.is_(True)))]
        orig = sum(r.validation.get("original_bytes", 0) for r in rs)
        out[key] = {"ratio": round(1 - sum(r.size_bytes for r in rs) / orig, 4) if orig else None, "files": len(rs)}
    caps = [(c, s.get(Capsule, c.sha256)) for c in s.scalars(select(Content).where(Content.detected_type == "video"))]
    caps = [(c, cap) for c, cap in caps if cap is not None and c.size_bytes > 5_000_000
            and not (c.meta or {}).get("rendition_of")]
    orig = sum(c.size_bytes for c, _ in caps)
    audio = sum(sum(a.get("size", 0) for a in cap.manifest["artifacts"] if a["role"] == "audio") for _, cap in caps)
    capb = sum(cap.size_bytes for _, cap in caps)
    out["capsule_text_frames"] = {"ratio": round(1 - (capb - audio) / orig, 4) if orig else None, "files": len(caps)}
    out["capsule_with_voice"] = {"ratio": round(1 - capb / orig, 4) if orig else None, "files": len(caps)}
    reference = {"video_compression": 0.847, "pdf_compression": 0.310, "capsule_text_frames": 0.994,
                 "capsule_with_voice": 0.988}               # Ericsson sample files (docs/REAL_DATA_RESULTS.md)
    for k, v in reference.items():
        if out[k]["ratio"] is None:
            out[k] = {"ratio": v, "files": 0, "source": "Ericsson sample files"}
        else:
            out[k]["source"] = "measured here"
    out["duplicate_copy"] = {"ratio": 1.0, "source": "a verified copy remains; the extra copy is removed"}
    out["superseded_revision"] = {"ratio": 0.79, "source": "Ericsson SD-WAN deck Rev B: unique slides kept, 79 % freed"}
    return out
