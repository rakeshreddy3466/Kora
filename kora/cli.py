"""Command-line interface.

The CLI talks to the database directly and is therefore an *administrative*
tool for operators on the KORA host; it acts as the principal given with
--as and the roles given with --roles, and every action is audited under that
principal. End users and integrations use the authenticated HTTP API.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from sqlalchemy import select

from . import audit, bootstrap, clock, db
from .security.auth import Identity
from .util import human_bytes


def _ident(s, args) -> Identity:
    from .models import Principal
    if args.as_ == "system":
        from .security.auth import SYSTEM
        return SYSTEM
    p = s.get(Principal, args.as_)
    if p is None:
        sys.exit(f"unknown principal {args.as_}")
    return Identity(p.id, frozenset(args.roles.split(",")), tuple(p.groups or ()))


def _drain() -> int:
    from .jobs import Worker
    return Worker(lease_s=600).drain()


def cmd_init(args):
    bootstrap.init(json_logs=False)
    if args.samples:
        with db.session_scope() as s:
            bootstrap.load_samples(s, Path(args.samples))
    print("initialised", db.engine().url.render_as_string(hide_password=True))


def cmd_principal(args):
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        bootstrap.upsert_principal(s, {"id": args.id, "display_name": args.name or args.id, "status": args.status,
                                       "groups": [g for g in (args.groups or "").split(",") if g],
                                       "manager_id": args.manager, "team": args.team})
    print("ok")


def cmd_source(args):
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        bootstrap.upsert_source(s, args.name, args.system, args.root, args.site, approved=not args.unapproved)
    print("ok")


def cmd_key(args):
    from .security.auth import create_key
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        key = create_key(s, args.principal, args.roles.split(","))
        audit.append(s, "admin", "apikey.created", "principal", args.principal, {"roles": args.roles.split(",")})
    print(key)


def cmd_scan(args):
    from .jobs import enqueue
    from .models import Source
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        names = args.sources or [x.name for x in s.scalars(select(Source))]
        for n in names:
            enqueue(s, "source.scan", {"source": n}, key=f"scan:{n}:{clock.now().isoformat()}", priority=10)
    if args.wait:
        t = time.time()
        print(f"processed {_drain()} jobs in {time.time() - t:.1f}s")


def cmd_worker(args):
    from .jobs import Worker
    st = bootstrap.init()
    kinds = [k for k in (args.kinds or "").split(",") if k] or None
    w = Worker(lease_s=st.worker_lease_s, kinds=kinds)
    if args.drain:
        print(w.drain())
    else:
        w.run_forever()


def cmd_serve(args):
    import uvicorn
    uvicorn.run("kora.api.app:create_app", factory=True, host=args.host, port=args.port)


UI_PERSONAS = [
    ("ui-analyst", "Abhilash (storage analyst)", ["analyst", "viewer", "auditor"]),
    ("ui-approver-1", "Jagadish (approver, records)", ["approver"]),
    ("ui-approver-2", "Rakesh (approver, IT)", ["approver"]),
    ("ui-operator", "Nandu (operator)", ["operator"]),
    ("ui-dpo-1", "Yashwitha (data protection)", ["dpo", "analyst"]),
    ("ui-dpo-2", "Second DPO (data protection)", ["dpo"]),
]


def _ui_personas(s) -> list[dict]:
    """Demo identities for the dashboard. Keys are created once, stored 0600 under secrets/, and
    handed to the browser in the URL fragment (never sent to the server as part of a URL)."""
    import json as _json
    import os
    from collections import Counter

    from .config import get_settings
    from .models import File, Principal
    from .security.auth import authenticate, create_key
    path = get_settings().data_dir / "secrets" / "ui-personas.json"
    saved = {}
    if path.exists():
        saved = {p["id"]: p for p in _json.loads(path.read_text())}
    acl_groups = sorted({a[6:] for f in s.scalars(select(File)) for a in (f.acl or []) if a.startswith("group:")}
                        | {"everyone"})               # folders added from the page are shared with group:everyone
    wanted = [dict(id=i, name=n, roles=r, groups=acl_groups) for i, n, r in UI_PERSONAS]
    owners = Counter(f.owner_id for f in s.scalars(select(File).where(File.status == "active")) if f.owner_id)
    persona_ids = {w["id"] for w in wanted}
    for oid, _n in owners.most_common(2):                 # people who own files: self-service deletion
        if oid in persona_ids:
            continue                                      # already in the list under their role
        p = s.get(Principal, oid)
        if p is not None and p.status != "departed":
            wanted.append(dict(id=oid, name=f"{(p.display_name or oid).split(' (')[0]} (file owner)", roles=["viewer"],
                               groups=list(p.groups or []), existing=True))
    out = []
    for w in wanted:
        if not w.get("existing"):
            bootstrap.upsert_principal(s, {"id": w["id"], "display_name": w["name"], "groups": w["groups"]})
        s.flush()
        key = saved.get(w["id"], {}).get("key")
        ident = authenticate(s, key) if key else None
        if ident is None or sorted(ident.roles) != sorted(w["roles"]):
            key = create_key(s, w["id"], w["roles"])
            audit.append(s, "admin", "apikey.created", "principal", w["id"], {"roles": w["roles"], "for": "dashboard"})
        out.append({"id": w["id"], "name": w["name"], "roles": w["roles"], "key": key})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(_json.dumps(out))
    return out


def cmd_dashboard(args):
    """API + dashboard + an in-process worker, bound to localhost."""
    import base64
    import json as _json
    import threading
    import webbrowser

    import uvicorn

    from .api.app import create_app
    from .jobs import Worker
    import os
    os.environ.setdefault("KORA_ALLOW_LOCAL_INGEST", "1")      # local single-user mode: the page may add folders
    from .config import configure, get_settings, load_settings
    configure(load_settings())
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        personas = _ui_personas(s)
    stop = threading.Event()
    from .jobs.queue import release_orphans
    if args.workers and (freed := release_orphans("dashboard")):
        print(f"Picking up {freed} job(s) left running when the website was last stopped.")

    def work(n):
        w = Worker(worker_id=f"dashboard{n}-{os.getpid()}", lease_s=600)
        while not stop.is_set():
            try:
                if not w.run_once():
                    stop.wait(1.0)
            except Exception:                              # keep serving; the job itself is retried
                stop.wait(2.0)
    for n in range(max(0, args.workers)):                 # 0: separate worker containers do the jobs
        threading.Thread(target=work, args=(n,), name=f"kora-worker-{n}", daemon=True).start()
    blob = base64.urlsafe_b64encode(_json.dumps(personas).encode()).decode().rstrip("=")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host     # what you type in a browser
    url = f"http://{shown}:{args.port}/ui/#p={blob}"
    print("KORA dashboard (keep this URL private: it carries demo keys in the fragment):")
    print(url)
    print(f"\nAPI docs (Swagger): http://{shown}:{args.port}/docs"
          f"  - click Authorize and paste a key from {get_settings().data_dir / 'secrets' / 'ui-personas.json'}")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(create_app(init=False), host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()


def cmd_lineages(args):
    from .models import Lineage
    from .queries import display_name
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        for lin in s.scalars(select(Lineage).order_by(Lineage.key)):
            print(f"{lin.key}  [{lin.ordering_basis}/{lin.ordering_confidence}]{'  CONFLICT' if lin.has_conflict else ''}")
            for v in sorted(lin.versions, key=lambda v: (v.ordinal, v.variant_of_id is not None)):
                tag = "HEAD" if v.is_head else ("variant" if v.variant_of_id else ("released" if v.released else ""))
                print(f"   {v.ordinal:>2} {str(v.revision_label or '-'):9} {tag:9} {display_name(s, v.content_sha):45} "
                      f"{(v.diff_from_parent or {}).get('summary', '')}")


def cmd_findings(args):
    from .models import Relation
    from .queries import display_name
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        for r in s.scalars(select(Relation).order_by(Relation.kind)):
            print(f"{r.kind:18} {display_name(s, r.a_sha):42} {display_name(s, r.b_sha):42} {r.score:.3f}")


def _print_plan(p, verbose=True):
    sm = p.evidence["summary"]
    print(f"plan {p.id} [{p.kind}] {p.status}: {sm['deletable']}/{sm['items']} deletable, "
          f"{human_bytes(sm['bytes_reclaimable'])} reclaimable, approvals {len(p.approvals)}/{p.required_approvals}, "
          f"readiness {sm['readiness']}")
    if verbose:
        for r in p.evidence["items"]:
            v, sv = r["victim"], r["survivor"]
            print(f"  [{'OK ' if r['deletable'] else 'BLK'}] {r['class']:19} {v['system']}:{v['path']}"
                  + (f"  ->  {sv['system']}:{sv['path']}" if sv else "") + f"  ({r['recovery']})")
            for c in r["checks"]:
                if c["hard"] and not c["passed"]:
                    print(f"        x {c['name']}: {c['detail']}")


def cmd_plan(args):
    from .governance import executor, planner
    from .models import DeletionPlan
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        ident = _ident(s, args)
        if args.action == "create":
            scope = {"owner": args.owner} if args.owner else {}
            p = planner.create(s, ident, args.target, scope)
            _print_plan(p)
            return
        p = s.get(DeletionPlan, args.target)
        if p is None:
            sys.exit("unknown plan")
        if args.action == "show":
            print(json.dumps(p.evidence, indent=2, default=str)) if args.json else _print_plan(p)
            return
        fn = {"submit": executor.submit, "approve": executor.approve, "execute": executor.execute,
              "purge": executor.purge, "restore": executor.restore, "cancel": executor.cancel}[args.action]
        fn(s, ident, p.id)
    if args.action in ("execute", "purge", "restore"):
        _drain()
    with db.session_scope() as s:
        _print_plan(s.get(DeletionPlan, args.target), verbose=False)


def cmd_departure(args):
    from .governance import departure
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        r = departure.assess(s, _ident(s, args), args.principal)
    print(json.dumps(r if args.json else r["summary"], indent=2, default=str))


def cmd_search(args):
    from .search.index import search
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        res = search(s, _ident(s, args), args.query, args.limit)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return
    for r in res["results"]:
        if r["type"] == "transcript":
            sl = r.get("slide") or {}
            print(f"[{r['score']:.2f}] {r['recording']} @ {r['start_s']:.0f}s  {r['speaker']}: \"{r['text'][:90]}\"")
            if sl:
                print(f"        on screen: slide {sl.get('ordinal')} '{sl.get('title')}' of {sl.get('doc_number')} rev {sl.get('revision')}")
        elif r["type"] == "frame":
            print(f"[{r['score']:.2f}] keyframe @{r['t']:.0f}s (OCR): \"{r['text'][:80]}\"")
        else:
            vn = r.get("version") or {}
            print(f"[{r['score']:.2f}] {r.get('document')} p.{r.get('page')} ({r.get('doc_number') or '-'} rev "
                  f"{vn.get('revision')}{', ' + vn['in_latest'] if vn.get('in_latest') else ''})  "
                  f"\"{r['text'][:70].strip()}\"  also in {len(r.get('also_in', []))} other docs")


def cmd_audit(args):
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        r = audit.verify(s)
    print(json.dumps(r.__dict__, indent=2))
    sys.exit(0 if r.ok else 2)


def cmd_report(args):
    from .reports import savings
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        r = savings(s)
    print(json.dumps(r, indent=2, default=str))


def cmd_reconstruct(args):
    from .versioning import reconstruct
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        Path(args.out).write_bytes(reconstruct.reconstruct_pdf(s, args.sha))
        print(json.dumps(reconstruct.verify(s, args.sha), indent=2))


def cmd_rebuild_video(args):
    from .capsule.rebuild import rebuild_video
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        print(json.dumps(rebuild_video(s, args.sha, Path(args.out)), indent=2))


def cmd_ingest_folder(args):
    from .runner import print_report, run
    r = run(Path(args.path), args.name, args.system, args.owner, args.acl.split(","), args.workers, args.ai_use_allowed)
    print_report(r)


def cmd_reviews(args):
    from . import hitl
    from .models import ReviewTask
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        if args.action == "list":
            for t in s.scalars(select(ReviewTask).where(ReviewTask.status == "open").order_by(ReviewTask.priority)):
                print(f"{t.id}  [{t.required_role}] {t.kind:18} {t.question[:110]}\n      options: {t.options}  blocks: {t.blocks}")
        else:
            t = hitl.decide(s, _ident(s, args), args.task, args.decision, args.comment or "")
            print(f"{t.kind} -> {t.decision} by {t.decided_by}")


def cmd_dsar(args):
    from .privacy import dsar
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        ident = _ident(s, args)
        if args.action == "export":
            print(json.dumps(dsar.export(s, ident, args.person), indent=2, default=str))
        else:
            t = dsar.request_erasure(s, ident, args.person, args.reason or "data subject request")
            print(f"erasure request opened as review task {t.id} (needs a DPO other than {ident.principal_id})")


def cmd_ai_export(args):
    from .privacy import ai_export
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        print(json.dumps(ai_export.export(s, _ident(s, args), Path(args.out)), indent=2))


SHERPA_ASSETS = [  # (url, kind, keep) - GitHub releases of k2-fsa/sherpa-onnx; checked by size after download
    ("asr-models/sherpa-onnx-whisper-small.tar.bz2", "tar",
     {"small-encoder.int8.onnx", "small-decoder.int8.onnx", "small-tokens.txt"}),
    ("speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", "tar", {"model.onnx"}),
    ("speaker-recongition-models/nemo_en_titanet_small.onnx", "file", None),
    ("asr-models/silero_vad.onnx", "file", None),
]


def _fetch_sherpa(dest) -> None:
    import tarfile
    import tempfile
    import urllib.request
    from pathlib import Path
    base = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    dest.mkdir(parents=True, exist_ok=True)
    for rel, kind, keep in SHERPA_ASSETS:
        name = rel.split("/")[-1]
        if kind == "file":
            if not (dest / name).is_file():
                print(f"downloading {name}")
                urllib.request.urlretrieve(base + rel, dest / name)
            continue
        folder = dest / name.removesuffix(".tar.bz2")
        if folder.is_dir() and all((folder / k).is_file() for k in keep):
            continue
        print(f"downloading {name} (large, once)")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / name
            urllib.request.urlretrieve(base + rel, tmp)
            folder.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tmp, "r:bz2") as tf:
                for m in tf.getmembers():
                    fn = Path(m.name).name
                    if m.isfile() and fn in keep:          # only the files we need; never trust archive paths
                        m.name = fn
                        tf.extract(m, folder, filter="data")


def cmd_fetch_models(args):
    """Download speech models once, outside the sandbox, into the cache the sandbox reads offline."""
    import os
    from pathlib import Path

    from .config import get_settings
    st = get_settings()
    cache = Path(st.model_cache_dir or Path(st.data_dir) / "models")
    cache.mkdir(parents=True, exist_ok=True)
    if args.sherpa or not args.faster_whisper:
        _fetch_sherpa(cache / "sherpa")
        print(f"Whisper (ONNX) + speaker-turn models cached in {cache / 'sherpa'}")
    if args.faster_whisper:
        os.environ["HF_HOME"] = str(cache)
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            sys.exit('faster-whisper is not installed: pip install -e ".[whisper]"')
        WhisperModel(args.model or st.whisper_model, device="cpu", compute_type="int8")
        print(f"faster-whisper model '{args.model or st.whisper_model}' cached in {cache}")


def cmd_retranscribe(args):
    """Re-run speech recognition for recordings that only have a machine transcript (or none)."""
    from .jobs import enqueue
    from .models import Content, File
    bootstrap.init(json_logs=False)
    n = 0
    with db.session_scope() as s:
        for c in s.scalars(select(Content).where(Content.detected_type == "video")):
            src = str((c.media or {}).get("transcript_source") or "")
            if src.startswith("sidecar") or (c.media or {}).get("transcript_inherited_from"):
                continue
            f = s.scalar(select(File).where(File.sha256 == c.sha256, File.status == "active").limit(1))
            if f is None:
                continue
            enqueue(s, "content.extract", {"sha": c.sha256, "file_id": f.id, "force": True},
                    key=f"extract:{c.sha256}:retranscribe:{clock.now().isoformat()}", priority=50)
            n += 1
    print(f"{n} recording(s) queued for transcription")
    if not args.no_wait:
        _drain()


def cmd_optimize(args):
    """Produce and measure renditions for every compression candidate (or one content)."""
    from sqlalchemy import select

    from .jobs import enqueue
    from .models import Content, File, Rendition
    from .optimization.engine import candidates
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        q = select(Content).where(Content.extracted.is_(True))
        if args.sha:
            q = q.where(Content.sha256.startswith(args.sha))
        for c in s.scalars(q):
            if s.scalar(select(File.id).where(File.sha256 == c.sha256, File.status == "active").limit(1)):
                for op in candidates(c):
                    enqueue(s, "content.optimize", {"sha": c.sha256, "op": op}, key=f"optimize:{c.sha256}:{op}")
    _drain()
    with db.session_scope() as s:
        for r in s.scalars(select(Rendition).order_by(Rendition.created_at)):
            v = r.validation
            print(f"{'PASS' if r.passed else 'FAIL'} {r.operation:<16} {v['original_bytes'] / 1e6:8.2f} MB -> "
                  f"{r.size_bytes / 1e6:8.2f} MB ({v['saving_ratio']:.0%})  {r.content_sha[:12]}")
            for ch in v["checks"]:
                print(f"      {'ok ' if ch['passed'] else 'NO '} {ch['name']}: {ch['detail']}")


def cmd_mcp_config(args):
    """Create a read-only key for an AI assistant and print ready-to-paste MCP settings."""
    import shutil
    from .ai.context import ai_sources
    from .config import get_settings
    from .models import Principal, Source
    from .security.auth import create_key
    bootstrap.init(json_logs=False)
    st = get_settings()
    with db.session_scope() as s:
        p = s.get(Principal, args.as_)
        if p is None:
            known = ", ".join(sorted(x.id for x in s.scalars(select(Principal)))[:12])
            raise SystemExit(f"unknown person '{args.as_}'. Known: {known}")
        key = create_key(s, p.id, ["viewer"])            # read-only: the MCP server can never plan or delete
        audit.append(s, p.id, "apikey.created", "principal", p.id, {"roles": ["viewer"], "for": "mcp"})
        opted = sorted(ai_sources(s))
        sources = sorted(x.name for x in s.scalars(select(Source)))
    env = {"KORA_API_KEY": key}
    if args.docker:
        cmd, cargs = "docker", ["compose", "-f", str(Path(args.compose).resolve()), "exec", "-T",
                               "-e", f"KORA_API_KEY={key}", "web", "kora", "mcp"]
        env = {}
    else:
        here = Path(sys.executable).with_name("kora")          # the kora of this environment (.venv)
        exe = here if here.is_file() else Path(shutil.which("kora") or "kora")
        cmd, cargs = str(exe.absolute()), ["mcp"]
        env["KORA_DATA_DIR"] = str(st.data_dir)
        if os.environ.get("KORA_DATABASE_URL"):
            env["KORA_DATABASE_URL"] = os.environ["KORA_DATABASE_URL"]
    entry = {"command": cmd, "args": cargs, **({"env": env} if env else {})}
    print(f"Read-only key created for {p.display_name} ({p.id}): the assistant sees what they may read.\n")
    print("1) Claude Desktop: Settings > Developer > Edit Config, add under \"mcpServers\", then restart Claude:\n")
    print(json.dumps({"mcpServers": {"kora": entry}}, indent=2))
    envs = " ".join(f"--env {k}={v}" for k, v in env.items())
    print("\n2) Claude Code (terminal):\n")
    # --env pairs first: an option must sit between the last --env and the server name
    print(f"claude mcp add {envs + ' ' if envs else ''}--transport stdio kora -- {cmd} {' '.join(cargs)}")
    print("\n3) Then ask, for example: \"Using KORA, what did the GraalVM talk conclude about native images? Cite sources.\"")
    if not opted:
        print(f"\nNOTE: no source is opted in for AI use yet, so answers will be empty. As the data protection "
              f"officer (Yashwitha) open Search & AI in the website and allow one of: {', '.join(sources) or '(none yet)'}")
    else:
        print(f"\nSources opted in for AI use: {', '.join(opted)}")


def cmd_mcp(args):
    """MCP server on stdio for AI assistants (read-only, permissions of KORA_API_KEY)."""
    from .ai.mcp_server import serve
    serve()


def cmd_gc(args):
    from .storage.gc import collect
    bootstrap.init(json_logs=False)
    with db.session_scope() as s:
        print(json.dumps(collect(s, dry_run=not args.delete, min_age_s=args.min_age), indent=2))


def cmd_demo(args):
    from .demo import run
    run(Path(args.samples), fresh=args.fresh, report_dir=Path(args.report_dir) if args.report_dir else None)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kora", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def who(p):
        p.add_argument("--as", dest="as_", required=True, help="acting principal id")
        p.add_argument("--roles", default="analyst", help="comma-separated roles for this action")

    p = sub.add_parser("init"); p.add_argument("--samples"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("principal"); p.add_argument("id"); p.add_argument("--name"); p.add_argument("--groups")
    p.add_argument("--status", default="active"); p.add_argument("--manager"); p.add_argument("--team")
    p.set_defaults(fn=cmd_principal)
    p = sub.add_parser("source"); p.add_argument("name"); p.add_argument("--system", required=True)
    p.add_argument("--root", required=True); p.add_argument("--site"); p.add_argument("--unapproved", action="store_true")
    p.set_defaults(fn=cmd_source)
    p = sub.add_parser("key"); p.add_argument("principal"); p.add_argument("--roles", required=True); p.set_defaults(fn=cmd_key)
    p = sub.add_parser("scan"); p.add_argument("sources", nargs="*"); p.add_argument("--wait", action="store_true")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("worker"); p.add_argument("--drain", action="store_true")
    p.add_argument("--kinds", help="only claim these job kinds (e.g. plan.* executor with write access)")
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("dashboard", help="interactive dashboard on localhost (API + worker + UI)")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--workers", type=int, default=2, help="background workers inside the dashboard process")
    p.set_defaults(fn=cmd_dashboard)
    sub.add_parser("lineages").set_defaults(fn=cmd_lineages)
    sub.add_parser("findings").set_defaults(fn=cmd_findings)
    p = sub.add_parser("plan"); p.add_argument("action", choices=["create", "show", "submit", "approve", "execute",
                                                                  "purge", "restore", "cancel"])
    p.add_argument("target", help="plan kind (create) or plan id"); p.add_argument("--owner"); p.add_argument("--json", action="store_true")
    who(p); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("departure"); p.add_argument("principal"); p.add_argument("--json", action="store_true"); who(p)
    p.set_defaults(fn=cmd_departure)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=8)
    p.add_argument("--json", action="store_true"); who(p); p.set_defaults(fn=cmd_search)
    sub.add_parser("audit-verify").set_defaults(fn=cmd_audit)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    p = sub.add_parser("reconstruct"); p.add_argument("sha"); p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_reconstruct)
    p = sub.add_parser("rebuild-video"); p.add_argument("sha"); p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_rebuild_video)
    p = sub.add_parser("optimize", help="measure compression renditions against the quality budget")
    p.add_argument("--sha", help="only this content (sha prefix)"); p.set_defaults(fn=cmd_optimize)
    p = sub.add_parser("fetch-models", help="download speech models once for offline, sandboxed ASR")
    p.add_argument("--sherpa", action="store_true", help="Whisper ONNX + speaker turns from GitHub releases (default)")
    p.add_argument("--faster-whisper", action="store_true", help="faster-whisper from Hugging Face instead")
    p.add_argument("--model", help="faster-whisper model size or path (default: settings)")
    p.set_defaults(fn=cmd_fetch_models)
    p = sub.add_parser("retranscribe", help="re-run speech recognition for recordings without a platform transcript")
    p.add_argument("--no-wait", action="store_true"); p.set_defaults(fn=cmd_retranscribe)
    sub.add_parser("mcp", help="serve KORA's knowledge to AI assistants over MCP (stdio)").set_defaults(fn=cmd_mcp)
    p = sub.add_parser("mcp-config", help="make a read-only key for an AI assistant and print MCP settings")
    p.add_argument("--as", dest="as_", default="ui-analyst", help="whose permissions the assistant gets (a person's id)")
    p.add_argument("--docker", action="store_true", help="KORA runs with docker compose")
    p.add_argument("--compose", default="docker-compose.yml", help="path to docker-compose.yml (with --docker)")
    p.set_defaults(fn=cmd_mcp_config)
    p = sub.add_parser("ingest-folder", help="run KORA over a folder or Drive zip of real files")
    p.add_argument("path"); p.add_argument("--name", default="ericsson"); p.add_argument("--system", default="sharepoint")
    p.add_argument("--owner", default="ericsson-partner"); p.add_argument("--acl", default="group:hackathon")
    p.add_argument("--workers", type=int, default=1); p.add_argument("--ai-use-allowed", action="store_true")
    p.set_defaults(fn=cmd_ingest_folder)
    p = sub.add_parser("reviews", help="human-in-the-loop review queue")
    p.add_argument("action", choices=["list", "decide"]); p.add_argument("task", nargs="?"); p.add_argument("decision", nargs="?")
    p.add_argument("--comment"); p.add_argument("--as", dest="as_", default="system"); p.add_argument("--roles", default="analyst")
    p.set_defaults(fn=cmd_reviews)
    p = sub.add_parser("dsar", help="GDPR data-subject requests")
    p.add_argument("action", choices=["export", "erase"]); p.add_argument("person"); p.add_argument("--reason")
    who(p); p.set_defaults(fn=cmd_dsar)
    p = sub.add_parser("ai-export", help="export redacted, provenance-tagged knowledge as JSONL")
    p.add_argument("--out", required=True); who(p); p.set_defaults(fn=cmd_ai_export)
    p = sub.add_parser("gc", help="find (and with --delete remove) unreferenced blobs")
    p.add_argument("--delete", action="store_true"); p.add_argument("--min-age", type=int, default=3600)
    p.set_defaults(fn=cmd_gc)
    p = sub.add_parser("demo"); p.add_argument("--samples", default="samples"); p.add_argument("--fresh", action="store_true")
    p.add_argument("--report-dir"); p.set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
