"""End-to-end scenario on the sample material (the pitch demo).

Copies the samples into the data dir (sources are mutated by deletions, the
originals stay untouched), then: scan -> extract -> relate -> capsules ->
versioning -> search -> departure -> plans for every class -> approvals ->
quarantine -> restore -> grace period -> purge -> recordings to archive ->
measured savings -> audit verification. Writes report.json + report.html.
"""
from __future__ import annotations

import html
import json
import shutil
import time
from pathlib import Path

from sqlalchemy import func, select

from . import audit, bootstrap, clock, db
from .config import get_settings
from .governance import departure, executor as ex, planner
from .jobs import Worker
from .models import AuditEvent, DeletionPlan, File, Lineage, Relation
from .queries import display_name
from .reports import savings
from .search.index import search
from .security.auth import Identity
from .util import human_bytes

CAROL = Identity("carol", frozenset({"analyst", "viewer"}), ("network-team", "core-team"))
ALICE = Identity("alice", frozenset({"viewer"}), ("network-team",))
FRANK = Identity("frank", frozenset({"viewer"}), ())
DAVE = Identity("dave", frozenset({"approver"}), ("governance",))
GINA = Identity("gina", frozenset({"approver", "auditor"}), ("governance",))
ERIN = Identity("erin", frozenset({"operator"}), ("governance",))


def h(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m")


def drain() -> int:
    return Worker(lease_s=600).drain()


def approve_and_run(pid: str, approvers: list[Identity]) -> DeletionPlan:
    with db.session_scope() as s:
        ex.submit(s, CAROL, pid)
        for a in approvers:
            p = s.get(DeletionPlan, pid)
            if p.status == "pending_approval":
                ex.approve(s, a, pid, "evidence reviewed")
        ex.execute(s, ERIN, pid)
    drain()
    with db.session_scope() as s:
        p = s.get(DeletionPlan, pid)
        for it in p.items:                # load before detaching
            s.expunge(it)
        s.expunge(p)
        return p


def summary_line(p: DeletionPlan) -> str:
    sm = p.evidence["summary"]
    return (f"{p.kind:11} {sm['deletable']}/{sm['items']} deletable  {human_bytes(sm['bytes_reclaimable']):>9}  "
            f"approvals {p.required_approvals}  blocked_by {sm['blocked_by'] or '-'}")


def run(samples: Path, fresh: bool = True, report_dir: Path | None = None) -> dict:
    st = get_settings()
    work = st.data_dir / "sources"
    if fresh:
        for p in (st.data_dir / "kora.db", st.data_dir / "kora.db-wal", st.data_dir / "kora.db-shm"):
            p.unlink(missing_ok=True)
        if not st.database_url.startswith("sqlite"):
            from .models import Base
            db.init_engine(st.database_url)
            Base.metadata.drop_all(db.engine())
        shutil.rmtree(st.data_dir / "blobs", ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    if not work.exists():
        shutil.copytree(samples, work)
    clock.reset()
    bootstrap.init(json_logs=False)
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    out: dict = {"plans": {}}

    h("1. Ingest: scan SharePoint, OneDrive and Ericsson Play")
    with db.session_scope() as s:
        bootstrap.load_samples(s, work)
        bootstrap.scan_all(s)
    t = time.time()
    n = drain()
    with db.session_scope() as s:
        files = s.scalar(select(func.count()).select_from(File))
        total = int(s.scalar(select(func.sum(File.size_bytes))) or 0)
    print(f"{files} files, {human_bytes(total)} processed through {n} idempotent jobs in {time.time() - t:.1f}s")

    h("2. Version handling: lineages, ordering and slide-level diffs")
    with db.session_scope() as s:
        for lin in s.scalars(select(Lineage).order_by(Lineage.key)):
            reps = sorted(lin.versions, key=lambda v: (v.ordinal, v.variant_of_id is not None))
            if len(reps) < 2:
                continue
            print(f"{lin.key}  ordering={lin.ordering_basis}/{lin.ordering_confidence}")
            for v in reps:
                tag = "HEAD" if v.is_head else "variant-of-" + str(next(
                    (x.revision_label for x in reps if x.id == v.variant_of_id), "")) if v.variant_of_id else \
                    ("released" if v.released else "")
                print(f"   {str(v.revision_label or '-'):9} {tag:14} {display_name(s, v.content_sha):40} "
                      f"{(v.diff_from_parent or {}).get('summary', '')}")
        out["lineages"] = s.scalar(select(func.count()).select_from(Lineage))

    h("3. Content relationship graph (all duplicate classes)")
    with db.session_scope() as s:
        counts = dict(s.execute(select(Relation.kind, func.count()).group_by(Relation.kind)).all())
        groups = s.execute(select(File.sha256, func.count()).where(File.status == "active")
                           .group_by(File.sha256).having(func.count() > 1)).all()
        counts["EXACT_COPY (extra files)"] = sum(c - 1 for _, c in groups)
        for k, v in sorted(counts.items()):
            print(f"   {k:26} {v}")
        out["relations"] = counts

    h("4. Evidence search (permission-aware, version-aware)")
    with db.session_scope() as s:
        for who, q in ((ALICE, "where did we discuss the network architecture change edge data centre"),
                       (ALICE, "latency budget"), (FRANK, "latency budget")):
            res = search(s, who, q, 3)["results"]
            print(f"   {who.principal_id} > \"{q}\"  ({len(res)} hits)")
            for r in res[:2]:
                if r["type"] == "transcript":
                    sl = r.get("slide") or {}
                    print(f"      {r['recording']} @{r['start_s']:.0f}s {r['speaker']}: \"{r['text'][:70]}\"")
                    if sl:
                        print(f"         on screen: slide {sl.get('ordinal')} \"{sl.get('title')}\" - {sl.get('doc_number')} rev {sl.get('revision')}")
                elif r["type"] == "frame":
                    print(f"      keyframe @{r['t']:.0f}s (OCR): \"{r['text'][:60]}\"")
                else:
                    vn = r.get("version") or {}
                    print(f"      {r['document']} p.{r['page']} rev {vn.get('revision')}"
                          f"{' (' + vn['in_latest'] + ')' if vn.get('in_latest') else ''}; same slide in "
                          f"{len(r.get('also_in', []))} other documents")

    h("5. Employee departure: knowledge continuity assessment for Bob")
    with db.session_scope() as s:
        rep = departure.assess(s, CAROL, "bob")
        sm = rep["summary"]
        for k in ("files_owned", "deletable_duplicates", "blocked_by_retention", "blocked_other", "partially_shared",
                  "unique_documents", "versioned_documents", "unique_recordings", "open_actions_and_decisions",
                  "lineage_heads_owned", "proposed_new_owner"):
            print(f"   {k:28} {sm[k]}")
        for d in rep["dependencies"]:
            print(f"   open {d['type']}: \"{d['text'][:60]}\" ({len(d['sources'])} sources)")
        dep_id = rep["plan_id"]
        out["departure"] = sm
    p = approve_and_run(dep_id, [DAVE])
    print(f"   departure plan -> {p.status}: " + ", ".join(f"{it.dup_class}:{it.state}" for it in p.items))
    out["plans"]["departure"] = p.evidence["summary"]

    h("6. Plans for every deletable class (dry run first, then governed execution)")
    with db.session_scope() as s:
        ids = {}
        for kind in ("revisions", "subsets", "media"):
            pl = planner.create(s, CAROL, kind)
            ids[kind] = pl.id
            print("   " + summary_line(pl))
    for kind, approvers in (("revisions", [DAVE]), ("subsets", [DAVE, GINA]), ("media", [DAVE, GINA])):
        p = approve_and_run(ids[kind], approvers)
        out["plans"][kind] = p.evidence["summary"]
        print(f"   {kind:11} -> {p.status}: " + ", ".join(it.state for it in p.items))
    with db.session_scope() as s:
        rec = planner.create(s, CAROL, "recordings")
        print("   " + summary_line(rec) + "   <- SharePoint original is pinned as media survivor")
        ex.cancel(s, CAROL, rec.id)

    h("7. Reversibility: restore one item during the grace period")
    with db.session_scope() as s:
        p = s.get(DeletionPlan, dep_id)
        it = next(i for i in p.items if i.state == "quarantined")
        path = s.get(File, it.file_id).path
        ex.restore(s, ERIN, dep_id, [it.id])
    drain()
    print(f"   restored {path}: {(work / 'onedrive' / 'bob' / path).exists()}")

    h("8. Grace period elapses (+31 days) -> purge with re-verification")
    clock.advance(days=31)
    with db.session_scope() as s:
        for pid in [dep_id, *ids.values()]:
            if s.get(DeletionPlan, pid).status == "quarantined":
                ex.purge(s, ERIN, pid)
    drain()
    with db.session_scope() as s:
        for pid in [dep_id, *ids.values()]:
            p = s.get(DeletionPlan, pid)
            print(f"   {p.kind:11} {p.status:10} " + ", ".join(sorted(i.state for i in p.items)))

    h("9. Recordings: validated capsule replaces the hot copy (original -> archive tier)")
    with db.session_scope() as s:
        rec = planner.create(s, CAROL, "recordings")
        print("   " + summary_line(rec))
        for r in rec.evidence["items"]:
            fails = [c["name"] for c in r["checks"] if c["hard"] and not c["passed"]]
            print(f"     {'OK ' if r['deletable'] else 'BLK'} {r['victim']['path']:45} {', '.join(fails) or 'all checks passed'}")
        rid = rec.id
    p = approve_and_run(rid, [DAVE, GINA])
    out["plans"]["recordings"] = p.evidence["summary"]
    print(f"   recordings -> {p.status}: " + ", ".join(it.state for it in p.items))

    with db.session_scope() as s:
        from .capsule.rebuild import rebuild_video
        from .models import Content
        big = s.scalar(select(Content).where(Content.detected_type == "video").order_by(Content.size_bytes.desc()))
        dst = (report_dir or st.data_dir) / "rebuilt_from_capsule.mp4"
        dst.parent.mkdir(parents=True, exist_ok=True)
        rb = rebuild_video(s, big.sha256, dst)
        out["rebuilt_video"] = rb
        print(f"   recreated a watchable video from the capsule alone: {rb['keyframes']} slides + speech + subtitles, "
              f"{human_bytes(rb['bytes'])} (original {human_bytes(rb['original_bytes'])}) -> {dst.name}")

    h("10. Measured savings")
    with db.session_scope() as s:
        rep = savings(s)
    out["savings"] = rep
    for r in rep["recordings"]["recordings"]:
        print(f"   {r['recording']:42} {human_bytes(r['original_bytes']):>9} -> capsule {human_bytes(r['capsule_bytes']):>9}"
              f"  ({r['reduction']:.1%} smaller, {r['reduction_without_audio']:.1%} without audio)"
              f"{'' if r['valid_capsule'] else '  [capsule NOT valid: not distillable]'}")
    sc = rep["recordings"]["scenario_2h_1gb"]
    if sc:
        print(f"   scenario 2 h / 1 GB: capsule {human_bytes(sc['capsule_without_audio_bytes'])} "
              f"({sc['reduction_without_audio']:.2%}) or {human_bytes(sc['capsule_with_audio_bytes'])} with speech audio "
              f"({sc['reduction_with_audio']:.2%})")
    with db.session_scope() as s:
        from .models import Rendition
        rends = []
        for r in s.scalars(select(Rendition).order_by(Rendition.created_at)):
            v = r.validation
            rends.append({"content": display_name(s, r.content_sha), "op": r.operation, "passed": r.passed,
                          "original": v["original_bytes"], "rendition": r.size_bytes,
                          "checks": [c["detail"] for c in v["checks"]]})
            print(f"   {'PASS' if r.passed else 'FAIL'} {r.operation:<15} {display_name(s, r.content_sha):26} "
                  f"{human_bytes(v['original_bytes'])} -> {human_bytes(r.size_bytes)} ({v['saving_ratio']:.0%}); "
                  + "; ".join(c["detail"] for c in v["checks"] if c["name"] != "net_saving"))
        out["renditions"] = rends
        freed = s.execute(select(File.status, func.sum(File.size_bytes)).group_by(File.status)).all()
    print("   source storage by status: " + ", ".join(f"{k}={human_bytes(int(v or 0))}" for k, v in freed))

    h("11. Audit trail")
    with db.session_scope() as s:
        v = audit.verify(s)
        print(f"   {v.events} hash-chained events, chain valid: {v.ok}, head {v.head_hash[:16]}")
        out["audit"] = {"events": v.events, "ok": v.ok, "head": v.head_hash}
        out["audit_tail"] = [{"seq": e.seq, "action": e.action, "actor": e.actor, "subject": e.subject_id}
                             for e in s.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(12))]

    rd = report_dir or st.data_dir
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "report.json").write_text(json.dumps(out, indent=2, default=str))
    (rd / "report.html").write_text(render_html(out))
    print(f"\nreport written to {rd / 'report.html'}")
    return out


def render_html(out: dict) -> str:
    sv = out["savings"]
    rows = "".join(
        f"<tr><td>{html.escape(r['recording'])}</td><td>{human_bytes(r['original_bytes'])}</td>"
        f"<td>{human_bytes(r['capsule_bytes'])}</td><td>{r['reduction']:.1%}</td>"
        f"<td>{'valid' if r['valid_capsule'] else 'not distillable'}</td></tr>" for r in sv["recordings"]["recordings"])
    plans = "".join(
        f"<tr><td>{k}</td><td>{v['deletable']}/{v['items']}</td><td>{human_bytes(v['bytes_reclaimable'])}</td>"
        f"<td>{v['required_approvals']}</td><td>{html.escape(', '.join(f'{a}: {b}' for a, b in v['blocked_by'].items()) or '-')}</td></tr>"
        for k, v in out["plans"].items())
    rel = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in sorted(out["relations"].items()))
    dep = "".join(f"<tr><td>{k.replace('_', ' ')}</td><td>{v}</td></tr>" for k, v in out["departure"].items()
                  if k not in ("principal", "display_name", "status"))
    sc = sv["recordings"]["scenario_2h_1gb"] or {}
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>KORA demo report</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#1f2937}}
h1{{font-size:1.6rem}}h2{{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #e5e7eb;padding-bottom:.3rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #f0f0f0;padding:.35rem .5rem;text-align:left}}
th{{color:#6b7280;font-weight:600}}.k{{font-size:2rem;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}}
.card{{border:1px solid #e5e7eb;border-radius:10px;padding:1rem}}</style></head><body>
<h1>KORA - Knowledge Optimization, Recovery &amp; Assurance</h1>
<div class="grid"><div class="card"><div class="k">{sc.get('reduction_without_audio', 0):.2%}</div>2 h / 1 GB recording, capsule without audio (measured per-minute cost)</div>
<div class="card"><div class="k">{sc.get('reduction_with_audio', 0):.2%}</div>same, keeping a 16 kbps speech track</div>
<div class="card"><div class="k">{out['audit']['events']}</div>hash-chained audit events, chain valid: {out['audit']['ok']}</div></div>
<h2>Recordings</h2><table><tr><th>Recording</th><th>Original</th><th>Capsule</th><th>Reduction</th><th>Capsule</th></tr>{rows}</table>
<h2>Deletion plans (evidence summary)</h2><table><tr><th>Plan</th><th>Deletable</th><th>Reclaimable</th><th>Approvals</th><th>Blocked by</th></tr>{plans}</table>
<h2>Relationship graph</h2><table>{rel}</table>
<h2>Departure assessment (Bob)</h2><table>{dep}</table>
</body></html>"""
