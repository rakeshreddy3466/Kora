"""HTTP API (FastAPI). Every route is authenticated (Bearer API key) and
role-checked; content routes are additionally ACL-filtered. Errors never leak
stack traces; every response carries a request id that also tags the logs.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File as UploadFileField, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from .. import __version__, audit, bootstrap, clock, db
from ..capsule import builder as capsule_builder
from ..config import get_settings
from ..governance import departure, executor, planner
from ..governance.executor import PlanStateError
from ..governance.planner import PlanError
from ..jobs import enqueue, retry_dead
from ..logs import correlation_id
from ..models import (Capsule, Content, DeletionPlan, File, Job, Lineage, Relation, Source, Unit, Version)
from ..queries import display_name, unit_views
from ..reports import savings
from ..search.index import search as do_search
from ..security.acl import can_read
from ..security.auth import Identity, authenticate
from ..storage import get_blobstore
from ..util import is_sha256
from ..versioning.diff import diff as slide_diff

log = logging.getLogger(__name__)


def get_session():
    with db.session_scope() as s:
        yield s


SessionDep = Annotated[Session, Depends(get_session)]


# Declared as a security scheme so the API docs (/docs) get an "Authorize" button.
bearer = HTTPBearer(auto_error=False, scheme_name="KORA API key",
                    description="Paste one key (kora_...) from .kora-data/secrets/ui-personas.json. "
                                "Each key is one demo person with its own roles.")


def identity(request: Request, s: SessionDep,
             cred: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None) -> Identity:
    raw = cred.credentials.strip() if cred else None
    ident = authenticate(s, raw)
    if ident is None:
        raise HTTPException(401, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = ident.principal_id
    return ident


IdentDep = Annotated[Identity, Depends(identity)]


def need(ident: Identity, *roles: str) -> None:
    if not any(ident.has(r) for r in roles):
        raise HTTPException(403, f"requires role: {' or '.join(roles)}")


def readable_content(s: Session, ident: Identity, sha: str) -> bool:
    toks = ident.acl_tokens
    if any(can_read(toks, f.acl) for f in s.scalars(select(File).where(File.sha256 == sha))):
        return True
    cap = s.get(Capsule, sha)
    return bool(cap and can_read(toks, cap.manifest.get("acl_snapshot", [])))


# --------------------------------------------------------------------------- request bodies
class HoldBody(BaseModel):
    legal_hold: bool
    reason: str = Field(min_length=3, max_length=500)

class PlanBody(BaseModel):
    kind: str
    scope: dict[str, Any] = {}
    title: str | None = Field(default=None, max_length=300)

class ApproveBody(BaseModel):
    comment: str = Field(default="", max_length=500)
    expected_version: int | None = None

class RejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

class RestoreBody(BaseModel):
    item_ids: list[str] | None = None

class DecideBody(BaseModel):
    decision: str = Field(max_length=100)
    comment: str = Field(default="", max_length=1000)

class ErasureBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

class IngestBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,40}$")
    owner: str = Field(default="me", pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    system: str = Field(default="sharepoint", pattern=r"^(sharepoint|onedrive|play)$")

class AiUseBody(BaseModel):
    allowed: bool
    reason: str = Field(min_length=3, max_length=500)


# --------------------------------------------------------------------------- app
TAGS = ["Dashboard", "Sources and files", "Pipeline and jobs", "Duplicates and versions", "Recordings",
        "Compression", "Deletion plans", "Human review", "Search and AI", "Privacy (GDPR)", "Audit", "Operations"]
_TAG_BY_PREFIX = [
    ("/v1/me", "Dashboard"), ("/v1/overview", "Dashboard"), ("/v1/inventory", "Dashboard"),
    ("/v1/scenarios", "Dashboard"), ("/v1/reports", "Dashboard"),
    ("/v1/sources/{name}/ai-use", "Search and AI"), ("/v1/sources", "Sources and files"),
    ("/v1/files", "Sources and files"), ("/v1/uploads", "Sources and files"), ("/v1/ingest-folder", "Sources and files"),
    ("/v1/blobs", "Sources and files"),
    ("/v1/pipeline", "Pipeline and jobs"), ("/v1/jobs", "Pipeline and jobs"),
    ("/v1/relations", "Duplicates and versions"), ("/v1/lineages", "Duplicates and versions"),
    ("/v1/findings", "Duplicates and versions"),
    ("/v1/recordings", "Recordings"), ("/v1/capsules", "Recordings"), ("/v1/units", "Recordings"),
    ("/v1/renditions", "Compression"), ("/v1/content", "Compression"),
    ("/v1/plans", "Deletion plans"), ("/v1/departures", "Deletion plans"),
    ("/v1/reviews", "Human review"),
    ("/v1/search", "Search and AI"), ("/v1/ai", "Search and AI"),
    ("/v1/dsar", "Privacy (GDPR)"), ("/v1/audit", "Audit"),
]


def _tag(path: str) -> str:
    for prefix, tag in _TAG_BY_PREFIX:
        if path.startswith(prefix):
            return tag
    return "Operations"


def create_app(init: bool = True) -> FastAPI:
    if init:
        bootstrap.init()
    app = FastAPI(title="KORA", version=__version__, docs_url=None, redoc_url=None,
                  description="Knowledge Optimization, Recovery & Assurance. Click **Authorize** and paste an "
                              "API key from `.kora-data/secrets/ui-personas.json` before trying a call.",
                  openapi_tags=[{"name": t} for t in TAGS])

    @app.middleware("http")
    async def _ctx(request: Request, call_next):
        rid = request.headers.get("x-request-id")
        rid = rid if rid and re.fullmatch(r"[A-Za-z0-9\-]{8,64}", rid) else uuid.uuid4().hex
        tok = correlation_id.set(rid)
        try:
            resp = await call_next(request)
        finally:
            correlation_id.reset(tok)
        resp.headers["x-request-id"] = rid
        resp.headers["x-content-type-options"] = "nosniff"
        resp.headers["x-frame-options"] = "DENY"
        resp.headers["referrer-policy"] = "no-referrer"
        resp.headers["cache-control"] = "no-store"
        if request.url.path.startswith("/ui"):
            resp.headers["content-security-policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
                "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        elif not request.url.path.startswith("/docs") and request.url.path != "/openapi.json":
            resp.headers["content-security-policy"] = "default-src 'none'; frame-ancestors 'none'"
        return resp

    @app.exception_handler(PermissionError)
    async def _perm(_r, e):
        return JSONResponse({"error": "forbidden", "detail": str(e)}, 403)

    @app.exception_handler(PlanStateError)
    async def _state(_r, e):
        return JSONResponse({"error": "conflict", "detail": str(e)}, 409)

    @app.exception_handler(PlanError)
    async def _plan(_r, e):
        return JSONResponse({"error": "bad_request", "detail": str(e)}, 400)

    @app.exception_handler(ValueError)
    async def _val(_r, e):
        return JSONResponse({"error": "bad_request", "detail": str(e)}, 400)

    @app.exception_handler(Exception)
    async def _any(_r, e):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal", "request_id": correlation_id.get()}, 500)

    # ------------------------------------------------------------------ ops
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/readyz")
    def readyz(s: SessionDep):
        s.execute(text("SELECT 1"))
        return {"ok": True, "db": db.engine().dialect.name}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(s: SessionDep, ident: IdentDep):
        need(ident, "operator", "auditor")
        lines = []
        for st, n in s.execute(select(Job.status, func.count()).group_by(Job.status)):
            lines.append(f'kora_jobs{{status="{st}"}} {n}')
        for st, n, b in s.execute(select(File.status, func.count(), func.sum(File.size_bytes)).group_by(File.status)):
            lines.append(f'kora_files{{status="{st}"}} {n}')
            lines.append(f'kora_file_bytes{{status="{st}"}} {b or 0}')
        for st, n in s.execute(select(DeletionPlan.status, func.count()).group_by(DeletionPlan.status)):
            lines.append(f'kora_plans{{status="{st}"}} {n}')
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ sources & files
    @app.post("/v1/sources/{name}/scan", status_code=202)
    def scan(name: str, s: SessionDep, ident: IdentDep):
        need(ident, "analyst")
        if s.get(Source, name) is None:
            raise HTTPException(404, "unknown source")
        jid = enqueue(s, "source.scan", {"source": name}, key=f"scan:{name}:{clock.now().isoformat()}", priority=10)
        audit.append(s, ident.principal_id, "source.scan_requested", "source", name, {"job": jid})
        return {"job_id": jid}

    @app.post("/v1/uploads", status_code=202)
    async def upload(s: SessionDep, ident: IdentDep, file: UploadFile = UploadFileField(...)):
        need(ident, "analyst")
        st = get_settings()
        src = s.get(Source, "upload")
        if src is None:
            root = st.data_dir / "uploads"
            root.mkdir(parents=True, exist_ok=True)
            src = bootstrap.upsert_source(s, "upload", "upload", str(root), "uploads", approved=False)
            s.flush()
        root = Path(src.config["root"])
        base = re.sub(r"[^A-Za-z0-9._\- ()]+", "_", Path(file.filename or "upload.bin").name)[:150] or "upload.bin"
        rel = f"{clock.now():%Y/%m/%d}/{secrets.token_hex(4)}-{base}"
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        limit, n = st.max_upload_mb * 1024 * 1024, 0
        with open(dst, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                n += len(chunk)
                if n > limit:
                    out.close()
                    dst.unlink()
                    raise HTTPException(413, "upload exceeds limit")
                out.write(chunk)
        from ..pipeline import _register
        from ..connectors import get_connector
        item = get_connector(src).stat(rel)
        item.owner, item.acl = ident.principal_id, [f"user:{ident.principal_id}"]
        _register(s, src, [item], set())
        audit.append(s, ident.principal_id, "file.uploaded", "source", "upload", {"bytes": n})
        f = s.scalar(select(File).where(File.source_name == "upload", File.path == rel))
        return {"file_id": f.id, "path": rel, "bytes": n}

    @app.get("/v1/files")
    def files(s: SessionDep, ident: IdentDep, source: str | None = None, status: str | None = None,
              limit: int = Query(200, le=2000)):
        q = select(File)
        if source:
            q = q.where(File.source_name == source)
        if status:
            q = q.where(File.status == status)
        out = []
        for f in s.scalars(q.order_by(File.source_name, File.path)):
            if not (ident.has("auditor") or can_read(ident.acl_tokens, f.acl)):
                continue
            out.append({"id": f.id, "system": f.system, "site": f.site, "path": f.path, "size_bytes": f.size_bytes,
                        "sha256": f.sha256, "type": f.detected_type, "status": f.status, "state": f.ingest_state,
                        "owner": f.owner_id, "legal_hold": f.legal_hold, "retention_label": f.retention_label})
            if len(out) >= limit:
                break
        return {"files": out}


    @app.post("/v1/files/{file_id}/legal-hold")
    def legal_hold(file_id: str, body: HoldBody, s: SessionDep, ident: IdentDep):
        need(ident, "admin")
        f = s.get(File, file_id)
        if f is None:
            raise HTTPException(404, "unknown file")
        f.legal_hold = body.legal_hold
        audit.append(s, ident.principal_id, "file.legal_hold_set", "file", f.id,
                     {"legal_hold": body.legal_hold, "reason": body.reason})
        return {"id": f.id, "legal_hold": f.legal_hold}

    # ------------------------------------------------------------------ versions
    def _lineage_json(s: Session, lin: Lineage, ident: Identity) -> dict | None:
        vs = sorted(lin.versions, key=lambda v: (v.ordinal, v.variant_of_id is not None))
        if not any(readable_content(s, ident, v.content_sha) for v in vs):
            return None
        return {"id": lin.id, "key": lin.key, "doc_number": lin.doc_number, "title": lin.title,
                "ordering": {"basis": lin.ordering_basis, "confidence": lin.ordering_confidence},
                "has_conflict": lin.has_conflict,
                "versions": [{"id": v.id, "content": v.content_sha, "name": display_name(s, v.content_sha),
                              "revision": v.revision_label, "scheme": v.revision_scheme, "source": v.revision_source,
                              "released": v.released, "ordinal": v.ordinal, "is_head": v.is_head,
                              "variant_of": v.variant_of_id, "conflict": v.conflict,
                              "diff_from_parent": v.diff_from_parent} for v in vs]}

    @app.get("/v1/lineages")
    def lineages(s: SessionDep, ident: IdentDep):
        return {"lineages": [j for lin in s.scalars(select(Lineage).order_by(Lineage.key))
                             if (j := _lineage_json(s, lin, ident))]}

    @app.get("/v1/lineages/{lid}")
    def lineage(lid: str, s: SessionDep, ident: IdentDep):
        lin = s.get(Lineage, lid)
        j = _lineage_json(s, lin, ident) if lin else None
        if j is None:
            raise HTTPException(404, "not found")
        return j

    @app.get("/v1/lineages/{lid}/diff")
    def lineage_diff(lid: str, from_version: str, to_version: str, s: SessionDep, ident: IdentDep):
        a, b = s.get(Version, from_version), s.get(Version, to_version)
        if not a or not b or a.lineage_id != lid or b.lineage_id != lid:
            raise HTTPException(404, "versions not in lineage")
        if not (readable_content(s, ident, a.content_sha) and readable_content(s, ident, b.content_sha)):
            raise HTTPException(404, "not found")
        return slide_diff(unit_views(s, a.content_sha), unit_views(s, b.content_sha))

    # ------------------------------------------------------------------ graph
    @app.get("/v1/relations")
    def relations(s: SessionDep, ident: IdentDep, kind: str | None = None):
        need(ident, "analyst", "auditor")
        q = select(Relation)
        if kind:
            q = q.where(Relation.kind == kind)
        return {"relations": [{"kind": r.kind, "a": r.a_sha, "a_name": display_name(s, r.a_sha), "b": r.b_sha,
                               "b_name": display_name(s, r.b_sha), "score": r.score, "details": r.details}
                              for r in s.scalars(q.order_by(Relation.kind))]}

    @app.get("/v1/findings")
    def findings(s: SessionDep, ident: IdentDep):
        need(ident, "analyst", "auditor")
        counts = dict(s.execute(select(Relation.kind, func.count()).group_by(Relation.kind)).all())
        groups = s.execute(select(File.sha256, func.count()).where(File.status == "active", File.sha256.is_not(None))
                           .group_by(File.sha256).having(func.count() > 1)).all()
        counts["EXACT_COPY_GROUPS"] = len(groups)
        counts["EXACT_COPY_EXTRA_FILES"] = sum(n - 1 for _, n in groups)
        return {"counts": counts}

    # ------------------------------------------------------------------ plans




    def _plan_json(p: DeletionPlan, full: bool = True) -> dict:
        d = {"id": p.id, "kind": p.kind, "title": p.title, "status": p.status, "created_by": p.created_by,
             "required_approvals": p.required_approvals, "approvals": p.approvals, "readiness": p.readiness,
             "quarantine_until": p.quarantine_until.isoformat() if p.quarantine_until else None,
             "version": p.row_version, "summary": p.evidence.get("summary"),
             "items": [{"id": it.id, "file_id": it.file_id, "class": it.dup_class, "state": it.state,
                        "deletable": it.deletable, "readiness": it.readiness, "error": it.error} for it in p.items]}
        if full:
            d["evidence"] = p.evidence
        return d

    @app.post("/v1/plans", status_code=201)
    def create_plan(body: PlanBody, s: SessionDep, ident: IdentDep):
        p = planner.create(s, ident, body.kind, body.scope, body.title)
        return _plan_json(p)

    GOVERNANCE_ROLES = ("analyst", "approver", "operator", "auditor")

    @app.get("/v1/plans")
    def list_plans(s: SessionDep, ident: IdentDep, status: str | None = None):
        q = select(DeletionPlan).order_by(DeletionPlan.created_at.desc())
        if status:
            q = q.where(DeletionPlan.status == status)
        if not any(ident.has(r) for r in GOVERNANCE_ROLES):
            q = q.where(DeletionPlan.created_by == ident.principal_id)     # file owners see their own requests
        return {"plans": [_plan_json(p, full=False) for p in s.scalars(q)]}

    @app.get("/v1/plans/{pid}")
    def get_plan(pid: str, s: SessionDep, ident: IdentDep):
        p = s.get(DeletionPlan, pid)
        if p is None or not (any(ident.has(r) for r in GOVERNANCE_ROLES) or p.created_by == ident.principal_id):
            raise HTTPException(404, "not found")
        return _plan_json(p)

    @app.post("/v1/plans/{pid}/submit")
    def submit(pid: str, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.submit(s, ident, pid), False)

    @app.post("/v1/plans/{pid}/approve")
    def approve(pid: str, body: ApproveBody, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.approve(s, ident, pid, body.comment, body.expected_version), False)

    @app.post("/v1/plans/{pid}/reject")
    def reject(pid: str, body: RejectBody, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.reject(s, ident, pid, body.reason), False)

    @app.post("/v1/plans/{pid}/cancel")
    def cancel(pid: str, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.cancel(s, ident, pid), False)

    @app.post("/v1/plans/{pid}/execute", status_code=202)
    def execute(pid: str, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.execute(s, ident, pid), False)

    @app.post("/v1/plans/{pid}/purge", status_code=202)
    def purge(pid: str, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.purge(s, ident, pid), False)

    @app.post("/v1/plans/{pid}/restore", status_code=202)
    def restore(pid: str, body: RestoreBody, s: SessionDep, ident: IdentDep):
        return _plan_json(executor.restore(s, ident, pid, body.item_ids), False)

    @app.post("/v1/departures/{principal_id}/assess")
    def assess(principal_id: str, s: SessionDep, ident: IdentDep):
        return departure.assess(s, ident, principal_id)

    # ------------------------------------------------------------------ knowledge
    @app.get("/v1/search")
    def search(s: SessionDep, ident: IdentDep, q: str = Query(min_length=2, max_length=300),
               limit: int = Query(10, ge=1, le=50)):
        need(ident, "viewer", "analyst", "auditor")
        return do_search(s, ident, q, limit)

    @app.get("/v1/capsules/{sha}")
    def capsule(sha: str, s: SessionDep, ident: IdentDep):
        if not is_sha256(sha) or not readable_content(s, ident, sha):
            raise HTTPException(404, "not found")
        cap = s.get(Capsule, sha)
        if cap is None:
            raise HTTPException(404, "not found")
        return {"manifest": cap.manifest, "manifest_hash": cap.manifest_hash, "signature": cap.signature,
                "signature_valid": capsule_builder.verify_signature(cap), "validation": cap.validation}

    @app.get("/v1/blobs/{key}")
    def blob(key: str, s: SessionDep, ident: IdentDep):
        if not is_sha256(key):
            raise HTTPException(404, "not found")
        u = s.scalar(select(Unit).where(or_(Unit.thumb_key == key, Unit.blob_key == key)).limit(1))
        owner_sha = u.content_sha if u else s.scalar(select(Content.sha256).where(
            Content.media["audio_blob"].as_string() == key).limit(1))
        owners = [owner_sha] if owner_sha else []
        if not owners:
            owners = [x for x in (
                s.scalar(select(Content.sha256).where(Content.media["rebuilt"]["key"].as_string() == key).limit(1)),
                s.scalar(select(Unit.content_sha).where(Unit.meta["spec"]["image"].as_string() == key).limit(1))) if x]
        if not owners:
            from .insights import preview_owners
            owners = preview_owners(s, key)
        if not any(readable_content(s, ident, o) for o in owners):
            raise HTTPException(404, "not found")
        data = get_blobstore().get_bytes("hot", key)
        mt = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "application/pdf" if data[:5] == b"%PDF-" else \
            "audio/ogg" if data[:4] == b"OggS" else "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else \
            "video/mp4" if data[4:8] == b"ftyp" else "application/octet-stream"
        return Response(data, media_type=mt)

    @app.post("/v1/content/{sha}/optimize", status_code=202)
    def optimize(sha: str, s: SessionDep, ident: IdentDep):
        need(ident, "analyst")
        from ..optimization.engine import candidates
        c = s.get(Content, sha)
        if c is None:
            raise HTTPException(404, "not found")
        ops = candidates(c)
        jobs = [enqueue(s, "content.optimize", {"sha": sha, "op": op}, key=f"opt:{sha}:{op}") for op in ops]
        return {"operations": ops, "jobs": jobs}

    # ------------------------------------------------------------------ human in the loop + GDPR
    @app.get("/v1/reviews")
    def reviews(s: SessionDep, ident: IdentDep, status: str = "open"):
        from ..models import ReviewTask
        need(ident, "analyst", "approver", "dpo", "auditor")
        rows = s.scalars(select(ReviewTask).where(ReviewTask.status == status).order_by(ReviewTask.priority))
        return {"reviews": [{"id": t.id, "kind": t.kind, "question": t.question, "options": t.options,
                             "evidence": t.evidence, "required_role": t.required_role, "blocks": t.blocks,
                             "status": t.status, "decision": t.decision, "decided_by": t.decided_by}
                            for t in rows if ident.has(t.required_role) or ident.has("auditor")]}

    @app.post("/v1/reviews/{tid}/decide")
    def decide_review(tid: str, body: DecideBody, s: SessionDep, ident: IdentDep):
        from .. import hitl
        try:
            t = hitl.decide(s, ident, tid, body.decision, body.comment)
        except hitl.ReviewError as e:
            raise HTTPException(409, str(e))
        return {"id": t.id, "kind": t.kind, "decision": t.decision, "decided_by": t.decided_by}

    @app.get("/v1/dsar/{person}/export")
    def dsar_export(person: str, s: SessionDep, ident: IdentDep):
        from ..privacy import dsar
        return dsar.export(s, ident, person)

    @app.post("/v1/dsar/{person}/erasure", status_code=202)
    def dsar_erasure(person: str, body: ErasureBody, s: SessionDep, ident: IdentDep):
        from ..privacy import dsar
        need(ident, "analyst", "dpo")
        t = dsar.request_erasure(s, ident, person, body.reason)
        return {"review_task": t.id, "status": t.status}

    @app.post("/v1/ai-dataset", status_code=201)
    def ai_dataset(s: SessionDep, ident: IdentDep):
        from ..privacy import ai_export
        out = get_settings().data_dir / "exports"
        out.mkdir(parents=True, exist_ok=True)
        return ai_export.export(s, ident, out / f"knowledge-{clock.now():%Y%m%dT%H%M%S}.jsonl")

    # ------------------------------------------------------------------ audit, jobs, reports
    @app.get("/v1/audit")
    def audit_log(s: SessionDep, ident: IdentDep, limit: int = Query(100, le=1000), subject_id: str | None = None):
        need(ident, "auditor")
        from ..models import AuditEvent
        q = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
        if subject_id:
            q = q.where(AuditEvent.subject_id == subject_id)
        return {"events": [{"seq": e.seq, "at": e.at.isoformat(), "actor": e.actor, "action": e.action,
                            "subject": f"{e.subject_type}:{e.subject_id}", "details": e.details, "hash": e.hash}
                           for e in s.scalars(q)]}

    @app.get("/v1/audit/verify")
    def audit_verify(s: SessionDep, ident: IdentDep):
        need(ident, "auditor")
        r = audit.verify(s)
        return {"ok": r.ok, "events": r.events, "head_hash": r.head_hash, "first_bad_seq": r.first_bad_seq,
                "reason": r.reason}

    @app.get("/v1/jobs")
    def jobs(s: SessionDep, ident: IdentDep, status: str | None = None, limit: int = Query(100, le=1000)):
        need(ident, "operator")
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if status:
            q = q.where(Job.status == status)
        return {"jobs": [{"id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts,
                          "last_error": (j.last_error or "")[-500:], "payload": j.payload} for j in s.scalars(q)]}

    @app.post("/v1/jobs/{jid}/retry")
    def retry(jid: str, s: SessionDep, ident: IdentDep):
        need(ident, "operator")
        return {"retried": retry_dead(jid)}

    # ------------------------------------------------------------------ dashboard read models
    @app.get("/v1/me")
    def me(s: SessionDep, ident: IdentDep):
        from ..models import Principal
        p = s.get(Principal, ident.principal_id)
        import os
        return {"id": ident.principal_id, "name": p.display_name if p else ident.principal_id,
                "roles": sorted(ident.roles), "groups": list(ident.groups),
                "import_hint": os.environ.get("KORA_IMPORT_HINT") or None}

    @app.get("/v1/overview")
    def overview(s: SessionDep, ident: IdentDep, refresh: bool = False):
        need(ident, "analyst", "approver", "operator", "auditor", "dpo")
        from .insights import overview as ov
        return ov(s, ident, refresh)

    @app.get("/v1/inventory")
    def inventory(s: SessionDep, ident: IdentDep):
        from .insights import inventory as inv
        return {"files": inv(s, ident)}

    @app.get("/v1/renditions")
    def renditions(s: SessionDep, ident: IdentDep):
        from .insights import renditions as rs
        return {"renditions": rs(s, ident)}

    @app.get("/v1/pipeline")
    def pipeline_state(s: SessionDep, ident: IdentDep):
        need(ident, "analyst", "operator", "auditor", "dpo", "approver")
        from .insights import pipeline
        return pipeline(s)

    @app.post("/v1/ingest-folder", status_code=202)
    def ingest_folder(body: IngestBody, s: SessionDep, ident: IdentDep):
        """Local, single-user mode only (`kora dashboard`): copy a folder on this machine and process it."""
        need(ident, "analyst")
        if not get_settings().allow_local_ingest:
            raise HTTPException(403, "adding local folders is only enabled in `kora dashboard` (local mode)")
        src = Path(body.path).expanduser()
        if not src.exists() or not (src.is_dir() or src.suffix.lower() == ".zip"):
            raise HTTPException(400, f"not a folder or .zip on this machine: {body.path}")
        if s.get(Source, body.name) is not None:
            raise HTTPException(409, "a source with that name exists; pick another name or rescan it")
        jid = enqueue(s, "source.ingest_folder", {"path": str(src), "name": body.name, "owner": body.owner,
                                                   "acl": [f"user:{body.owner}", "group:everyone"], "system": body.system,
                                                   "by": ident.principal_id},
                      key=f"ingest:{body.name}:{clock.now().isoformat()}", priority=5)
        return {"job_id": jid}

    @app.get("/v1/recordings")
    def recordings_list(s: SessionDep, ident: IdentDep):
        from .insights import recordings as rl
        return {"recordings": rl(s, ident)}

    @app.get("/v1/recordings/{sha}")
    def recording_detail(sha: str, s: SessionDep, ident: IdentDep):
        from .insights import recording as rd
        r = rd(s, ident, sha) if is_sha256(sha) else None
        if r is None:
            raise HTTPException(404, "not found")
        return r

    @app.get("/v1/recordings/{sha}/transcript.vtt", response_class=PlainTextResponse)
    def recording_vtt(sha: str, s: SessionDep, ident: IdentDep):
        from .insights import _readable_sha, vtt
        if not is_sha256(sha) or not _readable_sha(s, ident, sha):
            raise HTTPException(404, "not found")
        return PlainTextResponse(vtt(s, sha), media_type="text/vtt")

    @app.post("/v1/recordings/{sha}/rebuild", status_code=202)
    def recording_rebuild(sha: str, s: SessionDep, ident: IdentDep):
        need(ident, "analyst", "viewer")
        from .insights import _readable_sha
        if not is_sha256(sha) or not _readable_sha(s, ident, sha) or s.get(Capsule, sha) is None:
            raise HTTPException(404, "not found")
        jid = enqueue(s, "capsule.rebuild", {"sha": sha}, key=f"rebuild:{sha}:{clock.now().isoformat()}", priority=60)
        return {"job_id": jid}

    @app.get("/v1/units/{uid}/spec")
    def unit_spec(uid: str, s: SessionDep, ident: IdentDep, refresh: bool = False):
        from ..capsule import slidedesc
        from .insights import _readable_sha
        u = s.get(Unit, uid)
        if u is None or not _readable_sha(s, ident, u.content_sha):
            raise HTTPException(404, "not found")
        s.commit()          # release the request's write lock before seconds of OCR in the sandbox
        try:
            return slidedesc.describe(uid, refresh)
        except slidedesc.NotAFrame as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/sources")
    def sources(s: SessionDep, ident: IdentDep):
        return {"sources": [{"name": x.name, "system": x.system, "site": (x.config or {}).get("site"),
                             "approved_repository": x.approved_repository,
                             "ai_use_allowed": bool((x.config or {}).get("ai_use_allowed"))}
                            for x in s.scalars(select(Source).order_by(Source.name))]}

    @app.post("/v1/sources/{name}/ai-use")
    def source_ai_use(name: str, body: AiUseBody, s: SessionDep, ident: IdentDep):
        need(ident, "dpo", "admin")
        src = s.get(Source, name)
        if src is None:
            raise HTTPException(404, "unknown source")
        src.config = {**(src.config or {}), "ai_use_allowed": body.allowed}
        audit.append(s, ident.principal_id, "source.ai_use_set", "source", name,
                     {"allowed": body.allowed, "reason": body.reason})
        return {"name": name, "ai_use_allowed": body.allowed}

    @app.get("/v1/ai/context")
    def ai_context(s: SessionDep, ident: IdentDep, q: str = Query(min_length=2, max_length=300),
                   k: int = Query(8, ge=1, le=25)):
        need(ident, "viewer", "analyst", "auditor")
        from ..ai.context import context
        return context(s, ident, q, k)

    @app.get("/v1/scenarios")
    def scenarios(s: SessionDep, ident: IdentDep):
        need(ident, "analyst", "approver", "operator", "auditor", "dpo")
        from ..reports import measured_ratios
        return {"ratios": measured_ratios(s)}

    @app.get("/v1/reports/savings")
    def report(s: SessionDep, ident: IdentDep):
        need(ident, "analyst", "auditor")
        return savings(s)

    for r in app.routes:                                         # group the API docs by area
        if isinstance(r, APIRoute) and not r.tags:
            r.tags = [_tag(r.path)]

    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    swagger_dir = ui_dir / "vendor" / "swagger"

    @app.get("/docs", include_in_schema=False)
    def api_docs():
        """Swagger UI, served from this machine (works offline; nothing is fetched from a CDN)."""
        from fastapi.openapi.docs import get_swagger_ui_html
        local = (swagger_dir / "swagger-ui-bundle.js").is_file()
        extra = {"swagger_js_url": "/docs/assets/swagger-ui-bundle.js",
                 "swagger_css_url": "/docs/assets/swagger-ui.css"} if local else {}
        return get_swagger_ui_html(openapi_url="/openapi.json", title="KORA API", swagger_favicon_url="/ui/favicon.svg",
                                   swagger_ui_parameters={"docExpansion": "none", "displayRequestDuration": True,
                                                          "tryItOutEnabled": True, "filter": True},
                                   **extra)

    @app.get("/docs/assets/{name}", include_in_schema=False)
    def api_docs_asset(name: str):
        from fastapi.responses import FileResponse
        kinds = {"swagger-ui-bundle.js": "text/javascript; charset=utf-8", "swagger-ui.css": "text/css; charset=utf-8"}
        if name not in kinds or not (swagger_dir / name).is_file():
            raise HTTPException(404, "not found")
        return FileResponse(swagger_dir / name, media_type=kinds[name])

    if ui_dir.is_dir():
        from fastapi.responses import FileResponse, RedirectResponse
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/ui/")

        @app.get("/ui/", include_in_schema=False)
        @app.get("/ui/{name}", include_in_schema=False)
        def ui(name: str = "index.html"):
            if not re.fullmatch(r"[a-z0-9\-]+\.(html|js|css|svg)", name) or not (ui_dir / name).is_file():
                raise HTTPException(404, "not found")
            return FileResponse(ui_dir / name, media_type=types[Path(name).suffix])

    return app


def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("kora.api.app:create_app", factory=True, host="0.0.0.0", port=8080, proxy_headers=True)
