"""Dashboard read models, owner self-service over HTTP, AI context packs and the MCP server."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(world):
    from kora import bootstrap, db
    from kora.api.app import create_app
    from kora.security.auth import create_key
    keys = {}
    with db.session_scope() as s:
        bootstrap.upsert_principal(s, {"id": "maria", "display_name": "Maria DPO", "groups": ["privacy"]})
        s.flush()
        for pid, roles in (("carol", ["analyst", "viewer"]), ("bob", ["viewer"]), ("frank", ["viewer"]),
                           ("dave", ["approver"]), ("gina", ["approver", "auditor"]), ("erin", ["operator"]),
                           ("maria", ["dpo"])):
            keys[pid] = create_key(s, pid, roles)
    client = TestClient(create_app(init=False))
    client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
    client.keys = keys
    return client


def test_ui_is_served_with_a_strict_csp(api):
    r = api.get("/ui/")
    assert r.status_code == 200 and "app.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "connect-src 'self'" in csp
    assert api.get("/ui/app.js").headers["content-type"].startswith("text/javascript")
    assert api.get("/ui/..%2Fapi%2Fapp.py").status_code == 404
    assert api.get("/ui/secret.py").status_code == 404


def test_inventory_and_overview(api):
    me = api.get("/v1/me", headers=api.h("bob")).json()
    assert me["id"] == "bob" and me["roles"] == ["viewer"]
    files = {f["path"]: f for f in api.get("/v1/inventory", headers=api.h("bob")).json()["files"]}
    a1 = files["Downloads/Network_Architecture_A (1).pdf"]
    assert a1["copies"] == 3 and a1["version"]["is_head"] is False
    assert "Presentations/Network_Architecture_A.pdf" in files            # bob is in the network team
    frank = {f["path"] for f in api.get("/v1/inventory", headers=api.h("frank")).json()["files"]}
    assert frank == {"Downloads/5G_Core_Overview (1).pdf"}                  # ACL-filtered
    assert api.get("/v1/overview", headers=api.h("bob")).status_code == 403
    o = api.get("/v1/overview", headers=api.h("carol")).json()
    assert o["savings"]["active_bytes"] > 0 and o["relations"]["EXACT_COPY"] >= 3 and o["audit"]["ok"]


def test_owner_self_service_over_http(api):
    files = {f["path"]: f for f in api.get("/v1/inventory", headers=api.h("bob")).json()["files"]}
    fid = files["Notes/Bob_Lab_Findings.pdf"]["id"]
    r = api.post("/v1/plans", json={"kind": "manual", "scope": {"file_ids": [fid]}}, headers=api.h("bob"))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert api.get(f"/v1/plans/{pid}", headers=api.h("bob")).status_code == 200            # own plan
    assert api.get(f"/v1/plans/{pid}", headers=api.h("frank")).status_code == 404          # not someone else's
    assert [p["id"] for p in api.get("/v1/plans", headers=api.h("bob")).json()["plans"]] == [pid]
    assert api.post(f"/v1/plans/{pid}/submit", headers=api.h("bob")).status_code == 200
    assert api.post(f"/v1/plans/{pid}/approve", json={}, headers=api.h("bob")).status_code == 403
    r = api.post("/v1/plans", json={"kind": "manual", "scope": {"file_ids": [fid]}}, headers=api.h("frank"))
    assert r.status_code == 403


def test_ai_context_respects_opt_in_permissions_and_pii(api):
    r = api.get("/v1/ai/context", params={"q": "edge data centre migration"}, headers=api.h("carol")).json()
    assert r["chunks"] == [] and r["excluded"]["source_not_opted_in"] > 0
    assert api.post("/v1/sources/sp-network-team/ai-use", json={"allowed": True, "reason": "test"},
                    headers=api.h("carol")).status_code == 403                          # DPO decision only
    assert api.post("/v1/sources/sp-network-team/ai-use", json={"allowed": True, "reason": "DPIA done"},
                    headers=api.h("maria")).status_code == 200
    r = api.get("/v1/ai/context", params={"q": "edge data centre migration"}, headers=api.h("carol")).json()
    assert r["chunks"], r
    for ch in r["chunks"]:
        assert ch["cite"]["where"] and ch["label"]
        assert "Alice Andersson" not in ch["text"]                                      # people's names redacted
        assert ch["speaker"] is None or ch["speaker"].startswith("person-")
    assert "[1]" in r["context_block"] and "Cite them as [n]" in r["instructions"]
    ev = api.get("/v1/audit", params={"limit": 5}, headers=api.h("gina")).json()["events"]
    served = next(e for e in ev if e["action"] == "ai.context_served")
    assert "edge" not in json.dumps(served)                                            # the question is not logged
    nobody = api.get("/v1/ai/context", params={"q": "edge data centre migration"}, headers=api.h("frank")).json()
    assert nobody["chunks"] == []                                                       # frank cannot read the team site


def _mcp(world, key, msgs):
    env = {**os.environ, "KORA_DATA_DIR": str(world), "KORA_API_KEY": key, "KORA_SANDBOX": "inline"}
    p = subprocess.run([sys.executable, "-m", "kora.cli", "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=120)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]                 # every stdout line must be protocol JSON


def test_mcp_server_speaks_the_protocol(api, world):
    from kora import db
    from kora.models import Source
    with db.session_scope() as s:
        src = s.get(Source, "sp-network-team")
        src.config = {**src.config, "ai_use_allowed": True}
    out = _mcp(world, api.keys["carol"], [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_knowledge", "arguments": {"query": "edge data centre migration"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "document_versions", "arguments": {"doc_number": "LZA 701 0001"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nope"},
    ])
    by = {m["id"]: m for m in out}
    assert by[1]["result"]["serverInfo"]["name"] == "kora"
    assert {t["name"] for t in by[2]["result"]["tools"]} == {"search_knowledge", "document_versions", "get_capsule"}
    res = by[3]["result"]
    assert not res["isError"] and "[1]" in res["structuredContent"]["passages"]
    lin = by[4]["result"]["structuredContent"]["lineages"][0]
    assert [v["revision"] for v in lin["versions"] if v["current"]] == ["B"]
    assert by[5]["error"]["code"] == -32601
    bad = _mcp(world, "kora_invalid", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "search_knowledge", "arguments": {"query": "edge"}}}])
    assert bad[0]["result"]["isError"]


def test_recordings_capsule_slide_description_and_rebuild(api, drain):
    recs = api.get("/v1/recordings", headers=api.h("carol")).json()["recordings"]
    big = next(r for r in recs if r["name"] == "Architecture_Review_2026-05-12.mp4")
    assert big["capsule"]["valid"] and big["transcript"]["source"].startswith("sidecar")
    assert big["transcript"]["speakers"] and big["frames"] >= 5
    d = api.get(f"/v1/recordings/{big['content']}", headers=api.h("carol")).json()
    linked = [f for f in d["frames"] if f["slide"]]
    assert linked and linked[0]["slide"]["doc_number"]                      # frame -> document number + slide
    assert any(sg["frame"] is not None for sg in d["segments"])            # "as you see here" -> frame
    assert api.get(f"/v1/recordings/{big['content']}", headers=api.h("frank")).status_code == 404
    # describe one slide picture as text and recreate it from that text
    fr = max(d["frames"], key=lambda f: len(f["text"]))
    sp = api.get(f"/v1/units/{fr['id']}/spec", headers=api.h("carol")).json()
    m = sp["measured"]
    assert sp["prompt"].startswith("A ") and m["prompt_bytes"] < m["frame_bytes"] / 10
    assert m["text_recall"] is None or m["text_recall"] >= 0.6
    img = api.get(f"/v1/blobs/{sp['image']}", headers=api.h("carol"))
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    # recreate the recording as a video from the capsule alone
    assert api.post(f"/v1/recordings/{big['content']}/rebuild", headers=api.h("carol")).status_code == 202
    drain()
    rb = api.get(f"/v1/recordings/{big['content']}", headers=api.h("carol")).json()["summary"]["rebuilt"]
    assert rb and rb["subtitles"] and rb["bytes"] < big["size"] / 10
    v = api.get(f"/v1/blobs/{rb['key']}", headers=api.h("carol"))
    assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
    vtt = api.get(f"/v1/recordings/{big['content']}/transcript.vtt", headers=api.h("carol")).text
    assert vtt.startswith("WEBVTT") and "<v " in vtt
    ratios = api.get("/v1/scenarios", headers=api.h("carol")).json()["ratios"]
    assert 0.5 < ratios["video_compression"]["ratio"] < 1 and ratios["duplicate_copy"]["ratio"] == 1.0


@pytest.mark.skipif(not os.environ.get("KORA_SHERPA_DIR"), reason="speech models not downloaded (kora fetch-models)")
def test_offline_whisper_with_speaker_turns(tmp_path):
    from pathlib import Path

    from kora.extraction import asr_sherpa
    wav = Path(os.environ["KORA_SHERPA_DIR"]) / "sherpa-onnx-whisper-small" / "test_wavs" / "0.wav"
    if not wav.exists():
        pytest.skip("no test clip shipped with the model")
    segs, info = asr_sherpa.transcribe(wav, {"sherpa_dir": os.environ["KORA_SHERPA_DIR"]})
    assert segs and segs[0]["text"] and info["engine"].startswith("sherpa-onnx")


def test_sandbox_gives_recordings_a_longer_limit():
    import os
    from types import SimpleNamespace
    from kora.security.sandbox import limits_for
    st = SimpleNamespace(sandbox_timeout_s=900, media_timeout_s=7200)
    assert limits_for("pdf", st) == (900, 900 * (os.cpu_count() or 1))
    wall, cpu = limits_for("video", st)
    assert wall == 7200 and cpu >= wall
    assert limits_for("optimize_video", st)[0] == 7200


def test_restarted_dashboard_frees_jobs_of_a_dead_process(fresh_db):
    import subprocess
    import sys
    from kora import db
    from kora.jobs import enqueue
    from kora.jobs.queue import claim, release_orphans
    from kora.models import Job
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    with db.session_scope() as s:
        jid = enqueue(s, "demo.noop", {"n": 1}, key="orphan-test")
    held = claim(f"dashboard0-{dead.pid}", kinds=["demo.noop"], lease_s=600)
    assert held and held.id == jid
    assert claim("other", kinds=["demo.noop"]) is None            # its lease still looks valid
    assert release_orphans("dashboard") == 1
    again = claim("dashboard0-999999999", kinds=["demo.noop"])
    assert again and again.id == jid and again.attempts == 2
    with db.session_scope() as s:
        assert s.get(Job, jid).status == "running"


def test_api_docs_offer_an_authorize_button(api):
    client = api
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["KORA API key"]["scheme"] == "bearer"
    tags = {t for p in spec["paths"].values() for m in p.values() for t in m.get("tags", [])}
    assert {"Deletion plans", "Recordings", "Privacy (GDPR)"} <= tags
    assert client.get("/docs").status_code == 200
    assert client.get("/v1/me").status_code == 401                         # still locked without a key
    assert client.get("/v1/me", headers=client.h("carol")).status_code == 200


def test_mcp_config_prints_ready_settings_with_a_read_only_key(fresh_db, capsys):
    import argparse
    import json as _json
    from kora import bootstrap, db
    from kora.cli import cmd_mcp_config
    from kora.models import ApiKey
    with db.session_scope() as s:
        bootstrap.upsert_principal(s, {"id": "sara", "display_name": "Sara"})
    cmd_mcp_config(argparse.Namespace(as_="sara", docker=False, compose="docker-compose.yml"))
    out = capsys.readouterr().out
    block = _json.loads(out[out.index("{"):out.index("\n}\n") + 2])
    kora = block["mcpServers"]["kora"]
    assert kora["args"] == ["mcp"] and kora["env"]["KORA_API_KEY"].startswith("kora_")
    assert "claude mcp add --env KORA_API_KEY=" in out and "--transport stdio kora -- " in out
    with db.session_scope() as s:
        assert s.query(ApiKey).filter_by(principal_id="sara").one().roles == ["viewer"]
