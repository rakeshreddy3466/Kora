"""Run KORA over an arbitrary folder (or Drive-export zip) of real files and
produce a measured report: what was found, what took how long, what failed,
what a human must decide, and what could be saved."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import func, select

from . import audit, bootstrap, clock, db, hitl
from .config import get_settings
from .models import Capsule, Content, File, Job, Lineage, Relation, ReviewTask, Source
from .queries import display_name
from .reports import savings
from .util import human_bytes

MAX_ZIP_BYTES = 20 * 1024 ** 3


def safe_extract(zpath: Path, dest: Path) -> int:
    """Zip-slip, symlink and size guarded extraction (Drive folder downloads are zips)."""
    n, total = 0, 0
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/") or Path(name).name.startswith("._"):
                continue
            target = (dest / name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"unsafe path in zip: {name!r}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue                                   # symlink entry
            total += info.file_size
            if total > MAX_ZIP_BYTES:
                raise ValueError("zip expands beyond limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            n += 1
    return n


def stage_input(inp: Path, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    extracted = 0
    if inp.is_file() and zipfile.is_zipfile(inp):
        extracted += safe_extract(inp, dest)
    else:
        for p in inp.rglob("*"):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(inp)
                if zipfile.is_zipfile(p) and p.suffix.lower() == ".zip":
                    extracted += safe_extract(p, dest / rel.with_suffix(""))
                else:
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest / rel)
    return {"extracted_from_zips": extracted, "files": sum(1 for p in dest.rglob("*") if p.is_file())}


def _drain(workers: int) -> None:
    if workers <= 1:
        from .jobs import Worker
        Worker(lease_s=get_settings().worker_lease_s).drain()
        return
    procs = [subprocess.Popen([sys.executable, "-m", "kora.cli", "worker", "--drain"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(workers)]
    for p in procs:
        p.wait()
    # a final in-process pass picks up anything enqueued after the last worker exited
    from .jobs import Worker
    Worker(lease_s=get_settings().worker_lease_s).drain()


def run(inp: Path, name: str, system: str, owner: str, acl: list[str], workers: int = 1,
        ai_use_allowed: bool = False) -> dict:
    st = get_settings()
    bootstrap.init(json_logs=False)
    dest = st.data_dir / "sources" / name
    staged = stage_input(inp, dest) if not dest.exists() else {"reused": str(dest)}
    (dest / "_kora_source.json").write_text(json.dumps({"default_owner": owner, "default_acl": acl}))
    with db.session_scope() as s:
        bootstrap.upsert_principal(s, {"id": owner, "display_name": owner})
        src = bootstrap.upsert_source(s, name, system, str(dest), name)
        src.config = {**src.config, "ai_use_allowed": ai_use_allowed}
        bootstrap.scan_all(s)
    t0 = time.time()
    _drain(workers)
    wall = time.time() - t0
    return report(name, wall, staged)


def report(name: str, wall_s: float, staged: dict) -> dict:
    with db.session_scope() as s:
        files = list(s.scalars(select(File).where(File.source_name == name)))
        by_type = Counter(f.detected_type for f in files)
        bytes_type = defaultdict(int)
        for f in files:
            bytes_type[f.detected_type] += f.size_bytes
        jobs = list(s.scalars(select(Job)))
        dur = defaultdict(list)
        for j in jobs:
            if j.finished_at and j.status == "succeeded":
                dur[j.kind].append((j.finished_at - j.created_at).total_seconds())
        failures = [{"kind": j.kind, "error": (j.last_error or "").strip().splitlines()[0][:300] if j.last_error else "",
                     "payload": j.payload} for j in jobs if j.status == "dead"]
        retried = sum(1 for j in jobs if j.attempts > 1)
        caps = []
        for c in s.scalars(select(Content).where(Content.sha256.in_([f.sha256 for f in files if f.sha256]))):
            cap = s.get(Capsule, c.sha256)
            caps.append({"name": display_name(s, c.sha256), "type": c.detected_type, "bytes": c.size_bytes,
                         "units": c.unit_count, "duration_s": c.duration_s,
                         "doc_number": (c.meta or {}).get("doc_number"), "revision": (c.meta or {}).get("revision"),
                         "transcript": (c.media or {}).get("transcript_source"),
                         "capsule_bytes": cap.size_bytes if cap else None, "capsule_valid": cap.valid if cap else None,
                         "failed_checks": [ch["name"] for ch in (cap.validation.get("checks", []) if cap else [])
                                           if not ch["passed"]]})
        rels = Counter(r.kind for r in s.scalars(select(Relation)))
        groups = s.execute(select(File.sha256, func.count()).where(File.status == "active", File.sha256.is_not(None))
                           .group_by(File.sha256).having(func.count() > 1)).all()
        lineages = [{"key": l.key, "ordering": f"{l.ordering_basis}/{l.ordering_confidence}", "conflict": l.has_conflict,
                     "versions": [f"{v.revision_label or '-'}:{display_name(s, v.content_sha)}"
                                  for v in sorted(l.versions, key=lambda v: v.ordinal)]}
                    for l in s.scalars(select(Lineage)) if len(l.versions) > 1]
        reviews = [{"kind": t.kind, "role": t.required_role, "question": t.question, "blocks": t.blocks}
                   for t in s.scalars(select(ReviewTask).where(ReviewTask.status == "open").order_by(ReviewTask.priority))]
        sav = savings(s)
        out = {
            "source": name, "staged": staged, "wall_clock_s": round(wall_s, 1),
            "files": len(files), "bytes": sum(f.size_bytes for f in files),
            "by_type": {k or "unknown": {"files": v, "bytes": bytes_type[k]} for k, v in by_type.items()},
            "ingest_states": dict(Counter(f.ingest_state for f in files)),
            "jobs": {"total": len(jobs), "by_status": dict(Counter(j.status for j in jobs)), "retried": retried,
                     "median_s_by_kind": {k: round(sorted(v)[len(v) // 2], 2) for k, v in dur.items()}},
            "failures": failures,
            "contents": caps,
            "exact_copy_groups": len(groups), "exact_copy_extra_files": sum(n - 1 for _, n in groups),
            "relations": dict(rels), "lineages": lineages,
            "open_reviews": reviews, "review_stats": hitl.stats(s),
            "savings": {"summary": sav["summary"], "potential": sav["potential"],
                        "recordings": sav["recordings"]},
            "audit": audit.verify(s).__dict__,
        }
    rd = get_settings().data_dir / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{name}.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def print_report(r: dict) -> None:
    print(f"\n{r['files']} files, {human_bytes(r['bytes'])}, processed in {r['wall_clock_s']}s "
          f"({r['jobs']['total']} jobs: {r['jobs']['by_status']}, {r['jobs']['retried']} retried)")
    for t, v in sorted(r["by_type"].items()):
        print(f"   {t:12} {v['files']:4} files  {human_bytes(v['bytes']):>10}")
    if r["failures"]:
        print("failures (dead-lettered, visible in /v1/jobs?status=dead):")
        for f in r["failures"]:
            print(f"   {f['kind']}: {f['error']}")
    print("contents:")
    for c in r["contents"]:
        extra = f" doc {c['doc_number']} rev {c['revision']}" if c.get("doc_number") else ""
        dur = f" {c['duration_s'] / 60:.1f} min, transcript={c['transcript']}" if c.get("duration_s") else ""
        cap = (f" capsule {human_bytes(c['capsule_bytes'])} {'valid' if c['capsule_valid'] else 'INVALID ' + str(c['failed_checks'])}"
               if c["capsule_bytes"] is not None else " no capsule")
        print(f"   [{c['type']}] {c['name'][:60]} {human_bytes(c['bytes'])} {c['units']} units{extra}{dur}{cap}")
    print(f"exact copy groups {r['exact_copy_groups']} (+{r['exact_copy_extra_files']} files); relations {r['relations']}")
    for l in r["lineages"]:
        print(f"   lineage {l['key']} [{l['ordering']}]{' CONFLICT' if l['conflict'] else ''}: " + " -> ".join(l["versions"]))
    print(f"open human reviews: {len(r['open_reviews'])}")
    for t in r["open_reviews"][:12]:
        print(f"   ({t['role']}) {t['kind']}: {t['question'][:140]}")
    sm = r["savings"]["summary"]
    print(f"savings of {human_bytes(sm['active_bytes'])} active: removable {human_bytes(sm['bytes_removable'])} "
          f"({sm['removable_ratio']:.1%}: copies, variants, superseded drafts, excerpts); movable to archive "
          f"{human_bytes(sm['bytes_movable_to_archive'])} (recordings with validated capsules); compression "
          f"{human_bytes(sm['bytes_saved_by_compression'])} (renditions within the quality budget); hot-tier reduction "
          f"{sm['hot_reduction_ratio']:.1%}, net {sm['net_reduction_ratio']:.1%} after KORA's own capsule bytes")
    print(f"audit chain ok={r['audit']['ok']} events={r['audit']['events']}")
