#!/usr/bin/env python3
"""Chaos test for the pipeline: concurrent workers + a hard crash.

  1. fresh database, register the sources, enqueue scans
  2. start N worker processes (short leases)
  3. SIGKILL one worker mid-run (no cleanup, its lease just stops being renewed),
     start a replacement
  4. wait until the queue is empty
  5. verify invariants:
     - every job succeeded (or dead-lettered with a PermanentError for bad input)
     - each distinct content was extracted exactly once (one extract job, units == unit_count)
     - the crashed worker's job was re-claimed (attempts > 1) and completed
     - every lineage has exactly one head; no duplicate relations
     - audit chain verifies
     - a full rescan creates no new extraction work (idempotency)
Usage:
  KORA_DATABASE_URL=postgresql+psycopg://... KORA_DATA_DIR=/tmp/kr \
      python scripts/reliability_check.py SOURCES_DIR [--workers 3]
SOURCES_DIR is a samples-style folder (principals.json + sources.json) or any
folder of files (registered as one SharePoint-like source).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kill-after", type=float, default=4.0)
    ap.add_argument("--timeout", type=float, default=1800)
    a = ap.parse_args()
    os.environ.setdefault("KORA_WORKER_LEASE_S", "8")
    from kora import audit, bootstrap, db
    from kora.config import get_settings
    from kora.models import Base, Content, File, Job, Lineage, Relation, Unit, Version
    from sqlalchemy import func, select

    st = get_settings()
    db.init_engine(st.database_url)
    Base.metadata.drop_all(db.engine())
    shutil.rmtree(st.data_dir / "blobs", ignore_errors=True)
    work = st.data_dir / "sources"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(a.sources, work)
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        if (work / "principals.json").exists():
            bootstrap.load_samples(s, work)
        else:
            bootstrap.upsert_principal(s, {"id": "owner", "display_name": "owner"})
            bootstrap.upsert_source(s, "data", "sharepoint", str(work), "data")
        bootstrap.scan_all(s)

    def spawn():
        return subprocess.Popen([sys.executable, "-m", "kora.cli", "worker"], env=os.environ.copy(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    procs = [spawn() for _ in range(a.workers)]
    time.sleep(a.kill_after)
    # kill a worker that is *holding* a job right now (not one that happens to be idle)
    victim_i, interrupted = 0, None
    deadline = time.time() + 30
    while time.time() < deadline and interrupted is None:
        with db.session_scope() as s:
            for j in s.scalars(select(Job).where(Job.status == "running")):
                pid = (j.lease_owner or "").split(":")[0].rsplit("-", 1)[-1]
                hit = next((i for i, p in enumerate(procs) if str(p.pid) == pid), None)
                if hit is not None:
                    victim_i, interrupted = hit, j.kind
                    break
        if interrupted is None:
            time.sleep(0.2)
    victim = procs[victim_i]
    victim.send_signal(signal.SIGKILL)
    victim.wait()
    killed_at = time.time() - t0
    procs[victim_i] = spawn()

    def pending():
        with db.session_scope() as s:
            return s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued", "running"))))
    idle_since = None
    while time.time() - t0 < a.timeout:
        n = pending()
        if n == 0:
            idle_since = idle_since or time.time()
            if time.time() - idle_since > 3:
                break
        else:
            idle_since = None
        time.sleep(1)
    elapsed = time.time() - t0
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()

    res: dict = {"workers": a.workers, "killed_worker_at_s": round(killed_at, 1), "interrupted_job": interrupted,
                 "drain_s": round(elapsed, 1)}
    with db.session_scope() as s:
        jobs = list(s.scalars(select(Job)))
        res["jobs"] = dict(Counter(j.status for j in jobs))
        dead = [j for j in jobs if j.status == "dead"]
        res["dead_jobs"] = [{"kind": j.kind, "error": (j.last_error or "")[:160]} for j in dead]
        res["retried_jobs"] = [{"kind": j.kind, "attempts": j.attempts} for j in jobs if j.attempts > 1]
        ex = Counter(j.payload.get("sha") for j in jobs if j.kind == "content.extract")
        contents = list(s.scalars(select(Content).where(Content.extracted.is_(True))))
        unit_mismatch = [c.sha256[:12] for c in contents
                         if s.scalar(select(func.count()).select_from(Unit).where(Unit.content_sha == c.sha256)) != c.unit_count]
        heads = Counter(v.lineage_id for v in s.scalars(select(Version).where(Version.is_head.is_(True))))
        n_lin = s.scalar(select(func.count()).select_from(Lineage))
        rels = Counter((r.kind, r.a_sha, r.b_sha) for r in s.scalars(select(Relation)))
        checks = {
            "all_jobs_succeeded_or_permanent": all("PermanentError" in (j.last_error or "") for j in dead),
            "extracted_exactly_once": all(n == 1 for n in ex.values()) and len(ex) == len(contents),
            "units_consistent": not unit_mismatch,
            "crashed_work_recovered": bool(res["retried_jobs"]) and not any(j.status in ("queued", "running") for j in jobs),
            "one_head_per_lineage": all(n == 1 for n in heads.values()) and len(heads) == n_lin,
            "no_duplicate_relations": all(n == 1 for n in rels.values()),
            "audit_chain_valid": audit.verify(s).ok,
        }
        before = s.scalar(select(func.count()).select_from(Job).where(Job.kind == "content.extract"))
        bootstrap.scan_all(s)
    from kora.jobs import Worker
    Worker(lease_s=60).drain()
    with db.session_scope() as s:
        after = s.scalar(select(func.count()).select_from(Job).where(Job.kind == "content.extract"))
        checks["rescan_idempotent"] = after == before
        res["files"] = s.scalar(select(func.count()).select_from(File))
    res["checks"] = checks
    res["ok"] = all(checks.values())
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
