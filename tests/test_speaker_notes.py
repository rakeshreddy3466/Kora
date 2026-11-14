"""Speaker notes: "Slide responsible: <name>  Best Before: <date>" (as found in the partner's decks)."""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select


@pytest.mark.parametrize("notes, people, best_before", [
    ("Title\nSlide responsible: Alice Andersson\tBest Before: April 2020\n\nBody", ["Alice Andersson"], date(2020, 4, 30)),
    ("Owner: Bob Berg, Carol Chen; valid until 2026-03-31", ["Bob Berg", "Carol Chen"], date(2026, 3, 31)),
    ("Slide owner: José Núñez / Anna van der Berg\nExpires: Q2 2021", ["José Núñez", "Anna van der Berg"], date(2021, 6, 30)),
    ("Ansvarig: Åsa Öberg  Bäst före: maj 2021", ["Åsa Öberg"], date(2021, 5, 31)),
    ("Responsible: TBD\nBest before: 30 April 2020", [], date(2020, 4, 30)),
    ("Who is responsible for the network? The operator.", [], None),
])
def test_parse_owner_and_best_before(notes, people, best_before):
    from kora.knowledge.notes import parse
    info = parse(notes)
    assert info.people == people and info.best_before == best_before


def _onboarding(s):
    from kora.models import Content, File
    old = s.scalar(select(File).where(File.filename == "Onboarding.pptx")).sha256
    new = s.scalar(select(File).where(File.filename == "Onboarding_v2.pptx")).sha256
    return s.get(Content, old), s.get(Content, new)


def test_notes_become_units_with_one_copy_only(world):
    from kora import db
    from kora.models import Unit
    with db.session_scope() as s:
        old, new = _onboarding(s)
        notes = list(s.scalars(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "note")))
        assert len(notes) == 4 and all(n.meta["people"] == ["Bob Berg"] for n in notes)
        assert {n.meta["best_before"] for n in notes} == {"2020-04-30"}
        slides = list(s.scalars(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "slide")))
        assert all("notes" not in (u.meta or {}) and u.meta["has_notes"] for u in slides)   # no second copy
        assert old.meta["speaker_notes"]["best_before"] == "2020-04-30"
        assert new.meta["speaker_notes"]["best_before"] == "2030-12-31"


def test_privacy_review_and_freshness_review(world):
    from kora import db
    from kora.models import ReviewTask
    with db.session_scope() as s:
        old, new = _onboarding(s)
        pii = s.scalar(select(ReviewTask).where(ReviewTask.key == f"pii:{old.sha256}"))
        assert pii.evidence["counts"]["NAME_IN_NOTES"] == 4 and pii.required_role == "dpo"
        assert any(x["kind"] == "NAME_IN_NOTES" and x["unit"] == "note" for x in pii.evidence["samples"])
        stale = s.scalar(select(ReviewTask).where(ReviewTask.kind == "expired_content",
                                                  ReviewTask.subject_id == old.sha256))
        assert stale and stale.options == ["retire", "ask_owner_to_update", "keep_as_is"]
        assert stale.evidence["slides_past_best_before"] == 4
        assert not s.scalar(select(ReviewTask).where(ReviewTask.kind == "expired_content",
                                                     ReviewTask.subject_id == new.sha256))   # valid until 2030


def test_search_finds_notes_and_flags_stale_slides(world, ids):
    from kora import db
    from kora.search.index import search
    with db.session_scope() as s:
        hits = search(s, ids["carol"], "talk track keep it short", 5)["results"]
        top = hits[0]
        assert top["type"] == "speaker_notes" and top["past_best_before"] and top["best_before"] == "2020-04-30"


def test_ai_context_redacts_the_slide_owner_and_says_it_is_stale(world, ids):
    from kora import db
    from kora.ai.context import context
    from kora.models import Source
    with db.session_scope() as s:
        for src in s.scalars(select(Source)):
            src.config = {**src.config, "ai_use_allowed": True}
        r = context(s, ids["carol"], "talk track keep it short", 3)
    notes = [c for c in r["chunks"] if c["cite"]["type"] == "speaker_notes"]
    assert notes and all("Bob Berg" not in c["text"] for c in notes)
    assert "past its best-before date" in notes[0]["label"] and "best-before" in r["instructions"]


def test_access_export_erasure_and_leaver_cover_notes(world, ids):
    from kora import db, hitl
    from kora.governance import departure
    from kora.models import Unit
    from kora.privacy import dsar
    from kora.security.auth import Identity
    with db.session_scope() as s:
        old, _ = _onboarding(s)
        rep = departure.assess(s, ids["carol"], "bob", create_plan=False)
        assert rep["summary"]["slides_naming_them_responsible"] == 4
        assert rep["slide_ownership"][0]["slides"] == [1, 2, 3, 4]
        exp = dsar.export(s, Identity("gina", frozenset({"dpo"}), ()), "bob")
        named = [m for m in exp["mentions"] if m["kind"] == "note"]
        assert len(named) >= 4 and all(m["named_as_slide_responsible"] for m in named)
        t = dsar.request_erasure(s, Identity("carol", frozenset({"dpo"}), ()), "bob", "left the company")
        hitl.decide(s, Identity("gina", frozenset({"dpo"}), ()), t.id, "approve_erasure")
        for u in s.scalars(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "note")):
            assert "Bob Berg" not in u.text and u.meta["people"][0].startswith("person-")


def test_upgrade_of_decks_read_by_an_older_kora(world):
    """Data extracted before this feature still has notes inside the slide metadata."""
    from kora import db
    from kora.knowledge import notes
    from sqlalchemy import delete
    from kora.models import Content, Posting, Unit
    with db.session_scope() as s:
        old, _ = _onboarding(s)
        s.execute(delete(Posting).where(Posting.unit_id.in_(
            select(Unit.id).where(Unit.content_sha == old.sha256, Unit.kind == "note"))))
        for n in s.scalars(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "note")):
            sl = s.scalar(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "slide",
                                             Unit.ordinal == n.ordinal))
            sl.meta = {k: v for k, v in sl.meta.items() if k not in ("has_notes", "best_before")} | {"notes": n.text}
            s.delete(n)
        old.meta = {k: v for k, v in old.meta.items() if k != "speaker_notes"}
    with db.session_scope() as s:
        assert notes.backfill(s) >= 1
        assert notes.backfill(s) == 0                                     # runs once
        assert len(list(s.scalars(select(Unit).where(Unit.content_sha == old.sha256, Unit.kind == "note")))) == 4
        assert s.get(Content, old.sha256).meta["speaker_notes"]["slides_with_notes"] == 4


def test_deleting_a_deck_warns_about_notes_found_nowhere_else(world):
    from kora import db
    from kora.governance.checks import _notes_check
    from kora.models import DeletionItem
    with db.session_scope() as s:
        old, new = _onboarding(s)
        same = _notes_check(s, DeletionItem(victim_sha=old.sha256, survivor_sha=old.sha256))
        assert same["passed"] and not same["hard"]
        other = _notes_check(s, DeletionItem(victim_sha=old.sha256, survivor_sha=new.sha256))
        assert not other["passed"] and not other["hard"] and "found nowhere else" in other["detail"]
    assert json.dumps(other)
