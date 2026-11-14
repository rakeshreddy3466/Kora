"""Two 12-slide decks that share 11 slides (the Ericsson team's example): revision or variant?

* same document number, Rev A -> Rev B          -> a revision: B is current, A can be compacted
* same file-name stem, different property titles -> two documents (customer variants): never revisions
* same stem with only a bare letter (Offer_A/B), no titles -> a person decides; "different documents"
  splits them and records the shared slides instead
"""
from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy import or_, select

TOPICS = ["Why edge computing", "Market size", "Customer pain points", "Our platform", "Architecture",
          "Security model", "Pricing", "Roadmap", "Case study A", "Case study B", "Team", "Next steps"]


def _deck(path: Path, slide7: tuple[str, str], title: str = "", subject: str = "") -> None:
    from pptx import Presentation
    prs = Presentation()
    prs.core_properties.title, prs.core_properties.subject = title, subject
    for i, t in enumerate(TOPICS, start=1):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        head, body = slide7 if i == 7 else (t, f"{t}: the facts we present to every customer.")
        s.shapes.title.text = head
        s.placeholders[1].text_frame.text = body
    buf = io.BytesIO()
    prs.save(buf)
    path.write_bytes(buf.getvalue())


def _ingest(folder: Path, name: str):
    from kora.runner import run
    run(folder, name, "sharepoint", "alice", ["everyone"], 1)


def _pair(s, a: str, b: str):
    from kora.models import Content, File, Relation
    sa = s.scalar(select(File).where(File.filename == a)).sha256
    sb = s.scalar(select(File).where(File.filename == b)).sha256
    rels = {r.kind: r for r in s.scalars(select(Relation).where(
        or_((Relation.a_sha == sa) & (Relation.b_sha == sb), (Relation.a_sha == sb) & (Relation.b_sha == sa))))}
    return s.get(Content, sa), s.get(Content, sb), rels


def test_same_document_one_changed_slide_is_a_revision(fresh_db, tmp_path):
    from kora import db
    from kora.models import Version
    d = tmp_path / "same"
    d.mkdir()
    _deck(d / "Edge_offer_RevA.pptx", ("Pricing", "List price 100 per site."), "Edge offer", "1/221 09-ABC 123 4567 Uen Rev A")
    _deck(d / "Edge_offer_RevB.pptx", ("Pricing", "List price 90 per site from 2026."), "Edge offer", "1/221 09-ABC 123 4567 Uen Rev B")
    _ingest(d, "same")
    with db.session_scope() as s:
        a, b, rels = _pair(s, "Edge_offer_RevA.pptx", "Edge_offer_RevB.pptx")
        assert "REVISION_OF" in rels and "PARTIAL_OVERLAP" not in rels
        va = s.scalar(select(Version).where(Version.content_sha == a.sha256))
        vb = s.scalar(select(Version).where(Version.content_sha == b.sha256))
        assert va.lineage_id == vb.lineage_id and vb.is_head and not va.is_head
        assert va.lineage.ordering_confidence == "high"
        d = vb.diff_from_parent                                             # only slide 7 changed
        assert d["unchanged"] == 11 and [m["to"] for m in d["modified"]] == [7] and not d["added"]


def test_customer_variants_with_their_own_titles_are_not_revisions(fresh_db, tmp_path):
    from kora import db
    from kora.models import Version
    d = tmp_path / "variants"
    d.mkdir()
    _deck(d / "Offer_Operator_X.pptx", ("Pricing for Operator X", "85 per site, 3 years."), "Offer for Operator X")
    _deck(d / "Offer_Operator_Y.pptx", ("Pricing for Operator Y", "95 per site, 1 year."), "Offer for Operator Y")
    _ingest(d, "variants")
    with db.session_scope() as s:
        x, y, rels = _pair(s, "Offer_Operator_X.pptx", "Offer_Operator_Y.pptx")
        assert "REVISION_OF" not in rels
        ov = rels["PARTIAL_OVERLAP"]
        assert ov.details["shared_slides"] == 11
        assert set(ov.details["unique_slides"].values()) == {1}
        vx = s.scalar(select(Version).where(Version.content_sha == x.sha256))
        vy = s.scalar(select(Version).where(Version.content_sha == y.sha256))
        assert vx.lineage_id != vy.lineage_id and vx.is_head and vy.is_head   # both current, both kept
        # deleting one variant by hand: KORA says exactly what would exist only in that file
        from kora.governance import planner
        from kora.models import File
        from kora.security.auth import Identity
        fx = s.scalar(select(File).where(File.filename == "Offer_Operator_X.pptx"))
        plan = planner.create(s, Identity("alice", frozenset({"analyst"}), ("everyone",)), "manual",
                              {"file_ids": [fx.id], "reason": "deal closed"})
        sole = next(c for c in plan.items[0].checks if c["name"] == "sole_copy")
        assert "11 of 12 slides also live in Offer_Operator_Y.pptx; slide 7 exists only here" in sole["detail"]


def test_a_bare_letter_in_the_name_goes_to_a_person_who_can_split(fresh_db, tmp_path):
    from kora import db, hitl
    from kora.models import ReviewTask, Version
    from kora.security.auth import Identity
    d = tmp_path / "letters"
    d.mkdir()
    _deck(d / "Offer_A.pptx", ("Pricing", "85 per site."))
    _deck(d / "Offer_B.pptx", ("Pricing", "95 per site."))
    _ingest(d, "letters")
    with db.session_scope() as s:
        a, b, rels = _pair(s, "Offer_A.pptx", "Offer_B.pptx")
        lin = s.scalar(select(Version).where(Version.content_sha == a.sha256)).lineage
        assert lin.ordering_confidence == "low"                              # never compacted on a name alone
        t = s.scalar(select(ReviewTask).where(ReviewTask.kind == "lineage_order", ReviewTask.subject_id == lin.id))
        assert "not_the_same_document" in t.options
        hitl.decide(s, Identity("carol", frozenset({"analyst"}), ()), t.id, "not_the_same_document")
    with db.session_scope() as s:
        a, b, rels = _pair(s, "Offer_A.pptx", "Offer_B.pptx")
        assert "REVISION_OF" not in rels and rels["PARTIAL_OVERLAP"].details["shared_slides"] == 11
        va = s.scalar(select(Version).where(Version.content_sha == a.sha256))
        vb = s.scalar(select(Version).where(Version.content_sha == b.sha256))
        assert va.lineage_id != vb.lineage_id
