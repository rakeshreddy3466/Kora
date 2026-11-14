"""Manual deletion: a person picks files; KORA explains, keeps the knowledge, and governs it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from kora.dedup import classes as C


def _fid(s, path):
    from kora.models import File
    return s.scalar(select(File).where(File.path == path)).id


def _bob():
    from kora.security.auth import Identity
    return Identity("bob", frozenset({"viewer"}), ("network-team",))


def test_owner_selects_files_and_kora_explains_each(world, ids):
    from kora import db
    from kora.governance import planner
    with db.session_scope() as s:
        sel = [_fid(s, p) for p in ("Notes/Bob_Lab_Findings.pdf", "Downloads/Network_Architecture_A (1).pdf",
                                    "Projects/Onboarding (1).pptx", "Downloads/Network_Architecture_A (2).pdf")]
        p = planner.create(s, _bob(), "manual", {"file_ids": sel}, "Bob cleans up his OneDrive")
        rows = {r["victim"]["path"]: r for r in p.evidence["items"]}
        assert p.created_by == "bob" and p.required_approvals == 2
    # a copy exists elsewhere -> the stronger, machine-provable class is used
    assert rows["Downloads/Network_Architecture_A (1).pdf"]["class"] == C.EXACT_COPY
    assert rows["Downloads/Network_Architecture_A (1).pdf"]["survivor"]["system"] == "sharepoint"
    assert rows["Projects/Onboarding (1).pptx"]["class"] == C.EXACT_COPY
    # unique content -> MANUAL_DELETE, recovery = knowledge capsule, impact spelled out
    uniq = rows["Notes/Bob_Lab_Findings.pdf"]
    assert uniq["class"] == C.MANUAL_DELETE and uniq["recovery"] == "knowledge_capsule" and uniq["deletable"]
    chk = {c["name"]: c for c in uniq["checks"]}
    assert chk["knowledge_captured"]["passed"] and "searchable and AI-ready" in chk["knowledge_captured"]["detail"]
    assert not chk["sole_copy"]["passed"] and not chk["sole_copy"]["hard"]
    assert uniq["readiness"] < 100                                      # soft warnings lower confidence
    # a legal hold still wins over a person's wish
    held = {c["name"]: c for c in rows["Downloads/Network_Architecture_A (2).pdf"]["checks"]}
    assert not held["legal_hold_clear"]["passed"] and not rows["Downloads/Network_Architecture_A (2).pdf"]["deletable"]


def test_only_owner_or_analyst_may_ask(world, ids):
    from kora import db
    from kora.governance import planner
    from kora.governance.planner import PlanError
    with db.session_scope() as s:
        fid = _fid(s, "Notes/Bob_Lab_Findings.pdf")
        with pytest.raises(PermissionError):
            planner.create(s, ids["frank"], "manual", {"file_ids": [fid]})
        with pytest.raises(PlanError):
            planner.create(s, ids["carol"], "manual", {})
        assert planner.create(s, ids["carol"], "manual", {"file_ids": [fid]}).items


def test_selecting_the_team_copy_is_blocked_by_permission_coverage(world, ids):
    """Bob may not delete the SharePoint original just because he has a private copy: the team would lose it."""
    from kora import db
    from kora.governance import planner
    with db.session_scope() as s:
        fid = _fid(s, "Presentations/Network_Architecture_A.pdf")
        p = planner.create(s, ids["carol"], "manual", {"file_ids": [fid]})
        row = p.evidence["items"][0]
    assert row["class"] == C.EXACT_COPY and not row["deletable"]
    assert "alice" in {c["name"]: c for c in row["checks"]}["permission_coverage"]["detail"]


def test_manual_delete_keeps_knowledge_searchable_and_is_restorable(world, ids, drain):
    from kora import db
    from kora.governance import executor as ex, planner
    from kora.search.index import search
    target = world / "sources" / "onedrive" / "bob" / "Notes" / "Bob_Lab_Findings.pdf"
    with db.session_scope() as s:
        fid = _fid(s, "Notes/Bob_Lab_Findings.pdf")
        p = planner.create(s, _bob(), "manual", {"file_ids": [fid]})
        pid = p.id
        ex.submit(s, _bob(), pid)
        ex.approve(s, ids["dave"], pid)
        ex.approve(s, ids["gina"], pid)
        ex.execute(s, ids["erin"], pid)
    drain()
    stub = Path(str(target) + ".kora-stub.json")
    assert not target.exists() and stub.exists()
    st = json.loads(stub.read_text())
    assert st["reason"] == C.MANUAL_DELETE and st["recovery"] == "knowledge_capsule" and st["canonical"] is None
    with db.session_scope() as s:
        hits = search(s, _bob(), "replace batch 7 before pilot", 5)["results"]
        assert hits and any("knowledge capsule" in (h.get("original") or "") for h in hits)
        item = s.get(__import__("kora.models", fromlist=["DeletionPlan"]).DeletionPlan, pid).items[0]
        ex.restore(s, ids["erin"], pid, [item.id])
    drain()
    assert target.exists() and not stub.exists()
