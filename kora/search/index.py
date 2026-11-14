"""Evidence search: hybrid lexical (BM25 over a relational inverted index) +
semantic rerank, permission-filtered, version-aware, de-duplicated.

Every hit carries its evidence: the exact passage, speaker and timestamp, the
frame on screen and the slide it shows (document number, revision, page),
and the locations of the source the caller is allowed to open. Identical
slides across copies and revisions collapse into one result that points at
the head revision and says whether the slide changed there.

The inverted index lives in the primary DB so it is portable (SQLite/Postgres)
and transactional with ingestion; production swaps in OpenSearch/pgvector
behind the same function signatures.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..fingerprint import embed as emb
from ..fingerprint.text import tokens
from ..knowledge import notes
from ..models import Capsule, Content, File, Posting, Unit, Version
from ..security.acl import can_read
from ..security.auth import Identity

K1, B = 1.2, 0.75
INDEXED_KINDS = ("page", "slide", "segment", "frame", "message", "note")


def index_content(s: Session, sha: str) -> int:
    units = list(s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind.in_(INDEXED_KINDS))))
    ids = [u.id for u in units]
    if ids:
        s.execute(delete(Posting).where(Posting.unit_id.in_(ids)))
    n = 0
    for u in units:
        toks = tokens(u.text or "")
        u.token_count = len(toks)
        if not toks:
            continue
        u.embedding = emb.to_bytes(emb.embed(u.text))
        for term, tf in Counter(toks).items():
            s.add(Posting(term=term, unit_id=u.id, tf=tf))
        n += 1
    s.flush()
    return n


def _readable(s: Session, ident: Identity, sha: str, cache: dict) -> tuple[bool, list[File]]:
    if sha in cache:
        return cache[sha]
    files = list(s.scalars(select(File).where(File.sha256 == sha)))
    toks = ident.acl_tokens
    ok_files = [f for f in files if can_read(toks, f.acl)]
    if not ok_files:
        cap = s.get(Capsule, sha)            # originals gone: the capsule's ACL snapshot still governs
        allowed = bool(cap and can_read(toks, cap.manifest.get("acl_snapshot", [])))
    else:
        allowed = True
    cache[sha] = (allowed, ok_files)
    return cache[sha]


def search(s: Session, ident: Identity, q: str, limit: int = 10, kinds: tuple[str, ...] | None = None) -> dict:
    qt = list(dict.fromkeys(tokens(q)))
    if not qt:
        return {"query": q, "results": []}
    n_docs = s.scalar(select(func.count()).select_from(Unit).where(Unit.token_count > 0)) or 1
    avgdl = float(s.scalar(select(func.avg(Unit.token_count)).where(Unit.token_count > 0)) or 1.0)
    df = dict(s.execute(select(Posting.term, func.count()).where(Posting.term.in_(qt)).group_by(Posting.term)).all())
    rows = s.execute(select(Posting.unit_id, Posting.term, Posting.tf, Unit.token_count)
                     .join(Unit, Unit.id == Posting.unit_id).where(Posting.term.in_(qt))).all()
    scores: dict[str, float] = defaultdict(float)
    for uid, term, tf, dl in rows:
        idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        scores[uid] += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * (dl or 1) / avgdl))
    if not scores:
        return {"query": q, "results": []}
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:300]
    units = {u.id: u for u in s.scalars(select(Unit).where(Unit.id.in_([t[0] for t in top])))}
    qv = emb.embed(q)
    mx = top[0][1]
    ranked = []
    for uid, sc in top:
        u = units[uid]
        if kinds and u.kind not in kinds:
            continue
        cos = float(np.dot(emb.from_bytes(u.embedding), qv)) if u.embedding else 0.0
        ranked.append((0.75 * sc / mx + 0.25 * max(0.0, cos), u))
    ranked.sort(key=lambda t: -t[0])

    cache: dict = {}
    seen_bodies: dict[str, dict] = {}
    results = []
    for score, u in ranked:
        allowed, files = _readable(s, ident, u.content_sha, cache)
        if not allowed:
            continue
        if u.kind in ("page", "slide") and u.body_hash:
            if u.body_hash in seen_bodies:
                seen_bodies[u.body_hash]["also_in"].append(_doc_ref(s, u))
                continue
        hit = _evidence(s, u, files, round(score, 4))
        if u.kind in ("page", "slide") and u.body_hash:
            hit["also_in"] = []
            seen_bodies[u.body_hash] = hit
        results.append(hit)
        if len(results) >= limit:
            break
    return {"query": q, "results": results}


def _doc_ref(s: Session, u: Unit) -> dict:
    c = s.get(Content, u.content_sha)
    v = s.scalar(select(Version).where(Version.content_sha == u.content_sha))
    return {"content": u.content_sha, "page": u.ordinal, "doc_number": (c.meta or {}).get("doc_number"),
            "revision": v.revision_label if v else None, "is_head": bool(v and v.is_head)}


def _locations(files: list[File]) -> list[dict]:
    return [{"system": f.system, "site": f.site, "path": f.path, "status": f.status} for f in files]


def _version_note(s: Session, u: Unit) -> dict | None:
    v = s.scalar(select(Version).where(Version.content_sha == u.content_sha))
    if v is None:
        return None
    note = {"revision": v.revision_label, "is_head": v.is_head, "lineage": v.lineage_id}
    if not v.is_head:
        head = s.scalar(select(Version).where(Version.lineage_id == v.lineage_id, Version.is_head.is_(True)))
        if head:
            same = s.scalar(select(Unit).where(Unit.content_sha == head.content_sha, Unit.body_hash == u.body_hash))
            note["latest_revision"] = head.revision_label
            note["in_latest"] = (f"unchanged in {head.revision_label} (page {same.ordinal})" if same
                                 else f"changed or removed in {head.revision_label}")
    return note


def _evidence(s: Session, u: Unit, files: list[File], score: float) -> dict:
    c = s.get(Content, u.content_sha)
    active = [f for f in files if f.status == "active"]
    base = {"score": score, "content": u.content_sha, "kind": u.kind, "text": (u.text or "")[:600],
            "locations": _locations(active)}
    if not active:
        cap = s.get(Capsule, u.content_sha)
        base["original"] = ("removed from source; knowledge capsule available" if cap else "not available")
    if u.kind == "segment":
        rec = active[0].filename if active else (files[0].filename if files else u.content_sha[:12])
        src = (c.media or {}).get("transcript_source") or ""
        review = ((c.meta or {}).get("transcript_review") or {}).get("decision")
        base.update({"type": "transcript", "recording": rec, "speaker": u.speaker,
                     "transcript_quality": ("platform transcript" if src.startswith("sidecar") else
                                            f"machine transcript ({src}), {'human-approved' if review == 'approve' else 'not reviewed'}"),
                     "start_s": u.start_s, "end_s": u.end_s, "frame": (u.meta or {}).get("frame_ordinal"),
                     "slide": (u.meta or {}).get("slide")})
        sl = base["slide"]
        if sl:
            base["slide"] = {**sl, **_doc_ref(s, s.scalar(select(Unit).where(Unit.content_sha == sl["content"],
                                                                                 Unit.ordinal == sl["ordinal"],
                                                                                 Unit.kind.in_(("page", "slide")))))}
    elif u.kind == "frame":
        base.update({"type": "frame", "t": u.start_s, "thumb": u.blob_key, "slide": (u.meta or {}).get("slide")})
    elif u.kind == "message":
        base.update({"type": "message", "document": files[0].filename if files else None, "speaker": u.speaker,
                     "time": (u.meta or {}).get("time")})
    elif u.kind == "note":
        bb = (u.meta or {}).get("best_before")
        base.update({"type": "speaker_notes", "document": files[0].filename if files else None, "page": u.ordinal,
                     "doc_number": (c.meta or {}).get("doc_number"), "version": _version_note(s, u),
                     "best_before": bb, "past_best_before": notes.is_past(bb)})
    else:
        base.update({"type": "slide", "document": files[0].filename if files else None,
                     "page": u.ordinal, "thumb": u.thumb_key, "doc_number": (c.meta or {}).get("doc_number"),
                     "version": _version_note(s, u)})
        bb = (u.meta or {}).get("best_before")
        if bb:
            base.update({"best_before": bb, "past_best_before": notes.is_past(bb)})
    return base
