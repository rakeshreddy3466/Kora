"""Link recording keyframes to the slides they show (single source of truth).

1. candidates: slides whose perceptual hash is within MAX_DIST of the frame
2. ranking: OCR text of the frame vs slide text (token Jaccard) decides
   between near-identical template slides and between revisions (PA2 vs A
   differ by one word); pHash distance breaks remaining ties
3. "presented document": the document with the most linked frames wins;
   every frame is then re-pointed to *that* document's slide where the slide
   body is identical, so links are consistent (one deck, one revision)
4. transcript segments get the frame visible when they start, which makes
   "as you see here" resolvable to a concrete slide
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..dedup import classes as C
from ..dedup.engine import upsert
from ..fingerprint.phash import hamming_many, to_array
from ..fingerprint.text import tokens
from ..models import Content, FrameLink, Unit, Version

MAX_DIST = 16
MIN_OCR_AGREEMENT = 0.25
OCR_ONLY_MIN_TOKENS = 6
OCR_ONLY_MIN_JACCARD = 0.45


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def link(s: Session, video: Content) -> dict:
    frames = list(s.scalars(select(Unit).where(Unit.content_sha == video.sha256, Unit.kind == "frame")
                            .order_by(Unit.ordinal)))
    slides = list(s.scalars(select(Unit).where(Unit.kind.in_(("page", "slide", "image")), Unit.phash.is_not(None))))
    s.execute(delete(FrameLink).where(FrameLink.frame_unit_id.in_([f.id for f in frames])))
    if not frames or not slides:
        return {"frames": len(frames), "linked": 0}
    arr = to_array([u.phash for u in slides])
    slide_tokens: dict[str, set] = {}
    tops: dict[str, list[tuple[Unit, int]]] = {}         # frame -> equally good candidate slides
    for fr in frames:
        if not fr.phash:
            continue
        d = hamming_many(fr.phash, arr)
        idx = np.nonzero(d <= MAX_DIST)[0]
        ftoks = set(tokens(fr.text or ""))
        if idx.size == 0:
            # picture-in-picture / cropped share: no visual match, fall back to the words on screen
            if len(ftoks) < OCR_ONLY_MIN_TOKENS:
                continue
            for i, u in enumerate(slides):
                if u.id not in slide_tokens:
                    slide_tokens[u.id] = set(tokens(u.text or ""))
            scored = [(_jac(ftoks, slide_tokens[u.id]), u) for u in slides]
            best = max((x[0] for x in scored), default=0.0)
            if best < OCR_ONLY_MIN_JACCARD:
                continue
            tops[fr.id] = [(u, 64) for j, u in scored if j >= best - 0.02]
            continue
        scored = []
        for i in idx:
            u = slides[int(i)]
            if u.id not in slide_tokens:
                slide_tokens[u.id] = set(tokens(u.text or ""))
            ocr = _jac(ftoks, slide_tokens[u.id]) if ftoks else 0.0
            scored.append((ocr, int(d[i]), u))
        best_ocr = max(x[0] for x in scored)
        best_d = min(x[1] for x in scored if x[0] >= best_ocr - 0.02)
        cand = [(u, dist) for ocr, dist, u in scored if ocr >= best_ocr - 0.02 and dist <= best_d + 2]
        u0, d0 = min(cand, key=lambda t: t[1])
        if len(ftoks) >= 3 and slide_tokens.get(u0.id):
            if best_ocr < MIN_OCR_AGREEMENT:
                continue                                 # looks alike, says something else
        elif d0 > 8:
            continue                                     # no readable text: demand a close visual match
        tops[fr.id] = cand

    # presented document: the one that contains a best match for the most frames
    votes = Counter()
    dist_sum: Counter = Counter()
    for cand in tops.values():
        for sha in {u.content_sha for u, _ in cand}:
            votes[sha] += 1
            dist_sum[sha] += min(dd for u, dd in cand if u.content_sha == sha)
    if not votes:
        return {"frames": len(frames), "linked": 0}
    heads = {v.content_sha for v in s.scalars(select(Version).where(Version.is_head.is_(True)))}
    variants = set(s.scalars(select(Version.content_sha).where(Version.variant_of_id.is_not(None))))
    top = max(votes, key=lambda sha: (votes[sha], -dist_sum[sha], sha not in variants, sha in heads, sha))
    by_body = {u.body_hash: u for u in s.scalars(select(Unit).where(Unit.content_sha == top,
                                                                   Unit.kind.in_(("page", "slide", "image"))))}
    chosen: dict[str, tuple[Unit, int, str]] = {}
    for fid, cand in tops.items():
        mine = [(u, dd) for u, dd in cand if u.content_sha == top]
        u, dd = min(mine or cand, key=lambda t: t[1])
        if u.content_sha != top and u.body_hash in by_body:
            u = by_body[u.body_hash]
        fr = next(f for f in frames if f.id == fid)
        chosen[fid] = (u, dd, "ocr" if dd >= 64 else "ocr+phash" if len(set(tokens(fr.text or ""))) >= 3 else "phash")
    linked = 0
    for fr in frames:
        if fr.id not in chosen:
            fr.meta = {**(fr.meta or {}), "slide": None}
            continue
        u, dist, method = chosen[fr.id]
        s.add(FrameLink(frame_unit_id=fr.id, slide_unit_hash=u.unit_hash, slide_unit_id=u.id, method=method,
                        distance=float(dist)))
        fr.meta = {**(fr.meta or {}), "slide": {"content": u.content_sha, "ordinal": u.ordinal,
                                                 "unit_hash": u.unit_hash, "title": (u.text or "").split("\n")[0][:120]}}
        linked += 1

    # segments -> frame visible at segment start
    segs = list(s.scalars(select(Unit).where(Unit.content_sha == video.sha256, Unit.kind == "segment")))
    starts = [(f.start_s or 0.0, f) for f in frames]
    for seg in segs:
        vis = None
        for t, f in starts:
            if t <= (seg.start_s or 0) + 0.5:
                vis = f
        if vis is not None:
            seg.meta = {**(seg.meta or {}), "frame_ordinal": vis.ordinal, "slide": (vis.meta or {}).get("slide")}

    doc = s.get(Content, top)
    ver = s.scalar(select(Version).where(Version.content_sha == top))
    upsert(s, C.REL_PRESENTED, top, video.sha256, round(votes[top] / max(1, len(frames)), 3),
           {"frames_linked": linked, "frames": len(frames),
            "doc_number": (doc.meta or {}).get("doc_number"),
            "revision": ver.revision_label if ver else (doc.meta or {}).get("revision"),
            "slides_shown": sorted({c[0].ordinal for c in chosen.values() if c[0].content_sha == top})})
    s.flush()
    return {"frames": len(frames), "linked": linked, "presented": top}
