"""HTTP API: authentication, authorisation, ACL filtering, plan workflow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(world):
    from kora import db
    from kora.api.app import create_app
    from kora.security.auth import create_key
    keys = {}
    with db.session_scope() as s:
        for pid, roles in (("carol", ["analyst", "viewer"]), ("alice", ["viewer"]), ("frank", ["viewer"]),
                           ("dave", ["approver"]), ("gina", ["approver", "auditor"]), ("erin", ["operator"])):
            keys[pid] = create_key(s, pid, roles)
    client = TestClient(create_app(init=False))
    client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
    return client


def test_auth_and_security_headers(api):
    assert api.get("/healthz").status_code == 200
    r = api.get("/v1/lineages")
    assert r.status_code == 401
    r = api.get("/v1/lineages", headers={"Authorization": "Bearer kora_wrong"})
    assert r.status_code == 401
    r = api.get("/v1/lineages", headers=api.h("alice"))
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in r.headers and r.headers["cache-control"] == "no-store"


def test_roles_enforced(api):
    assert api.post("/v1/plans", json={"kind": "duplicates"}, headers=api.h("alice")).status_code == 403
    assert api.get("/v1/audit/verify", headers=api.h("carol")).status_code == 403
    assert api.get("/v1/audit/verify", headers=api.h("gina")).json()["ok"] is True
    assert api.get("/v1/jobs", headers=api.h("alice")).status_code == 403


def test_acl_filtered_views(api):
    alice = {l["key"] for l in api.get("/v1/lineages", headers=api.h("alice")).json()["lineages"]}
    frank = {l["key"] for l in api.get("/v1/lineages", headers=api.h("frank")).json()["lineages"]}
    assert "docno:1/1551-LZA 701 0001 UEN" in alice
    assert frank == {"docno:1/1551-LZA 701 0002 UEN"}         # only the copy shared with him
    hits = api.get("/v1/search", params={"q": "dual-homed aggregation decision"}, headers=api.h("alice")).json()
    assert hits["results"]
    cap_sha = hits["results"][0]["content"]
    assert api.get(f"/v1/capsules/{cap_sha}", headers=api.h("alice")).status_code == 200
    assert api.get(f"/v1/capsules/{cap_sha}", headers=api.h("frank")).status_code == 404


def test_plan_workflow_over_http(api, drain):
    r = api.post("/v1/plans", json={"kind": "duplicates"}, headers=api.h("carol"))
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["summary"]["deletable"] == 4
    assert api.post(f"/v1/plans/{pid}/approve", json={}, headers=api.h("dave")).status_code == 409   # not submitted
    assert api.post(f"/v1/plans/{pid}/submit", headers=api.h("carol")).status_code == 200
    assert api.post(f"/v1/plans/{pid}/execute", headers=api.h("erin")).status_code == 409             # not approved
    assert api.post(f"/v1/plans/{pid}/approve", json={"comment": "ok"}, headers=api.h("dave")).json()["status"] == "approved"
    assert api.post(f"/v1/plans/{pid}/execute", headers=api.h("erin")).status_code == 202
    drain()
    body = api.get(f"/v1/plans/{pid}", headers=api.h("gina")).json()
    assert body["status"] == "quarantined"
    assert sum(1 for i in body["items"] if i["state"] == "quarantined") == 4
    sav = api.get("/v1/reports/savings", headers=api.h("carol")).json()
    assert sav["realized"]["EXACT_COPY"]["items"] == 3
