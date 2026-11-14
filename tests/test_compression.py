"""Governed compression: measured quality budget, in-place replacement saga, restore, purge."""
from __future__ import annotations

import hashlib

from sqlalchemy import select

from kora.dedup import classes as C


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _approve_execute(s, pid, ids):
    from kora.governance import executor as ex
    ex.submit(s, ids["carol"], pid)
    ex.approve(s, ids["dave"], pid)
    ex.approve(s, ids["gina"], pid)                   # COMPRESSED needs two approvers
    ex.execute(s, ids["erin"], pid)


def test_judge_rejects_renditions_outside_budget():
    from kora.optimization.engine import judge
    ok, checks = judge("video.transcode", {"duration_delta_s": 0.1, "has_audio": True, "ssim": 0.91,
                                           "ocr_recall": 0.97, "ocr_frames_checked": 4}, 100, 30)
    assert not ok and [c["name"] for c in checks if not c["passed"]] == ["ssim"]
    ok, checks = judge("video.transcode", {"duration_delta_s": 0.1, "has_audio": False, "ssim": 0.99,
                                           "ocr_recall": 0.5, "ocr_frames_checked": 4}, 100, 30)
    assert {c["name"] for c in checks if not c["passed"]} == {"audio_kept", "slide_text_readable"}
    ok, _ = judge("pdf.recompress", {"pages_equal": True, "text_identical": True, "min_page_ssim": 0.99}, 100, 90)
    assert not ok                                      # 10 % is below the 20 % saving floor
    ok, _ = judge("pdf.recompress", {"pages_equal": True, "text_identical": False, "min_page_ssim": 0.99}, 100, 50)
    assert not ok                                      # text must be identical


def test_recording_rendition_is_measured_and_never_recompressed(world):
    from kora import db
    from kora.models import Content, Rendition
    from kora.optimization.engine import candidates
    with db.session_scope() as s:
        rs = list(s.scalars(select(Rendition).where(Rendition.operation == "video.transcode")))
        assert rs, "auto-compression should have produced a rendition for the 20 MB recording"
        r = max(rs, key=lambda r: r.validation["original_bytes"])
        names = {c["name"]: c for c in r.validation["checks"]}
        assert r.passed, names
        assert {"net_saving", "duration", "audio_kept", "ssim"} <= set(names)
        assert r.validation["saving_ratio"] >= 0.2
        rendition_like = Content(sha256="x" * 64, size_bytes=10**7, detected_type="video",
                                 media={"bit_rate": 10**6}, meta={"rendition_of": r.content_sha})
        assert candidates(rendition_like) == []


def test_compress_plan_replaces_in_place_restores_and_purges(world, ids, drain):
    from kora import clock, db
    from kora.governance import executor as ex, planner
    from kora.models import Content, DeletionPlan, File, Rendition, Unit
    from kora.storage import get_blobstore
    rec = world / "sources" / "sharepoint" / "network-team" / "Recordings" / "Architecture_Review_2026-05-12.mp4"
    orig_sha, orig_size = _sha(rec), rec.stat().st_size
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "compress")
        pid = p.id
        row = next(r for r in p.evidence["items"] if r["victim"]["path"].endswith("Architecture_Review_2026-05-12.mp4"))
        assert row["class"] == C.COMPRESSED and row["action"] == "REPLACE_WITH_RENDITION" and row["deletable"]
        assert {"quality_budget", "rendition_intact", "net_saving", "lossy_allowed"} <= {c["name"] for c in row["checks"]}
        assert p.required_approvals == 2
        new_sha = next(it for it in p.items if it.id == row["item_id"]).survivor_sha
        orig_segments = s.scalar(select(Content).where(Content.sha256 == orig_sha)).unit_count
        _approve_execute(s, pid, ids)
    drain()
    # the file is still there, smaller, holding exactly the approved rendition
    assert rec.exists() and _sha(rec) == new_sha and rec.stat().st_size < 0.8 * orig_size
    store = get_blobstore()
    with db.session_scope() as s:
        f = s.scalar(select(File).where(File.path == "Recordings/Architecture_Review_2026-05-12.mp4"))
        assert f.status == "active" and f.sha256 == new_sha
        c = s.get(Content, new_sha)
        assert c.meta.get("rendition_of") == orig_sha and c.extracted
        assert c.media.get("transcript_inherited_from") == orig_sha          # no second ASR pass
        segs = s.scalar(select(Unit.id).where(Unit.content_sha == new_sha, Unit.kind == "segment").limit(1))
        assert segs is not None
        assert store.verify("quarantine", orig_sha)                         # byte-exact original kept
        from kora.models import Relation, ReviewTask
        twin = s.scalar(select(Relation).where(((Relation.a_sha == orig_sha) & (Relation.b_sha == new_sha)) |
                                               ((Relation.a_sha == new_sha) & (Relation.b_sha == orig_sha))))
        assert twin is None                                                 # not reported as its own duplicate
        assert s.scalar(select(ReviewTask).where(ReviewTask.key.like(f"transcript:{new_sha}%"))) is None
        assert s.get(DeletionPlan, pid).status == "quarantined"
        item = next(it for it in s.get(DeletionPlan, pid).items if it.id == row["item_id"])
        ex.restore(s, ids["erin"], pid, [item.id])
    drain()
    assert _sha(rec) == orig_sha                                            # restore swaps the original back
    with db.session_scope() as s:
        f = s.scalar(select(File).where(File.path == "Recordings/Architecture_Review_2026-05-12.mp4"))
        assert f.sha256 == orig_sha and f.status == "active"

    # second round: execute and purge after the grace period
    with db.session_scope() as s:
        p2 = planner.create(s, ids["carol"], "compress")
        pid2 = p2.id
        _approve_execute(s, pid2, ids)
    drain()
    assert _sha(rec) == new_sha
    clock.advance(days=31)
    with db.session_scope() as s:
        ex.purge(s, ids["erin"], pid2)
    drain()
    with db.session_scope() as s:
        f = s.scalar(select(File).where(File.path == "Recordings/Architecture_Review_2026-05-12.mp4"))
        assert f.status == "active" and f.sha256 == new_sha                 # purge never removes the live file
        assert not store.exists("quarantine", orig_sha)
        r = s.scalar(select(Rendition).where(Rendition.content_sha == orig_sha, Rendition.passed.is_(True)))
        assert r.blob_key is None and "blob_released_at" in r.validation    # hot copy no longer needed
    assert not store.exists("hot", new_sha)
    assert _sha(rec) == new_sha


def test_compress_blocks_when_source_changes_after_approval(world, ids, drain):
    from kora import db
    from kora.governance import planner
    from kora.models import DeletionItem, DeletionPlan
    rec = world / "sources" / "sharepoint" / "network-team" / "Recordings" / "Architecture_Review_2026-05-12.mp4"
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "compress")
        pid = p.id
        _approve_execute(s, pid, ids)
    with open(rec, "ab") as fh:                       # someone edits the recording mid-flight
        fh.write(b"\0" * 16)
    drain()
    with db.session_scope() as s:
        items = [it for it in s.get(DeletionPlan, pid).items]
        it = next(i for i in items if i.victim_sha and i.dup_class == C.COMPRESSED)
        assert it.state == "blocked"
    assert rec.read_bytes().endswith(b"\0" * 16)        # the edited file was not overwritten


def test_pdf_recompression_downsamples_images_and_keeps_text(tmp_path):
    import io

    import numpy as np
    import pymupdf
    from PIL import Image

    from kora.extraction import optimize
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:1800, 0:3000]
    base = np.stack([(x / 12) % 255, (y / 8) % 255, ((x + y) / 20) % 255], -1)
    photo = np.clip(base + rng.normal(0, 6, base.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(photo).save(buf, "JPEG", quality=95)
    src = tmp_path / "deck.pdf"
    d = pymupdf.open()
    for i in range(2):
        pg = d.new_page(width=960, height=540)                    # 13.33 x 7.5 in slide
        pg.insert_image(pymupdf.Rect(40, 120, 520, 408), stream=buf.getvalue())   # 3000 px over 6.7 in ~ 450 dpi
        pg.insert_text((560, 200), f"Slide {i + 1}: radio access network rollout", fontsize=18)
    d.save(src)
    out = tmp_path / "out"
    out.mkdir()
    res = optimize.pdf(src, out, {"dpi_threshold": 200, "dpi_target": 150, "quality": 80, "min_page_ssim": 0.97})
    m = res["measured"]
    assert m["pages_equal"] and m["text_identical"] and m["images_recompressed"] == 1
    assert res["bytes"] < 0.5 * src.stat().st_size
    with pymupdf.open(out / res["file"]) as r:
        img = r.extract_image(r[0].get_images()[0][0])
        assert img["width"] < 1100 and img["ext"] == "jpeg"         # downsampled to ~150 dpi
    assert 0 < m["min_page_ssim"] <= 1


def test_rendition_lineage_survives_reextraction(world, drain):
    """A compressed PDF re-extracted later still records which original it came from."""

    from kora import db
    from kora.jobs import enqueue
    from kora.models import Content, File
    with db.session_scope() as s:
        f = s.scalar(select(File).where(File.path == "Presentations/Network_Architecture_B.pdf"))
        orig = f.sha256
        p = world / "sources" / "sharepoint" / "network-team" / f.path
    import pymupdf                                   # a lossy rendition renders slightly differently
    d = pymupdf.open(p)
    d[0].draw_rect(pymupdf.Rect(2, 2, 6, 6), color=(0.9, 0.9, 0.9), fill=(0.9, 0.9, 0.9))
    tweaked = d.tobytes(garbage=3)
    new_sha = hashlib.sha256(tweaked).hexdigest()
    with db.session_scope() as s:
        f = s.scalar(select(File).where(File.path == "Presentations/Network_Architecture_B.pdf"))
        s.add(Content(sha256=new_sha, size_bytes=len(tweaked), detected_type="pdf", meta={"rendition_of": orig}))
        s.flush()
        p.write_bytes(tweaked)
        f.sha256 = new_sha
        enqueue(s, "content.extract", {"sha": new_sha, "file_id": f.id}, key=f"extract:{new_sha}:test")
    drain()
    from kora.models import Lineage, Version
    with db.session_scope() as s:
        c = s.get(Content, new_sha)
        assert c.extracted and c.meta.get("rendition_of") == orig
        assert c.normalized_hash != s.get(Content, orig).normalized_hash
        v = s.scalar(select(Version).where(Version.content_sha == new_sha))
        lin = s.get(Lineage, v.lineage_id)
        assert not lin.has_conflict                     # same revision as its original, not a second "Rev B"
        assert v.is_head and v.revision_label == "B"    # the live, compressed file represents Rev B
