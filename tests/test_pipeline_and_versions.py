"""The ingested sample corpus: version lineages, relationship graph, capsules, search."""
from __future__ import annotations

from sqlalchemy import select

from kora.dedup import classes as C


def _name(s, sha):
    from kora.queries import display_name
    return display_name(s, sha)


def test_all_jobs_succeeded(world):
    from kora import db
    from kora.models import Job
    with db.session_scope() as s:
        bad = [(j.kind, j.last_error) for j in s.scalars(select(Job).where(Job.status != "succeeded"))]
    assert not bad


def test_ericsson_lineage_order_variant_and_excerpt(world):
    from kora import db
    from kora.models import Lineage
    with db.session_scope() as s:
        lin = s.scalar(select(Lineage).where(Lineage.key == "docno:1/1551-LZA 701 0001 UEN"))
        assert (lin.ordering_basis, lin.ordering_confidence, lin.has_conflict) == ("revision", "high", False)
        reps = sorted((v for v in lin.versions if not v.variant_of_id), key=lambda v: v.ordinal)
        assert [v.revision_label for v in reps] == ["PA1", "PA2", "A", "B"]
        assert reps[-1].is_head and [v.released for v in reps] == [False, False, True, True]
        variants = [v for v in lin.versions if v.variant_of_id]
        assert len(variants) == 1 and _name(s, variants[0].content_sha) == "Network_Architecture_A_resaved.pdf"
        names = {_name(s, v.content_sha) for v in lin.versions}
        assert "Network_Architecture_excerpt.pdf" not in names     # excerpt is a subset, not a revision
        b = reps[-1].diff_from_parent
        assert b["unchanged_body"] >= 4 and b["added"] and b["removed"]   # footers renumbered after removal


def test_filename_lineages(world):
    from kora import db
    from kora.models import Lineage
    with db.session_scope() as s:
        lin = s.scalar(select(Lineage).where(Lineage.key == "name:team update"))
        order = [v.revision_label for v in sorted(lin.versions, key=lambda v: v.ordinal)]
        assert order == ["", "Final", "Final_v2"] and lin.ordering_confidence == "medium"
        assert s.scalar(select(Lineage).where(Lineage.key == "name:onboarding")) is not None


def test_relationship_graph_has_every_class(world):
    from kora import db
    from kora.models import Relation
    with db.session_scope() as s:
        rels = {(r.kind, frozenset({_name(s, r.a_sha), _name(s, r.b_sha)})) for r in s.scalars(select(Relation))}
        kinds = {k for k, _ in rels}
    assert {C.REL_CONTAINER, C.REL_SUBSET, C.REL_PARTIAL, C.REL_NEAR, C.REL_MEDIA, C.REL_REVISION,
            C.REL_PRESENTED, C.REL_KNOWLEDGE} <= kinds
    assert (C.REL_CONTAINER, frozenset({"Network_Architecture_A.pdf", "Network_Architecture_A_resaved.pdf"})) in rels
    assert (C.REL_PARTIAL, frozenset({"Network_Architecture_A.pdf", "5G_Core_Overview.pdf"})) in rels
    assert (C.REL_NEAR, frozenset({"Network_Architecture_A.pdf", "Architecture_notes_retyped.pdf"})) in rels
    assert (C.REL_PRESENTED, frozenset({"Network_Architecture_A.pdf", "Architecture_Review_2026-05-12.mp4"})) in rels
    # no overlap edges between members of the same lineage
    assert (C.REL_PARTIAL, frozenset({"Network_Architecture_A.pdf", "Network_Architecture_B.pdf"})) not in rels


def test_recording_capsule_links_frames_to_rev_a_slides(world):
    from kora import db
    from kora.capsule.builder import verify_signature
    from kora.models import Capsule, Content
    with db.session_scope() as s:
        vid = s.scalar(select(Content).where(Content.detected_type == "video", Content.size_bytes > 10_000_000))
        cap = s.get(Capsule, vid.sha256)
        assert cap.valid and verify_signature(cap)
        frames = [a for a in cap.manifest["artifacts"] if a["role"] == "keyframe"]
        assert len(frames) == 9 and all(f["slide"] for f in frames)
        assert [f["slide"]["ordinal"] for f in frames] == list(range(1, 10))
        assert cap.size_bytes < vid.size_bytes * 0.05
        facts = {f["type"] for f in cap.manifest["facts"]}
        assert {"decision", "action_item"} <= facts
        cap.manifest = {**cap.manifest, "acl_snapshot": ["everyone"]}       # tamper
        assert not verify_signature(cap)
        s.rollback()


def test_search_is_evidence_based_and_permission_filtered(world, ids):
    from kora import db
    from kora.search.index import search
    with db.session_scope() as s:
        r = search(s, ids["alice"], "as you see here edge data centre", 3)["results"]
        top = r[0]
        assert top["type"] == "transcript" and top["speaker"] == "Alice Andersson"
        assert top["slide"]["ordinal"] == 3 and top["slide"]["revision"] == "A"
        frank = search(s, ids["frank"], "latency budget", 10)["results"]
        assert frank and all("5G_Core_Overview (1).pdf" == x.get("document") for x in frank)
        leak = search(s, ids["frank"], "lab bench optics soak test", 10)["results"]
        assert all(x.get("document") == "5G_Core_Overview (1).pdf" for x in leak)   # never Bob's lab session
        from kora.security.auth import Identity
        bob = Identity("bob", frozenset({"viewer"}), ("network-team",))
        own = search(s, bob, "lab bench optics soak test", 10)["results"]
        assert any(x["type"] == "frame" and "Lab bench" in x["text"] for x in own)       # owner sees OCR'd frame
        assert not any("Lab bench" in x["text"] for x in search(s, ids["carol"], "lab bench optics soak test", 10)["results"])
