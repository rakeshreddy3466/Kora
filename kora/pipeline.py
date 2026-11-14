"""Ingestion pipeline stages (job handlers).

source.scan  -> file.hash -> content.extract -> content.relate -> content.capsule
                                            \\-> content.index
Each stage:
  * is idempotent (content-addressed blobs, upserts, unique constraints)
  * does heavy work outside DB transactions (sandboxed parsers)
  * commits its results, the next stage's job and its own completion in ONE
    transaction (no lost or phantom work)
  * is keyed so N copies of the same bytes trigger ONE extraction
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from . import EXTRACTOR_VERSION, audit, clock, db, hitl
from .capsule import builder as capsule_builder
from .config import get_settings
from .connectors import get_connector
from .dedup import engine as dedup
from .fingerprint import embed as emb
from .fingerprint import minhash as mh
from .fingerprint.text import shingles, text_hash
from .jobs import PermanentError, enqueue, handler
from .knowledge import notes
from .media import framematch
from .models import Content, File, FrameLink, Posting, Relation, SlideBlob, Source, Unit, Version
from .search.index import index_content
from .security.filetype import UnsafeFile, check_zip, extension_mismatch, sniff
from .security import sandbox
from .storage import get_blobstore
from .util import CHUNK
from .versioning import docnumber, lineage

log = logging.getLogger(__name__)
EXTRACTABLE = {"pdf": "pdf", "pptx": "pptx", "video": "video", "image": "image", "text": "text",
               "docx": "docx", "msg": "msg"}
PRIORITY = {"pdf": 40, "pptx": 40, "docx": 45, "msg": 45, "text": 45, "image": 60, "video": 120}


# --------------------------------------------------------------------------- scan
def _register(s, src: Source, items, seen: set[str]) -> tuple[int, int]:
    new = changed = 0
    for it in items:
        seen.add(it.path)
        f = s.scalar(select(File).where(File.source_name == src.name, File.path == it.path))
        if f is None:
            f = File(source_name=src.name, system=src.system, site=src.config.get("site"), path=it.path,
                     filename=Path(it.path).name, extension=Path(it.path).suffix.lower().lstrip("."),
                     size_bytes=it.size, owner_id=it.owner, acl=it.acl, retention_label=it.retention_label,
                     legal_hold=it.legal_hold, created_at_source=it.created_at, modified_at_source=it.modified_at,
                     meta=it.meta)
            s.add(f)
            s.flush()
            enqueue(s, "file.hash", {"file_id": f.id}, key=f"hash:{f.id}:{it.etag}", priority=30)
            new += 1
            continue
        # metadata may change without content changing (ACL, hold, owner)
        if (f.acl, f.owner_id, f.legal_hold, f.retention_label) != (it.acl, it.owner, it.legal_hold, it.retention_label):
            if f.legal_hold != it.legal_hold:
                audit.append(s, "system", "file.legal_hold_changed", "file", f.id, {"legal_hold": it.legal_hold})
            f.acl, f.owner_id, f.legal_hold, f.retention_label = it.acl, it.owner, it.legal_hold, it.retention_label
        f.last_seen_at = clock.now()
        if f.status in ("missing", "purged"):
            audit.append(s, "system", "file.reappeared", "file", f.id, {"was": f.status})
            f.status = "active"
        if f.source_etag != it.etag and f.status == "active":
            f.size_bytes, f.modified_at_source, f.ingest_state = it.size, it.modified_at, "registered"
            enqueue(s, "file.hash", {"file_id": f.id}, key=f"hash:{f.id}:{it.etag}", priority=30)
            changed += 1
    return new, changed


@handler("source.scan")
def scan(ctx) -> None:
    name = ctx.payload["source"]
    with db.session_scope() as s:
        src = s.get(Source, name)
        if src is None or not src.enabled:
            raise PermanentError(f"source {name} not found or disabled")
        s.expunge(src)
    conn = get_connector(src)
    seen: set[str] = set()
    totals = [0, 0]
    batch = []
    for item in conn.list_items():
        batch.append(item)
        if len(batch) >= 200:
            with db.session_scope() as s:
                n, c = _register(s, src, batch, seen)
                totals[0] += n
                totals[1] += c
            batch = []
    with ctx.tx() as s:
        n, c = _register(s, src, batch, seen)
        totals[0] += n
        totals[1] += c
        missing = 0
        for f in s.scalars(select(File).where(File.source_name == name, File.status == "active")):
            if f.path not in seen:
                f.status = "missing"
                missing += 1
                audit.append(s, "system", "file.missing_at_source", "file", f.id, {})
        s.get(Source, name).last_scan_at = clock.now()
        ctx.result = {"new": totals[0], "changed": totals[1], "missing": missing, "seen": len(seen)}
        audit.append(s, "system", "source.scanned", "source", name, ctx.result)


# --------------------------------------------------------------------------- hash
@handler("file.hash")
def hash_file(ctx) -> None:
    import hashlib
    fid = ctx.payload["file_id"]
    with db.session_scope() as s:
        f = s.get(File, fid)
        if f is None or f.status != "active":
            return
        src = s.get(Source, f.source_name)
        s.expunge(src)
        path, filename = f.path, f.filename
    conn = get_connector(src)
    st0 = conn.stat(path)
    if st0 is None:
        with ctx.tx() as s:
            s.get(File, fid).status = "missing"
        return
    h = hashlib.sha256()
    size = 0
    with conn.open(path) as fh:
        head = fh.read(8192)
        h.update(head)
        size += len(head)
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    st1 = conn.stat(path)
    if st1 is None or st1.etag != st0.etag:
        raise RuntimeError("file changed while hashing; will retry")
    sha = h.hexdigest()
    detected = sniff(head, filename)
    with ctx.tx() as s:
        f = s.get(File, fid)
        c = s.get(Content, sha)
        if c is None:
            try:
                with s.begin_nested():
                    c = Content(sha256=sha, size_bytes=size, detected_type=detected)
                    s.add(c)
                    s.flush()
            except IntegrityError:
                c = s.get(Content, sha)
        f.sha256, f.size_bytes, f.detected_type, f.source_etag = sha, size, detected, st0.etag
        f.last_error = None
        if extension_mismatch(detected, filename):
            f.ingest_state = "rejected"
            f.last_error = f"content is {detected} but extension says otherwise"
            audit.append(s, "system", "file.rejected", "file", fid, {"reason": f.last_error, "sha256": sha})
            return
        if detected in EXTRACTABLE:
            if c.extracted and c.extractor_version == EXTRACTOR_VERSION:
                f.ingest_state = "ready"
                enqueue(s, "content.capsule", {"sha": sha}, key=f"capsule:{sha}:file:{fid}:{st0.etag}")
            else:
                f.ingest_state = "hashed"
                enqueue(s, "content.extract", {"sha": sha, "file_id": fid},
                        key=f"extract:{sha}:v{EXTRACTOR_VERSION}", priority=PRIORITY[detected])
        elif detected == "transcript":
            f.ingest_state = "sidecar"
        else:
            f.ingest_state = "unsupported"        # still participates in exact-copy dedup


# --------------------------------------------------------------------------- extract
def _store_slide(s, uh: str, key: str, size: int, sha: str) -> None:
    if s.get(SlideBlob, uh) is not None:
        return
    try:
        with s.begin_nested():
            s.add(SlideBlob(unit_hash=uh, blob_key=key, size_bytes=size, first_content_sha=sha))
            s.flush()
    except IntegrityError:
        pass


@handler("content.extract")
def extract(ctx) -> None:
    sha = ctx.payload["sha"]
    st = get_settings()
    with db.session_scope() as s:
        c = s.get(Content, sha)
        if c.extracted and c.extractor_version == EXTRACTOR_VERSION and not ctx.payload.get("force"):
            return
        f = s.get(File, ctx.payload.get("file_id"))
        if f is None or f.status != "active" or f.sha256 != sha:
            f = s.scalar(select(File).where(File.sha256 == sha, File.status == "active").limit(1))
        if f is None:
            raise PermanentError("no readable copy of content")
        src = s.get(Source, f.source_name)
        s.expunge(src)
        path, ctype, ext, fid, repo_meta = f.path, c.detected_type, f.extension, f.id, dict(f.meta or {})
        derived = (c.meta or {}).get("rendition_of")
        inherited = None
        if derived and ctype == "video":
            # a transcode that passed the duration/audio budget keeps the original's timeline, so its
            # transcript (and the transcript's provenance, e.g. asr:* still needing review) carries over
            oc = s.get(Content, derived)
            segs = list(s.scalars(select(Unit).where(Unit.content_sha == derived, Unit.kind == "segment")
                                  .order_by(Unit.ordinal)))
            if oc is not None and segs:
                inherited = ((oc.media or {}).get("transcript_source"),
                             [(u.start_s or 0.0, u.end_s or 0.0, u.speaker, u.text) for u in segs])
    conn = get_connector(src)
    store = get_blobstore()
    work = Path(tempfile.mkdtemp(prefix="kora-x-"))
    try:
        import hashlib
        inp = work / f"input.{ext or 'bin'}"
        h = hashlib.sha256()
        with conn.open(path) as fh, open(inp, "wb") as out:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != sha:
            with ctx.tx() as s:
                enqueue(s, "file.hash", {"file_id": fid}, key=f"hash:{fid}:rehash:{clock.now().isoformat()}")
                ctx.result = {"stale": True}
            return
        if ctype in ("pptx", "docx"):
            try:
                check_zip(inp, st.max_zip_uncompressed_mb * 1024 * 1024,
                          expected=b"presentationml" if ctype == "pptx" else b"wordprocessingml")
            except UnsafeFile as e:
                raise PermanentError(f"unsafe {ctype}: {e}") from e
        args = {"path": str(inp)}
        if ctype == "video":
            sc = []
            for i, side in enumerate(conn.sidecars(path)):
                dst = work / f"sidecar-{i}{Path(side).suffix}"
                with conn.open(side) as fh, open(dst, "wb") as out:
                    shutil.copyfileobj(fh, out, CHUNK)
                sc.append(str(dst))
            if inherited:
                from .extraction.transcript import to_vtt
                vtt = work / "inherited.vtt"
                vtt.write_text(to_vtt(inherited[1]), encoding="utf-8")
                sc.insert(0, str(vtt))
            args["sidecars"] = sc
        out_dir = work / "out"
        out_dir.mkdir()
        ctx.heartbeat()
        with ctx.keepalive(progress_dir=out_dir):                # long recordings: ASR can outlive one lease
            res = sandbox.run(EXTRACTABLE[ctype], args, out_dir)
        ctx.heartbeat()

        def put(rel: str | None) -> tuple[str | None, int]:
            if not rel:
                return None, 0
            p = (out_dir / rel).resolve()
            if not p.is_relative_to(out_dir.resolve()) or not p.is_file():
                raise PermanentError("sandbox returned an invalid artifact path")
            ref = store.put_file("hot", p)
            return ref.key, ref.size

        stored_units = []
        if ctype == "video":
            for fr in res["frames"]:
                key, _ = put(fr["file"])
                stored_units.append(("frame", fr, key, None, 0))
            audio_key, _ = put(res.get("audio"))
        else:
            for u in res["units"]:
                thumb, _ = put(u.get("thumb"))
                # lazy slide store: page PDFs are materialised only when a compaction needs them
                blob, bsize = put(u.get("blob")) if st.slide_store == "eager" else (None, 0)
                stored_units.append((u["kind"], u, blob, thumb, bsize))

        with ctx.tx() as s:
            c = s.scalars(select(Content).where(Content.sha256 == sha).with_for_update()).one()
            old = select(Unit.id).where(Unit.content_sha == sha)
            s.execute(delete(Posting).where(Posting.unit_id.in_(old)))
            s.execute(delete(FrameLink).where(FrameLink.frame_unit_id.in_(old) | FrameLink.slide_unit_id.in_(old)))
            s.execute(delete(Unit).where(Unit.content_sha == sha))
            all_text = []
            if ctype == "video":
                p = res["probe"]
                c.duration_s = p["duration"]
                tsrc = res.get("transcript_source")
                if inherited and tsrc == "sidecar:vtt":
                    tsrc = inherited[0] or tsrc               # provenance follows the words, not the file
                c.media = {**p, "temporal_signature": res["temporal_signature"], "audio_blob": audio_key,
                           "transcript_source": tsrc, **({"transcript_inherited_from": derived} if inherited else {}),
                           **({"asr": res["asr_info"]} if res.get("asr_info") else {})}
                for kind, fr, key, _t, _ in stored_units:
                    s.add(Unit(content_sha=sha, kind="frame", ordinal=fr["ordinal"], start_s=fr["t"], end_s=fr["end"],
                               phash=fr["phash"], text=fr.get("ocr", ""), text_hash=text_hash(fr.get("ocr", "")),
                               blob_key=key, meta={}))
                for i, seg in enumerate(res["segments"], start=1):
                    s.add(Unit(content_sha=sha, kind="segment", ordinal=i, start_s=seg["start"], end_s=seg["end"],
                               speaker=seg.get("speaker"), text=seg["text"], text_hash=text_hash(seg["text"]),
                               minhash=mh.to_bytes(mh.signature(shingles(seg["text"]))), meta={}))
                    all_text.append(seg["text"])
                c.unit_count = len(stored_units) + len(res["segments"])
                prior = (s.get(Content, derived).meta or {}).get("transcript_review") if inherited else None
                c.meta = {"title": p.get("title"), "creation_time": p.get("creation_time"),
                          **({"rendition_of": derived} if derived else {}),
                          **({"transcript_review": {**prior, "via": derived}} if prior else {})}
                c.normalized_hash = None
            else:
                for kind, u, blob, thumb, bsize in stored_units:
                    body = u.get("body_text", u["text"])
                    s.add(Unit(content_sha=sha, kind=kind, ordinal=u["ordinal"], text=u["text"], text_hash=u["text_hash"],
                               speaker=u.get("speaker"),
                               unit_hash=u["unit_hash"], body_hash=u.get("body_hash"), phash=u.get("phash"),
                               minhash=mh.to_bytes(mh.signature(shingles(body))), thumb_key=thumb,
                               blob_key=blob, meta=u.get("meta", {})))
                    if blob and u.get("unit_hash"):
                        _store_slide(s, u["unit_hash"], blob, bsize, sha)
                    all_text.append(body)        # footers excluded: they are noise for similarity
                c.unit_count = len(stored_units)
                c.normalized_hash = res.get("normalized_hash")
                meta = {k: v for k, v in (res.get("metadata") or {}).items() if v not in (None, "")}
                first = stored_units[0][1]["text"] if stored_units else ""
                last = stored_units[-1][1]["text"] if stored_units else ""
                meta.update(docnumber.detect(meta, first, last, repo_meta))
                if not meta.get("title") and first:
                    meta["title"] = first.strip().split("\n")[0][:200]
                    meta["title_source"] = "first_page"
                if ctype == "pptx":
                    meta["rendered"] = bool(res.get("rendered"))
                if derived:
                    meta["rendition_of"] = derived       # lineage of a compressed file is never lost
                    prior_pii = (s.get(Content, derived).meta or {}).get("pii_review")
                    if prior_pii:
                        meta["pii_review"] = {**prior_pii, "via": derived}
                c.meta = meta
            joined = "\n".join(all_text)
            c.text_chars = len(joined)
            c.minhash = mh.to_bytes(mh.signature(shingles(joined)))
            c.embedding = emb.to_bytes(emb.embed(joined)) if joined.strip() else None
            c.extracted, c.extractor_version = True, EXTRACTOR_VERSION
            s.flush()
            if ctype == "pptx":
                notes.apply(s, sha)                   # speaker notes: owner, best-before, personal data
            dedup.index_lsh(s, c)
            for f in s.scalars(select(File).where(File.sha256 == sha)):
                f.ingest_state = "ready"
            enqueue(s, "content.relate", {"sha": sha}, key=f"relate:{sha}:v{EXTRACTOR_VERSION}", priority=50)
            enqueue(s, "content.index", {"sha": sha}, key=f"index:{sha}:v{EXTRACTOR_VERSION}", priority=70)
            audit.append(s, "system", "content.extracted", "content", sha,
                         {"type": ctype, "units": c.unit_count, "bytes": c.size_bytes})
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- relate / capsule / index
def _touched(s, sha: str) -> set[str]:
    out = set()
    for r in s.scalars(select(Relation).where((Relation.a_sha == sha) | (Relation.b_sha == sha))):
        out.add(r.b_sha if r.a_sha == sha else r.a_sha)
    return out


@handler("content.relate")
def relate(ctx) -> None:
    sha = ctx.payload["sha"]
    with ctx.tx() as s:
        c = s.get(Content, sha)
        before = _touched(s, sha)
        v = lineage.assign(s, c)
        relink = dedup.relate(s, c)
        if c.detected_type == "video":
            framematch.link(s, c)
        for vid in relink:
            enqueue(s, "content.relink", {"sha": vid}, key=f"relink:{vid}:{sha}", priority=55)
        enqueue(s, "content.capsule", {"sha": sha}, key=f"capsule:{sha}:{ctx.job.id}", priority=80)
        hitl.generate(s, sha)                     # route anything ambiguous to a person
        from .governance.policy import get_policy
        from .optimization.engine import candidates
        if get_policy().get("compression", {}).get("auto", True):
            for op in candidates(c):
                enqueue(s, "content.optimize", {"sha": sha, "op": op}, key=f"optimize:{sha}:{op}", priority=150)
        others = _touched(s, sha) - before
        if v is not None:
            others |= {x.content_sha for x in s.scalars(select(Version).where(Version.lineage_id == v.lineage_id))}
        for o in others - {sha}:
            enqueue(s, "content.capsule", {"sha": o}, key=f"capsule:{o}:{ctx.job.id}", priority=90)
        ctx.result = {"lineage": v.lineage_id if v else None, "relinked_videos": relink}


@handler("content.relink")
def relink(ctx) -> None:
    sha = ctx.payload["sha"]
    with ctx.tx() as s:
        ctx.result = framematch.link(s, s.get(Content, sha))
        enqueue(s, "content.capsule", {"sha": sha}, key=f"capsule:{sha}:{ctx.job.id}", priority=80)


@handler("content.capsule")
def capsule(ctx) -> None:
    sha = ctx.payload["sha"]
    with ctx.tx() as s:
        c = s.get(Content, sha)
        if not c.extracted:
            return
        cap = capsule_builder.build(s, sha)
        hitl.generate(s, sha)
        ctx.result = {"valid": cap.valid, "manifest_hash": cap.manifest_hash}
        audit.append(s, "system", "capsule.built", "content", sha,
                     {"manifest_hash": cap.manifest_hash, "valid": cap.valid, "bytes": cap.size_bytes})


@handler("content.index")
def index(ctx) -> None:
    sha = ctx.payload["sha"]
    with ctx.tx() as s:
        ctx.result = {"indexed_units": index_content(s, sha)}


@handler("source.ingest_folder")
def ingest_folder(ctx) -> None:
    """Copy a folder (or a Drive zip) into the workspace, register it as a source, scan it.
    KORA works on the copy, so plans can never touch the originals."""
    import json as _json

    from . import bootstrap
    from .config import get_settings
    from .runner import stage_input
    p = ctx.payload
    src, name = Path(p["path"]).expanduser(), p["name"]
    dest = get_settings().data_dir / "sources" / name
    with ctx.keepalive():
        staged = stage_input(src, dest)
    (dest / "_kora_source.json").write_text(_json.dumps({"default_owner": p["owner"], "default_acl": p["acl"]}))
    with ctx.tx() as s:
        bootstrap.upsert_principal(s, {"id": p["owner"], "display_name": p["owner"]})
        bootstrap.upsert_source(s, name, p.get("system", "sharepoint"), str(dest), name)
        enqueue(s, "source.scan", {"source": name}, key=f"scan:{name}:{clock.now().isoformat()}", priority=10)
        audit.append(s, p.get("by", "system"), "source.ingested", "source", name, {"files": staged.get("files")})
        ctx.result = staged


@handler("capsule.rebuild")
def rebuild(ctx) -> None:
    """Recreate a watchable video from the capsule alone (keyframes + speech + subtitles)."""
    from .capsule.rebuild import rebuild_video
    sha = ctx.payload["sha"]
    work = Path(tempfile.mkdtemp(prefix="kora-rb-"))
    try:
        with ctx.keepalive():
            with db.session_scope() as s:
                info = rebuild_video(s, sha, work / "rebuilt.mp4")
        ref = get_blobstore().put_file("hot", work / "rebuilt.mp4")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with ctx.tx() as s:
        c = s.get(Content, sha)
        c.media = {**(c.media or {}), "rebuilt": {"key": ref.key, "bytes": ref.size, "at": clock.now().isoformat(),
                                                   "keyframes": info["keyframes"], "audio": info["audio"],
                                                   "subtitles": info["subtitles"]}}
        audit.append(s, "system", "capsule.rebuilt", "content", sha, {"bytes": ref.size})
        ctx.result = {"bytes": ref.size}


@handler("content.optimize")
def optimize(ctx) -> None:
    from .optimization import engine as opt
    ctx.heartbeat()
    where = {}
    with ctx.keepalive(progress_dir=lambda: where.get("dir")):  # transcodes can outlive one lease
        r = opt.run(ctx.payload["sha"], ctx.payload["op"],      # transcode/measure outside any transaction
                    on_work_dir=lambda d: where.__setitem__("dir", d))
    with ctx.tx() as s:
        if r is None:
            ctx.result = {"skipped": "no live copy or source changed"}
            return
        ctx.result = {"rendition": r.id, "passed": r.passed, "saving": r.validation.get("saving_ratio")}
        audit.append(s, "system", "rendition.created", "content", ctx.payload["sha"],
                     {"op": r.operation, "passed": r.passed, "bytes": r.size_bytes,
                      "failed": [c["name"] for c in r.validation["checks"] if not c["passed"]]})


# --------------------------------------------------------------------------- governance jobs
from .governance import executor  # noqa: E402,F401  (registers plan.* handlers)
