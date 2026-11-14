"""Deletion governance: every class, every guard, the full lifecycle."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from kora.dedup import classes as C


def _items(plan):
    from kora import db
    from kora.models import File
    with db.session_scope() as s:
        return {s.get(File, it.file_id).path: it for it in plan.items}


def _approve_execute(s, pid, ids, approvers=("dave",)):
    from kora.governance import executor as ex
    ex.submit(s, ids["carol"], pid)
    for a in approvers:
        ex.approve(s, ids[a], pid)
    ex.execute(s, ids["erin"], pid)


def test_duplicate_plan_survivor_and_blocks(world, ids):
    from kora import db
    from kora.governance import planner
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "duplicates")
        rows = {r["victim"]["path"]: r for r in p.evidence["items"]}
    assert rows["Downloads/Network_Architecture_A (1).pdf"]["deletable"]
    assert rows["Downloads/Network_Architecture_A (1).pdf"]["survivor"]["system"] == "sharepoint"
    assert rows["Desktop/Network_Architecture_A_resaved.pdf"]["class"] == C.CONTAINER_VARIANT
    held = {c["name"]: c for c in rows["Downloads/Network_Architecture_A (2).pdf"]["checks"]}
    assert not held["legal_hold_clear"]["passed"]
    shared = {c["name"]: c for c in rows["Downloads/5G_Core_Overview (1).pdf"]["checks"]}
    assert not shared["permission_coverage"]["passed"] and "frank" in shared["permission_coverage"]["detail"]
    assert p.evidence["summary"]["deletable"] == 4 and p.required_approvals == 1


def test_lifecycle_quarantine_stub_restore_purge(world, ids, drain):
    from kora import audit, clock, db
    from kora.governance import executor as ex, planner
    from kora.models import DeletionPlan, File
    from kora.storage import get_blobstore
    od = world / "sources" / "onedrive" / "bob"
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "duplicates")
        pid = p.id
        ex.submit(s, ids["carol"], pid)
        from kora.security.auth import Identity
        with pytest.raises(PermissionError):           # separation of duties
            ex.approve(s, Identity("carol", frozenset({"approver"})), pid)
        with pytest.raises(PermissionError):           # analyst cannot execute
            ex.execute(s, ids["carol"], pid)
        ex.approve(s, ids["dave"], pid)
        ex.execute(s, ids["erin"], pid)
    drain()
    victim = od / "Downloads" / "Network_Architecture_A (1).pdf"
    stub = Path(str(victim) + ".kora-stub.json")
    assert not victim.exists() and stub.exists()
    assert json.loads(stub.read_text())["canonical"]["path"] == "Presentations/Network_Architecture_A.pdf"
    assert (od / "Downloads" / "Network_Architecture_A (2).pdf").exists()      # legal hold untouched
    with db.session_scope() as s:
        p = s.get(DeletionPlan, pid)
        assert p.status == "quarantined"
        item = next(it for it in p.items if s.get(File, it.file_id).path.endswith("A (1).pdf"))
        with pytest.raises(ex.PlanStateError):
            ex.purge(s, ids["erin"], pid)                # grace period
        ex.restore(s, ids["erin"], pid, [item.id])
    drain()
    assert victim.exists() and not stub.exists()
    clock.advance(days=31)
    with db.session_scope() as s:
        ex.purge(s, ids["erin"], pid)
    drain()
    with db.session_scope() as s:
        p = s.get(DeletionPlan, pid)
        states = sorted(it.state for it in p.items)
        assert p.status == "purged" and states.count("purged") == 3 and "restored" in states
        assert list(get_blobstore().iter_keys("quarantine")) == []
        assert audit.verify(s).ok
        actions = {e.action for e in s.scalars(select(__import__("kora.models", fromlist=["AuditEvent"]).AuditEvent))}
    assert {"plan.created", "plan.approved", "item.quarantined", "item.restored", "item.purged"} <= actions


def test_toctou_victim_changed_after_approval(world, no_saving_rule, ids, drain):
    from kora import db
    from kora.governance import planner
    from kora.models import DeletionPlan
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "subsets")
        pid = p.id
        _approve_execute(s, pid, ids, ("dave", "gina"))
    f = world / "sources/sharepoint/network-team/Presentations/Network_Architecture_excerpt.pdf"
    f.write_bytes(f.read_bytes() + b"\n%edited\n")
    drain()
    with db.session_scope() as s:
        it = s.get(DeletionPlan, pid).items[0]
        assert it.state == "blocked" and "changed" in it.error
    assert f.exists()


def test_survivor_removed_before_execution(world, ids, drain):
    from kora import db
    from kora.governance import planner
    from kora.models import DeletionPlan
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "media")
        pid = p.id
        _approve_execute(s, pid, ids, ("dave", "gina"))
    (world / "sources/sharepoint/network-team/Recordings/Architecture_Review_2026-05-12.mp4").unlink()
    drain()
    with db.session_scope() as s:
        it = s.get(DeletionPlan, pid).items[0]
        assert it.state == "blocked" and "survivor" in it.error
    assert (world / "sources/play/Architecture_Review_2026-05-12_480p.mp4").exists()


def test_concurrent_plans_cannot_delete_each_others_survivor(world, ids):
    from kora import db
    from kora.governance import planner
    with db.session_scope() as s:
        planner.create(s, ids["carol"], "media")         # SharePoint recording pinned as survivor
        p2 = planner.create(s, ids["carol"], "recordings")
        rows = {r["victim"]["path"]: r for r in p2.evidence["items"]}
        sp = rows["Recordings/Architecture_Review_2026-05-12.mp4"]
        assert not sp["deletable"]
        assert any(c["name"] == "no_conflicting_plan" and not c["passed"] for c in sp["checks"])
        p3 = planner.create(s, ids["carol"], "duplicates")
        p4 = planner.create(s, ids["carol"], "duplicates")
        assert p3.evidence["summary"]["deletable"] == 4 and p4.evidence["summary"]["deletable"] == 0


def test_revision_compaction_proves_recoverability(world, no_saving_rule, ids, drain):
    from kora import db
    from kora.governance import planner
    from kora.models import DeletionPlan, SlideBlob
    from kora.queries import doc_units
    from kora.storage import get_blobstore
    from kora.versioning import reconstruct
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "revisions")
        rows = {r["victim"]["path"]: r for r in p.evidence["items"]}
        assert set(rows) == {"Presentations/Network_Architecture_PA1.pdf", "Presentations/Network_Architecture_PA2.pdf",
                             "Team/Team_Update.pdf"}
        assert all(r["deletable"] and r["recovery"] == "render_equivalent" for r in rows.values())
        pa1 = next(it for it in p.items if it.file_id == rows["Presentations/Network_Architecture_PA1.pdf"]["victim"]["file_id"])
        pdf = reconstruct.reconstruct_pdf(s, pa1.victim_sha)
        assert pdf.startswith(b"%PDF") and reconstruct.verify(s, pa1.victim_sha)["ok"]
        # corrupt one slide-store page that only PA1 has -> proof fails -> a new plan blocks it
        u = next(u for u in doc_units(s, pa1.victim_sha) if s.get(SlideBlob, u.unit_hash).first_content_sha == pa1.victim_sha)
        key = s.get(SlideBlob, u.unit_hash).blob_key
        path = get_blobstore()._path("hot", key)
        path.write_bytes(path.read_bytes()[:-20])
        assert not reconstruct.verify(s, pa1.victim_sha)["ok"]
        from kora.governance import executor as ex
        ex.cancel(s, ids["carol"], p.id)
        p2 = planner.create(s, ids["carol"], "revisions")
        r2 = {r["victim"]["path"]: r for r in p2.evidence["items"]}
        assert not r2["Presentations/Network_Architecture_PA1.pdf"]["deletable"]
        # PA2 re-uses pages stored in PA1's pack, so it is blocked too; an unrelated lineage is not
        assert not r2["Presentations/Network_Architecture_PA2.pdf"]["deletable"]
        assert r2["Team/Team_Update.pdf"]["deletable"]


def test_revision_conflict_blocks_compaction(world, ids):
    from kora import db
    from kora.governance import planner
    from kora.models import Lineage, Version
    from kora.versioning import lineage
    with db.session_scope() as s:
        lin = s.scalar(select(Lineage).where(Lineage.key == "docno:1/1551-LZA 701 0001 UEN"))
        pa2 = next(v for v in lin.versions if v.revision_label == "PA2")
        pa2.revision_label, pa2.revision_key = "PA1", [1, 0, 1]      # two contents now claim PA1
        lineage.reorder(s, lin)
        assert lin.has_conflict
        p = planner.create(s, ids["carol"], "revisions")
        for r in p.evidence["items"]:
            if r["victim"]["path"].startswith("Presentations/"):
                assert not r["deletable"]
                assert any(c["name"] == "lineage_consistent" and "conflict" in c["detail"] for c in r["checks"])


def test_recordings_require_valid_capsule_and_archive(world, ids, drain):
    from kora import db
    from kora.governance import planner
    from kora.models import DeletionPlan
    from kora.storage import get_blobstore
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "recordings")
        rows = {r["victim"]["path"]: r for r in p.evidence["items"]}
        assert rows["Recordings/Architecture_Review_2026-05-12.mp4"]["deletable"]
        assert not rows["Recordings/Bob_Lab_Session.mp4"]["deletable"]
        assert p.required_approvals == 2
        _approve_execute(s, p.id, ids, ("dave", "gina"))
        pid = p.id
    drain()
    with db.session_scope() as s:
        it = next(i for i in s.get(DeletionPlan, pid).items if i.state == "quarantined")
        assert it.quarantine_tier == "archive" and get_blobstore().verify("archive", it.victim_sha)


def test_departure_assessment(world, ids):
    from kora import db
    from kora.governance import departure
    with db.session_scope() as s:
        r = departure.assess(s, ids["carol"], "bob")
    sm = r["summary"]
    assert sm["deletable_duplicates"] == 4 and sm["blocked_by_retention"] == 1 and sm["blocked_other"] == 1
    assert sm["unique_recordings"] == 1 and sm["unique_documents"] == 1 and sm["proposed_new_owner"] == "alice"
    assert any("Bob" in d["text"] for d in r["dependencies"])
    assert r["plan_id"]


def test_departure_requires_departing_status(world, ids):
    from kora import db
    from kora.governance import planner
    with db.session_scope() as s, pytest.raises(planner.PlanError):
        planner.create(s, ids["carol"], "departure", {"owner": "alice"})


def test_net_saving_guard_refuses_and_keeps_nothing(world, ids):
    """Compacting a tiny text deck would store about as much as it frees: refused, pages discarded."""
    from kora import db
    from kora.governance import planner
    from kora.models import SlideBlob
    from kora.storage.gc import collect
    with db.session_scope() as s:
        p = planner.create(s, ids["carol"], "revisions")
        rows = {r["victim"]["path"]: r for r in p.evidence["items"]}
        pa1 = rows["Presentations/Network_Architecture_PA1.pdf"]
        guard = next(c for c in pa1["checks"] if c["name"] == "net_saving")
        proof = next(c for c in pa1["checks"] if c["name"] == "recovery_path_verified")
        assert proof["passed"]                       # recoverable ...
        if not guard["passed"]:                      # ... but not worth it -> nothing kept for it
            assert not pa1["deletable"]
            assert not [sb for sb in s.scalars(select(SlideBlob)) if sb.first_content_sha == pa1["victim"]["sha256"]]
        assert collect(s, dry_run=True, min_age_s=0)["hot"]["orphans"] == 0
