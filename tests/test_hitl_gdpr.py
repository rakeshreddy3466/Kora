"""Human-in-the-loop decisions and GDPR controls."""
from __future__ import annotations

import json
import zipfile

import pytest
from sqlalchemy import select


def test_pii_detection_is_precise():
    from kora.privacy.pii import find, redact
    t = ("Mail bob.berg@ericsson.com, ring +46 70 123 45 67 or 070-123 45 67, pnr 811228-9874, "
         "doc 1/1551-LZA 701 0001 Uen, date 2026-05-12, random 123456-7890, EAB-24:000123")
    kinds = sorted(f.kind for f in find(t))
    assert kinds == ["EMAIL", "PHONE", "PHONE", "PNR"]
    assert "1/1551-LZA 701 0001" in redact(t) and "[PNR]" in redact(t)
    assert redact("Ask Alice Andersson", ["Alice Andersson"]) == "Ask [NAME]"


def test_near_duplicates_routed_to_humans_with_roles(world, ids):
    from kora import db, hitl
    from kora.models import Relation, ReviewTask
    with db.session_scope() as s:
        tasks = list(s.scalars(select(ReviewTask).where(ReviewTask.kind == "near_duplicate")))
        assert tasks and all(t.status == "open" for t in tasks)
        with pytest.raises(PermissionError):
            hitl.decide(s, ids["alice"], tasks[0].id, "same_document")          # viewer cannot decide
        with pytest.raises(hitl.ReviewError):
            hitl.decide(s, ids["carol"], tasks[0].id, "delete_it")              # not an allowed answer
        hitl.decide(s, ids["carol"], tasks[0].id, "different_documents", "checked both decks")
        assert s.get(Relation, tasks[0].subject_id).details["human_decision"] == "different_documents"
        with pytest.raises(hitl.ReviewError):
            hitl.decide(s, ids["carol"], tasks[0].id, "same_document")          # decided once


def test_low_confidence_order_blocks_compaction_until_confirmed(world, no_saving_rule, ids):
    from kora import db, hitl
    from kora.governance import executor as ex, planner
    from kora.models import Lineage, ReviewTask
    with db.session_scope() as s:
        lin = s.scalar(select(Lineage).where(Lineage.key == "name:team update"))
        lin.ordering_confidence = "low"
        hitl.generate(s, lin.versions[0].content_sha)
        p = planner.create(s, ids["carol"], "revisions")
        row = next(r for r in p.evidence["items"] if r["victim"]["path"] == "Team/Team_Update.pdf")
        assert not row["deletable"]
        ex.cancel(s, ids["carol"], p.id)
        t = s.scalar(select(ReviewTask).where(ReviewTask.kind == "lineage_order", ReviewTask.subject_id == lin.id))
        hitl.decide(s, ids["carol"], t.id, "confirm_order")
        assert lin.ordering_confidence == "high"
        p2 = planner.create(s, ids["carol"], "revisions")
        assert next(r for r in p2.evidence["items"] if r["victim"]["path"] == "Team/Team_Update.pdf")["deletable"]


def test_revision_conflict_resolved_by_human(world, ids):
    from kora import db, hitl
    from kora.models import Lineage, ReviewTask
    from kora.versioning import lineage
    with db.session_scope() as s:
        lin = s.scalar(select(Lineage).where(Lineage.key == "docno:1/1551-LZA 701 0001 UEN"))
        pa2 = next(v for v in lin.versions if v.revision_label == "PA2")
        pa2.revision_label, pa2.revision_key = "PA1", [1, 0, 1]
        lineage.reorder(s, lin)
        hitl.generate(s, pa2.content_sha)
        t = s.scalar(select(ReviewTask).where(ReviewTask.kind == "lineage_conflict"))
        keep = next(o for o in t.options if pa2.id not in o)
        hitl.decide(s, ids["carol"], t.id, keep, "PA1 file is the real one")
        s.refresh(lin)
        assert not lin.has_conflict and pa2.content_sha not in {v.content_sha for v in lin.versions}


def test_machine_transcript_needs_review_before_distillation(world, ids):
    from kora import db, hitl
    from kora.capsule import builder
    from kora.models import Content, ReviewTask
    with db.session_scope() as s:
        vid = s.scalar(select(Content).where(Content.detected_type == "video").order_by(Content.size_bytes.desc()))
        vid.media = {**vid.media, "transcript_source": "asr:pocketsphinx"}
        assert not builder.build(s, vid.sha256).valid
        hitl.generate(s, vid.sha256)
        t = s.scalar(select(ReviewTask).where(ReviewTask.kind == "transcript_review"))
        assert t.blocks.startswith("distilling")
        hitl.decide(s, ids["carol"], t.id, "approve", "spot-checked 5 passages")
        assert builder.build(s, vid.sha256).valid


def test_dsar_export_and_four_eyes_erasure(world, ids, drain):
    from kora import db, hitl
    from kora.models import Capsule, Content, Principal, Unit
    from kora.privacy import dsar
    from kora.search.index import search
    from kora.security.auth import Identity
    from kora.storage import get_blobstore
    dpo_carol = Identity("carol", frozenset({"analyst", "dpo"}), ())
    dpo_gina = Identity("gina", frozenset({"dpo"}), ())
    with db.session_scope() as s:
        exp = dsar.export(s, dpo_gina, "bob")
        assert exp["spoken_segments"] and exp["files_owned"]
        vid = s.scalar(select(Content).where(Content.detected_type == "video").order_by(Content.size_bytes.desc()))
        audio = vid.media["audio_blob"]
        t = dsar.request_erasure(s, dpo_carol, "bob", "employee left, requested erasure")
        with pytest.raises(PermissionError):
            hitl.decide(s, dpo_carol, t.id, "approve_erasure")                 # requester cannot approve
        hitl.decide(s, dpo_gina, t.id, "approve_erasure")
        res = t.evidence and s.get(Principal, "bob")
        assert res.status == "erased" and res.display_name.startswith("person-")
        assert not s.scalar(select(Unit.id).where(Unit.speaker == "Bob Berg"))
        assert not get_blobstore().exists("hot", audio)                         # voice removed
        cap = s.get(Capsule, vid.sha256)
        assert not any(a["role"] == "audio" for a in cap.manifest["artifacts"])
        hits = search(s, ids["alice"], "Bob validates the lab results", 10)["results"]
        assert all("Bob Berg" not in h["text"] for h in hits if h["type"] == "transcript")
        # rev A is under legal hold (Art. 17(3)(e)): its slide text is retained and reported
        held = s.scalar(select(Unit).where(Unit.text.contains("Bob Berg")))
        assert held is not None


def test_ai_export_is_redacted_pseudonymised_and_deduplicated(world, ids, tmp_path):
    from kora import db
    from kora.models import Source
    from kora.privacy import ai_export
    with db.session_scope() as s:
        src = s.get(Source, "sp-network-team")
        src.config = {**src.config, "ai_use_allowed": True}
        res = ai_export.export(s, ids["carol"], tmp_path / "k.jsonl")
    recs = [json.loads(l) for l in (tmp_path / "k.jsonl").read_text().splitlines()]
    assert res["records"] == len(recs) > 0 and res["skipped"]["superseded"] >= 2
    blob = json.dumps(recs)
    assert "Alice Andersson" not in blob and "Bob Berg" not in blob
    assert all(r["speaker"] is None or r["speaker"].startswith("person-") for r in recs)
    assert all(r["provenance"]["content"] for r in recs)
    assert "od-bob" not in res["sources"]                                       # not allowed -> not exported


def test_audit_log_holds_no_file_names(world, ids, drain):
    from kora import db
    from kora.governance import executor as ex, planner
    from kora.models import AuditEvent, File
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "duplicates")
        ex.submit(s, ids["carol"], p.id)
        ex.approve(s, ids["dave"], p.id)
        ex.execute(s, ids["erin"], p.id)
    drain()
    with db.session_scope() as s:
        names = {f.filename for f in s.scalars(select(File))} | {f.path for f in s.scalars(select(File))}
        blob = json.dumps([e.details for e in s.scalars(select(AuditEvent))])
    assert not [n for n in names if n in blob]


def test_zip_extraction_is_guarded(tmp_path):
    from kora.runner import safe_extract
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok/a.pdf", b"%PDF-1.4")
        zf.writestr("../../escape.txt", b"x")
    with pytest.raises(ValueError):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_frame_links_survive_picture_in_picture_via_ocr(world):
    from kora import db
    from kora.media import framematch
    from kora.models import Content, FrameLink, Unit
    with db.session_scope() as s:
        vid = s.scalar(select(Content).where(Content.detected_type == "video").order_by(Content.size_bytes.desc()))
        frames = list(s.scalars(select(Unit).where(Unit.content_sha == vid.sha256, Unit.kind == "frame")))
        for f in frames:
            f.phash = "0123456789abcdef"            # no visual match possible any more
        framematch.link(s, vid)
        links = list(s.scalars(select(FrameLink).where(FrameLink.frame_unit_id.in_([f.id for f in frames]))))
        assert len(links) >= len(frames) - 2 and all(l.method == "ocr" for l in links)
