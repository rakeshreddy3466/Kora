"""Reliability + security primitives: job queue, audit chain, blob store, sandbox."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select


def test_queue_idempotency_leases_fencing_and_dead_letter(fresh_db):
    from kora import clock, db
    from kora.jobs import LeaseLost, claim, complete_in, enqueue, fail, retry_dead
    from kora.models import Job
    with db.session_scope() as s:
        a = enqueue(s, "t.x", {}, key="same")
        b = enqueue(s, "t.x", {}, key="same")
        assert a == b
    j1 = claim("w1", lease_s=10)
    assert j1 and j1.attempts == 1
    assert claim("w2", lease_s=10) is None                 # leased
    clock.advance(seconds=11)                              # w1 "crashed"
    j2 = claim("w2", lease_s=10)
    assert j2.id == j1.id and j2.attempts == 2 and j2.token != j1.token
    with pytest.raises(LeaseLost):                         # zombie w1 cannot complete
        with db.session_scope() as s:
            complete_in(s, j1.id, j1.token)
    assert fail(j1, "late") == "lease_lost"
    assert fail(j2, "boom") == "queued"                   # backoff retry
    clock.advance(hours=2)
    with db.session_scope() as s:
        s.get(Job, j2.id).max_attempts = 3
    j3 = claim("w3")
    assert fail(j3, "boom again") == "dead"
    assert retry_dead(j3.id)
    with db.session_scope() as s:
        assert s.get(Job, j3.id).status == "queued"


def test_handler_work_and_completion_are_atomic(fresh_db):
    from kora import db
    from kora.jobs import Worker, enqueue, handler
    from kora.models import Job, Principal

    @handler("t.atomic")
    def _h(ctx):
        with ctx.tx() as s:
            s.add(Principal(id="zed", display_name="Zed"))
            raise RuntimeError("fail after write")
    with db.session_scope() as s:
        jid = enqueue(s, "t.atomic", {}, max_attempts=1)
    Worker().drain()
    with db.session_scope() as s:
        assert s.get(Principal, "zed") is None            # rolled back with the job
        assert s.get(Job, jid).status == "dead"


def test_audit_chain_detects_edit_reorder_and_truncation(fresh_db):
    from kora import audit, db
    from kora.models import AuditEvent, AuditHead
    with db.session_scope() as s:
        for i in range(5):
            audit.append(s, "alice", "x", "t", str(i), {"i": i})
    with db.session_scope() as s:
        assert audit.verify(s).ok
        s.get(AuditEvent, 3).details = {"i": 99}
    with db.session_scope() as s:
        r = audit.verify(s)
        assert not r.ok and r.first_bad_seq == 3
        s.get(AuditEvent, 3).details = {"i": 2}
    with db.session_scope() as s:
        assert audit.verify(s).ok
        s.delete(s.get(AuditEvent, 5))
    with db.session_scope() as s:
        assert not audit.verify(s).ok                      # head no longer matches


def test_blobstore_encryption_integrity_and_key_validation(tmp_path):
    import os
    from kora.storage.blobstore import BlobIntegrityError, LocalBlobStore
    from kora.storage.crypto import BlobCipher
    st = LocalBlobStore(tmp_path, BlobCipher(os.urandom(32)))
    data = os.urandom(3 * 1024 * 1024 + 17)               # multi-chunk
    ref = st.put_bytes("hot", data)
    assert st.get_bytes("hot", ref.key) == data
    raw = st._path("hot", ref.key)
    assert data[:64] not in raw.read_bytes()              # encrypted at rest
    b = bytearray(raw.read_bytes())
    b[100] ^= 1
    raw.write_bytes(bytes(b))
    with pytest.raises(BlobIntegrityError):
        st.get_bytes("hot", ref.key)
    assert not st.verify("hot", ref.key)
    with pytest.raises(ValueError):
        st.get_bytes("hot", "../../etc/passwd")
    with pytest.raises(ValueError):
        st.put_bytes("nope", b"x")


def test_subprocess_sandbox_matches_inline_and_rejects_corrupt(samples, tmp_path, monkeypatch):
    from dataclasses import replace
    from kora.config import configure, get_settings, load_settings
    from kora.jobs import PermanentError
    from kora.security import sandbox
    monkeypatch.setenv("KORA_DATA_DIR", str(tmp_path))
    configure(replace(load_settings(), sandbox="subprocess", sandbox_timeout_s=120))
    pdf = samples / "sharepoint/network-team/Presentations/Network_Architecture_A.pdf"
    out = tmp_path / "o1"
    out.mkdir()
    r1 = sandbox.run("pdf", {"path": str(pdf)}, out)
    configure(replace(get_settings(), sandbox="inline"))
    out2 = tmp_path / "o2"
    out2.mkdir()
    r2 = sandbox.run("pdf", {"path": str(pdf)}, out2)
    assert [u["unit_hash"] for u in r1["units"]] == [u["unit_hash"] for u in r2["units"]]
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n garbage")
    configure(replace(get_settings(), sandbox="subprocess"))
    with pytest.raises(PermanentError):
        sandbox.run("pdf", {"path": str(bad)}, out)


def test_s3_blobstore_adapter(monkeypatch):
    moto = pytest.importorskip("moto")
    import io
    import os
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    from kora.storage.blobstore import BlobIntegrityError, S3BlobStore
    with moto.mock_aws():
        st = S3BlobStore(None, "kora-test", "us-east-1")
        data = os.urandom(5 * 1024 * 1024 + 3)
        r = st.put_stream("quarantine", io.BytesIO(data))
        assert st.verify("quarantine", r.key) and st.get_bytes("quarantine", r.key) == data
        r2 = st.put_bytes("hot", b"hello")
        b, k = st._k("hot", r2.key)
        st.s3.put_object(Bucket=b, Key=k, Body=b"tampered")
        with pytest.raises(BlobIntegrityError):
            st.get_bytes("hot", r2.key)
