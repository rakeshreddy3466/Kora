"""Relationship engine: builds the content relationship graph incrementally.

Each new content is compared only against *candidates* produced by indexes,
never against the whole corpus:
  normalized_hash index      -> CONTAINER_VARIANT
  body_hash index (per unit) -> CONTAINED_SUBSET / PARTIAL_OVERLAP
  MinHash LSH bands          -> NEAR_DUPLICATE
  duration range + signature -> MEDIA_VARIANT
  embedding scan (ANN in prod)-> KNOWLEDGE_OVERLAP
Exact copies need no edges: files sharing a SHA-256 *are* the group.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..fingerprint import embed as emb
from ..fingerprint import minhash as mh
from ..fingerprint.phash import hamming, hamming_many, to_array
from ..governance.policy import get_policy
from ..models import Content, LshBand, Relation, Unit, Version
from ..queries import DOC_KINDS, doc_units
from . import classes as C

DOC_TYPES = ("pdf", "pptx")


def twins(s: Session, a: str, b: str) -> bool:
    """A compressed rendition and its original are one document by construction, not duplicates."""
    ca, cb = s.get(Content, a), s.get(Content, b)
    return bool(ca and cb and ((ca.meta or {}).get("rendition_of") == b or (cb.meta or {}).get("rendition_of") == a))


def upsert(s: Session, kind: str, a: str, b: str, score: float, details: dict) -> Relation | None:
    if twins(s, a, b):
        return None
    if kind in C.SYMMETRIC and a > b:
        a, b = b, a
    if kind != C.REL_KNOWLEDGE:
        # a more specific relation supersedes a topical one
        for weak in s.scalars(select(Relation).where(Relation.kind == C.REL_KNOWLEDGE, or_(
                and_(Relation.a_sha == a, Relation.b_sha == b), and_(Relation.a_sha == b, Relation.b_sha == a)))):
            s.delete(weak)
    rel = s.scalar(select(Relation).where(Relation.kind == kind, Relation.a_sha == a, Relation.b_sha == b))
    if rel is None:
        rel = Relation(kind=kind, a_sha=a, b_sha=b, score=score, details=details)
        s.add(rel)
    else:
        rel.score, rel.details = score, details
    s.flush()
    return rel


def related(s: Session, a: str, b: str, kinds: set[str]) -> bool:
    return s.scalar(select(Relation.id).where(Relation.kind.in_(kinds), or_(
        and_(Relation.a_sha == a, Relation.b_sha == b), and_(Relation.a_sha == b, Relation.b_sha == a)))) is not None


def lineage_of(s: Session, sha: str) -> str | None:
    return s.scalar(select(Version.lineage_id).where(Version.content_sha == sha))


def index_lsh(s: Session, c: Content) -> None:
    sig = mh.from_bytes(c.minhash)
    if sig is None or c.text_chars < 40:
        return
    for band, bucket in mh.bands(sig):
        if s.get(LshBand, (band, bucket, c.sha256)) is None:
            s.add(LshBand(band=band, bucket=bucket, content_sha=c.sha256))
    s.flush()


def relate(s: Session, c: Content) -> list[str]:
    """Compute relations for one content. Returns ids of videos whose frame
    links should be recomputed because this document may be shown in them."""
    pol = get_policy()
    index_lsh(s, c)              # normally done at extraction; idempotent
    relink: list[str] = []
    if c.detected_type in DOC_TYPES:
        _doc_relations(s, c, pol)
        relink = _videos_showing(s, c)
    elif c.detected_type == "video":
        _media_relations(s, c, pol)
    _near_duplicates(s, c, pol)
    _knowledge(s, c, pol)
    return relink


def _drop_intra_lineage(s: Session, sha: str, lin: str) -> None:
    """Edges recorded before both contents joined the same lineage are revisions, not overlaps."""
    members = set(s.scalars(select(Version.content_sha).where(Version.lineage_id == lin)))
    for r in s.scalars(select(Relation).where(
            Relation.kind.in_((C.REL_PARTIAL, C.REL_SUBSET, C.REL_NEAR, C.REL_KNOWLEDGE)),
            or_(Relation.a_sha == sha, Relation.b_sha == sha))):
        other = r.b_sha if r.a_sha == sha else r.a_sha
        if other in members:
            s.delete(r)
    s.flush()


def _doc_relations(s: Session, c: Content, pol) -> None:
    units = doc_units(s, c.sha256)
    if not units:
        return
    my_bodies = {u.body_hash for u in units if u.body_hash}
    my_units = {u.unit_hash for u in units}
    lin = lineage_of(s, c.sha256)
    if lin:
        _drop_intra_lineage(s, c.sha256, lin)

    if c.normalized_hash:
        for other in s.scalars(select(Content).where(Content.normalized_hash == c.normalized_hash,
                                                     Content.sha256 != c.sha256)):
            upsert(s, C.REL_CONTAINER, c.sha256, other.sha256, 1.0,
                   {"render_identical": True, "units": len(units),
                    "size_bytes": {c.sha256: c.size_bytes, other.sha256: other.size_bytes}})

    rows = s.execute(select(Unit.content_sha, Unit.body_hash, Unit.unit_hash).where(
        Unit.body_hash.in_(my_bodies), Unit.content_sha != c.sha256, Unit.kind.in_(DOC_KINDS))).all()
    shared: dict[str, set[str]] = defaultdict(set)
    exact: dict[str, set[str]] = defaultdict(set)
    for sha, body, uh in rows:
        shared[sha].add(body)
        if uh in my_units:
            exact[sha].add(uh)
    min_ratio = pol["partial_overlap"]["min_shared_ratio"]
    for other_sha, bodies in shared.items():
        other = s.get(Content, other_sha)
        if other is None or other.detected_type not in DOC_TYPES:
            continue
        if c.normalized_hash and other.normalized_hash == c.normalized_hash:
            continue
        if lin and lineage_of(s, other_sha) == lin:
            continue                                   # revisions: REVISION_OF carries the diff
        other_bodies = {u.body_hash for u in doc_units(s, other_sha) if u.body_hash}
        inter = len(bodies)
        c_in_o, o_in_c = inter / len(my_bodies), inter / max(1, len(other_bodies))
        details = {"shared_slides": inter, "exact_shared_slides": len(exact[other_sha]),
                   "slides": {c.sha256: len(my_bodies), other_sha: len(other_bodies)}}
        if c_in_o == 1.0 and len(my_bodies) < len(other_bodies):
            upsert(s, C.REL_SUBSET, c.sha256, other_sha, round(o_in_c, 3),
                   {**details, "exact": len(exact[other_sha]) == len(my_units)})
        elif o_in_c == 1.0 and len(other_bodies) < len(my_bodies):
            upsert(s, C.REL_SUBSET, other_sha, c.sha256, round(c_in_o, 3),
                   {**details, "exact": len(exact[other_sha]) == len(other_bodies)})
        elif max(c_in_o, o_in_c) >= min_ratio:
            details["unique_slides"] = {c.sha256: len(my_bodies) - inter, other_sha: len(other_bodies) - inter}
            upsert(s, C.REL_PARTIAL, c.sha256, other_sha, round(inter / len(my_bodies | other_bodies), 3), details)


def _near_duplicates(s: Session, c: Content, pol) -> None:
    sig = mh.from_bytes(c.minhash)
    if sig is None or c.text_chars < 40:
        return
    conds = [and_(LshBand.band == b, LshBand.bucket == k) for b, k in mh.bands(sig)]
    cands = set(s.scalars(select(LshBand.content_sha).where(or_(*conds), LshBand.content_sha != c.sha256)))
    thr = pol["near_duplicate"]["min_jaccard"]
    lin = lineage_of(s, c.sha256)
    for other_sha in cands:
        other = s.get(Content, other_sha)
        j = mh.jaccard(sig, mh.from_bytes(other.minhash))
        if j < thr:
            continue
        if related(s, c.sha256, other_sha, C.STRONG):
            continue
        if lin and lineage_of(s, other_sha) == lin:
            continue
        if (c.detected_type == "video") != (other.detected_type == "video"):
            continue
        upsert(s, C.REL_NEAR, c.sha256, other_sha, round(j, 3),
               {"jaccard": round(j, 3), "method": "minhash-lsh", "note": "similar wording, no shared slides"})


def _media_relations(s: Session, c: Content, pol) -> None:
    sig = (c.media or {}).get("temporal_signature") or []
    dur = c.duration_s or 0
    if not sig or dur <= 0:
        return
    mp = pol["media_variant"]
    lo, hi = dur * (1 - mp["max_duration_delta_ratio"]), dur * (1 + mp["max_duration_delta_ratio"])
    for other in s.scalars(select(Content).where(Content.detected_type == "video", Content.sha256 != c.sha256,
                                                 Content.duration_s >= lo, Content.duration_s <= hi)):
        osig = (other.media or {}).get("temporal_signature") or []
        n = min(len(sig), len(osig))
        if n < 3:
            continue
        mean = float(np.mean([hamming(a, b) for a, b in zip(sig[:n], osig[:n])]))
        if mean <= mp["max_mean_phash_distance"]:
            q = lambda x: {"width": x.media.get("width"), "height": x.media.get("height"),  # noqa: E731
                           "bit_rate": x.media.get("bit_rate"), "size_bytes": x.size_bytes}
            upsert(s, C.REL_MEDIA, c.sha256, other.sha256, round(1 - mean / 64, 3),
                   {"mean_phash_distance": round(mean, 2), "duration_s": {c.sha256: dur, other.sha256: other.duration_s},
                    "quality": {c.sha256: q(c), other.sha256: q(other)}})


def _knowledge(s: Session, c: Content, pol) -> None:
    v = emb.from_bytes(c.embedding)
    if v is None or c.text_chars < 200:
        return
    thr = pol["knowledge_overlap"]["min_cosine"]
    rows = s.execute(select(Content.sha256, Content.embedding).where(
        Content.sha256 != c.sha256, Content.embedding.is_not(None), Content.text_chars >= 200)).all()
    if not rows:
        return
    mat = np.stack([emb.from_bytes(r[1]) for r in rows])
    sims = mat @ v
    lin = lineage_of(s, c.sha256)
    for (sha, _), sim in zip(rows, sims):
        if sim < thr:
            continue
        if related(s, c.sha256, sha, C.STRONG | {C.REL_NEAR, C.REL_MEDIA, C.REL_PRESENTED}):
            continue
        if lin and lineage_of(s, sha) == lin:
            continue
        upsert(s, C.REL_KNOWLEDGE, c.sha256, sha, round(float(sim), 3), {"cosine": round(float(sim), 3)})


def _videos_showing(s: Session, c: Content, max_dist: int = 12) -> list[str]:
    pages = [u.phash for u in doc_units(s, c.sha256) if u.phash]
    if not pages:
        return []
    frames = s.execute(select(Unit.content_sha, Unit.phash).where(Unit.kind == "frame", Unit.phash.is_not(None))).all()
    if not frames:
        return []
    arr = to_array([f[1] for f in frames])
    hit = set()
    for ph in pages:
        d = hamming_many(ph, arr)
        for i in np.nonzero(d <= max_dist)[0]:
            hit.add(frames[int(i)][0])
    return sorted(hit)
