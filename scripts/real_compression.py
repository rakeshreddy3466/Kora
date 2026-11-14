#!/usr/bin/env python3
"""Governed compression on an ingested real corpus (no transcript dependency).

  1. rescan (plans are always built on fresh source state)
  2. produce + measure renditions for every candidate (video transcode, PDF image recompression)
  3. build a `compress` plan; two approvers; execute; verify the files at the source
  4. print measured savings next to the dedup findings

Usage:  KORA_DATA_DIR=<dir> python scripts/real_compression.py [--no-execute]
Writes <data_dir>/reports/real_compression.json (file names only, no personal data from contents).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

from sqlalchemy import select

from kora import bootstrap, db
from kora.config import get_settings
from kora.governance import executor as ex, planner
from kora.jobs import Worker, enqueue
from kora.models import Content, DeletionPlan, File, Rendition, Source
from kora.optimization.engine import candidates
from kora.security.auth import Identity

ANALYST = Identity("data-owner", frozenset({"analyst", "viewer"}), ("hackathon",))
APPROVER_1 = Identity("rev-anna", frozenset({"approver"}), ("hackathon",))
APPROVER_2 = Identity("rev-johan", frozenset({"approver"}), ("hackathon",))
OPERATOR = Identity("op-erik", frozenset({"operator"}), ())


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-execute", action="store_true", help="measure and plan only")
    args = ap.parse_args()
    bootstrap.init(json_logs=False)
    logging.getLogger().setLevel(logging.WARNING)
    out: dict = {"renditions": []}
    with db.session_scope() as s:
        for pid, name in (("rev-anna", "Anna Reviewer"), ("rev-johan", "Jagadish Reviewer"), ("op-erik", "Erik Operator")):
            bootstrap.upsert_principal(s, {"id": pid, "display_name": name, "groups": ["hackathon"]})
        bootstrap.scan_all(s)
    Worker(lease_s=600).drain()

    t0 = time.time()
    with db.session_scope() as s:
        for c in s.scalars(select(Content).where(Content.extracted.is_(True))):
            if not s.scalar(select(File.id).where(File.sha256 == c.sha256, File.status == "active").limit(1)):
                continue
            for op in candidates(c):
                if not s.scalar(select(Rendition.id).where(Rendition.content_sha == c.sha256, Rendition.operation == op).limit(1)):
                    enqueue(s, "content.optimize", {"sha": c.sha256, "op": op},
                            key=f"optimize:{c.sha256}:{op}:manual:{int(t0)}")
    Worker(lease_s=600).drain()
    out["optimize_wall_s"] = round(time.time() - t0, 1)

    with db.session_scope() as s:
        for r in s.scalars(select(Rendition).order_by(Rendition.created_at)):
            f = s.scalar(select(File).where(File.sha256 == r.content_sha).limit(1))
            out["renditions"].append({
                "file": f.filename if f else r.content_sha[:12], "op": r.operation, "passed": r.passed,
                "original_mb": round(r.validation["original_bytes"] / 1e6, 2), "rendition_mb": round(r.size_bytes / 1e6, 2),
                "saving": r.validation["saving_ratio"], "measured": r.validation["measured"],
                "checks": [{k: c[k] for k in ("name", "passed", "detail")} for c in r.validation["checks"]]})
        p = planner.create(s, ANALYST, "compress", title="compress survivors")
        pid = p.id
        out["plan"] = {"id": pid, "summary": p.evidence["summary"],
                       "items": [{"file": r["victim"]["path"], "deletable": r["deletable"], "bytes": r["bytes_reclaimable"],
                                  "failed": [c["name"] for c in r["checks"] if c["hard"] and not c["passed"]]}
                                 for r in p.evidence["items"]]}
        before = {it.file_id: (s.get(File, it.file_id).path, it.victim_sha, it.survivor_sha) for it in p.items if it.deletable}
        root = Path(s.get(Source, s.get(File, next(iter(before))).source_name).config["root"]) if before else None

    if before and not args.no_execute:
        with db.session_scope() as s:
            ex.submit(s, ANALYST, pid)
            # scripted approvals for the reproducible run; in the demo two people approve after looking
            ex.approve(s, APPROVER_1, pid, "scripted approval (reproducible run)")
            ex.approve(s, APPROVER_2, pid, "scripted second approval (two required for in-place replacement)")
            ex.execute(s, OPERATOR, pid)
        Worker(lease_s=600).drain()
        verify = []
        with db.session_scope() as s:
            pl = s.get(DeletionPlan, pid)
            for it in pl.items:
                if it.file_id not in before:
                    continue
                path, orig, new = before[it.file_id]
                live = _sha(root / path)
                verify.append({"file": path, "state": it.state, "source_holds_rendition": live == new,
                               "original_in_quarantine": it.quarantine_tier == "quarantine",
                               "size_now_mb": round((root / path).stat().st_size / 1e6, 2)})
            out["executed"] = {"status": pl.status, "items": verify}
    path = Path(get_settings().data_dir) / "reports" / "real_compression.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str))
    for r in out["renditions"]:
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['op']:<16} {r['original_mb']:>8.2f} MB -> {r['rendition_mb']:>8.2f} MB "
              f"({r['saving']:.0%})  {r['file']}")
        for c in r["checks"]:
            print(f"      {'ok ' if c['passed'] else 'NO '} {c['name']}: {c['detail']}")
    print("plan:", json.dumps(out["plan"]["summary"], default=str))
    if "executed" in out:
        print("executed:", json.dumps(out["executed"], indent=1))
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
