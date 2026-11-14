"""Version lineage: group contents into document families and order them.

Grouping evidence (strongest first)
  1. document number (repository field, document properties, page text)
  2. normalised file-name stem  AND  content similarity with an existing
     version (shared slide bodies or MinHash) - a name alone never suffices,
     because names like "presentation.pdf" are not identities
Content similarity alone never creates a lineage: decks that share slides
are PARTIAL_OVERLAP, not revisions of each other.

Ordering (with recorded confidence)
  high    document-control revisions (PA1 < PA2 < A < PB1 < B)
  medium  file-name tokens agreeing with modification times
  low     modification times only, or tokens contradicting times
Policies require >= medium before superseded versions may be compacted.

Special cases
  * same rendered content, different bytes (re-saved/linearised) -> variant_of,
    not a separate version, not a conflict
  * two different contents claiming the same revision -> conflict (both
    flagged, lineage blocked from compaction until a human resolves it)
  * content with a document number but no revision that is a strict subset
    of a version (an excerpt) -> not a version; CONTAINED_SUBSET relation
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit, clock
from ..fingerprint import minhash as mh
from ..models import Content, File, Lineage, Relation, Version
from ..queries import content_time, doc_units, files_of, unit_views
from . import naming, revision
from .diff import diff

DOC_TYPES = ("pdf", "pptx")
SIMILAR_UNIT_OVERLAP = 0.3
SIMILAR_MINHASH = 0.45
HIGH_TRUST = ("repository", "doc_metadata", "doc_text")


def _lock_or_create(s: Session, key: str, **fields) -> Lineage:
    q = select(Lineage).where(Lineage.key == key).with_for_update()
    lin = s.scalars(q).first()
    if lin:
        return lin
    try:
        with s.begin_nested():
            lin = Lineage(key=key, **fields)
            s.add(lin)
            s.flush()
        return lin
    except IntegrityError:
        return s.scalars(q).first()


def _overlap(s: Session, a_sha: str, b_sha: str) -> tuple[float, float]:
    """(body overlap / min(len), minhash jaccard)"""
    a = {u.body_hash for u in doc_units(s, a_sha)}
    b = {u.body_hash for u in doc_units(s, b_sha)}
    ov = len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0
    ca, cb = s.get(Content, a_sha), s.get(Content, b_sha)
    return ov, mh.jaccard(mh.from_bytes(ca.minhash), mh.from_bytes(cb.minhash))


def _similar_to(s: Session, lin: Lineage, sha: str) -> float:
    best = 0.0
    for v in lin.versions:
        ov, j = _overlap(s, sha, v.content_sha)
        if ov >= SIMILAR_UNIT_OVERLAP or j >= SIMILAR_MINHASH:
            best = max(best, ov, j)
    return best


def _is_excerpt(s: Session, lin: Lineage, sha: str) -> str | None:
    mine = [u.unit_hash for u in doc_units(s, sha)]
    for v in lin.versions:
        theirs = {u.unit_hash for u in doc_units(s, v.content_sha)}
        if mine and set(mine) <= theirs and len(theirs) > len(set(mine)):
            return v.content_sha
    return None


def _norm_title(t: str | None) -> str:
    return " ".join(str(t or "").lower().split())


def _property_title(s: Session, c: Content) -> str | None:
    """The title the author set in the file's properties; None when KORA took it from the first page."""
    t = _norm_title((c.meta or {}).get("title"))
    if not t or (c.meta or {}).get("title_source") == "first_page" or t in ("powerpoint presentation", "presentation"):
        return None
    first = doc_units(s, c.sha256)[:1]
    if first and _norm_title((first[0].text or "").strip().split("\n")[0]) == t:
        return None                                      # older data: derived from the first page
    return t


def _titles_differ(s: Session, lin: Lineage, mine: str | None) -> bool:
    if not mine:
        return False
    for v in s.scalars(select(Version).where(Version.lineage_id == lin.id)):
        theirs = _property_title(s, s.get(Content, v.content_sha))
        if theirs and theirs != mine:
            return True
    return False


def assign(s: Session, content: Content) -> Version | None:
    if content.detected_type not in DOC_TYPES or not content.extracted:
        return None
    existing = s.scalar(select(Version).where(Version.content_sha == content.sha256))
    if existing:
        return existing
    files = files_of(s, content.sha256, None)
    infos = [naming.parse(f.filename) for f in files]
    meta = content.meta or {}
    docno = meta.get("doc_number")
    rev = revision.parse(meta.get("revision"))
    rev_src = meta.get("revision_source") if rev else None
    evidence: dict = {}

    if docno:
        lin = _lock_or_create(s, f"docno:{docno}", doc_number=docno, title=meta.get("title"),
                              evidence={"grouped_by": "document_number"})
        if rev is None:
            parent = _is_excerpt(s, lin, content.sha256)
            if parent:
                return None           # an excerpt is not a revision; dedup records CONTAINED_SUBSET
            # fall back to file-name tokens, then time
            toks = max((i.tokens for i in infos), key=len, default=[])
            rev = revision.from_filename_tokens(toks) if toks else None
            rev_src = "filename" if rev else "mtime"
    else:
        lin = None
        stems = sorted({i.stem_key for i in infos})
        for stem in stems:
            cands = list(s.scalars(select(Lineage).where(
                (Lineage.key == f"name:{stem}") | Lineage.key.like(f"name:{stem}#%")).with_for_update()))
            mine = _property_title(s, content)
            # same name + shared slides is not enough when the files' own titles say they differ
            # (a deck tailored per customer: "Offer for Operator X" vs "Offer for Operator Y")
            cands = [c for c in cands if not _titles_differ(s, c, mine)]
            scored = sorted(((_similar_to(s, c, content.sha256), c) for c in cands), key=lambda t: -t[0])
            if scored and scored[0][0] > 0:
                lin = scored[0][1]
                evidence = {"joined_by": "name+content", "similarity": round(scored[0][0], 3)}
                break
        if lin is None:
            stem = stems[0] if stems else content.sha256[:12]
            n = s.scalar(select(Lineage.id).where(Lineage.key == f"name:{stem}"))
            key = f"name:{stem}" if n is None else f"name:{stem}#{content.sha256[:8]}"
            lin = _lock_or_create(s, key, title=meta.get("title") or stem,
                                  evidence={"grouped_by": "file_name", "stem": stem})
        best = sorted(infos, key=lambda i: (i.copy_marker > 0, -len(i.tokens)))
        toks = best[0].tokens if best else []
        rev = revision.from_filename_tokens(toks)
        rev_src = "filename"

    v = Version(lineage_id=lin.id, content_sha=content.sha256,
                revision_label=rev.label if rev else None, revision_scheme=rev.scheme if rev else None,
                revision_key=list(rev.key) if rev else [], released=rev.released if rev else None,
                revision_source=rev_src)
    s.add(v)
    s.flush()
    if evidence:
        lin.evidence = {**(lin.evidence or {}), **evidence}
    reorder(s, lin)
    return v


def split(s: Session, lin: Lineage, by: str) -> dict:
    """A person said these files are different documents (variants that share slides): every
    version leaves the family, each gets its own, and the slide overlap is recorded instead."""
    from ..dedup import engine as dedup
    moved = []
    versions = sorted(s.scalars(select(Version).where(Version.lineage_id == lin.id)), key=lambda v: v.ordinal or 0)
    for v in versions[1:]:
        own = _lock_or_create(s, f"{lin.key}#{v.content_sha[:8]}", title=(s.get(Content, v.content_sha).meta or {}).get("title"),
                              evidence={"grouped_by": "person", "split_from": lin.id, "by": by})
        v.lineage_id, v.parent_version_id, v.variant_of_id, v.conflict = own.id, None, None, False
        moved.append(v.content_sha)
        s.flush()
        reorder(s, own)
    lin.evidence = {**(lin.evidence or {}), "split": {"by": by, "moved": moved}}
    s.flush()
    reorder(s, lin)
    for sha in [versions[0].content_sha, *moved]:
        dedup.relate(s, s.get(Content, sha))           # now PARTIAL_OVERLAP / CONTAINED_SUBSET
    audit.append(s, by, "lineage.split", "lineage", lin.id, {"moved": len(moved)})
    return {"split_into": 1 + len(moved)}


def reorder(s: Session, lin: Lineage) -> None:
    s.refresh(lin)
    versions = list(s.scalars(select(Version).where(Version.lineage_id == lin.id)))
    if lin.doc_number:
        # processing order is not guaranteed: an excerpt may have arrived before its parent
        for v in list(versions):
            if v.revision_scheme in ("ericsson", "rstate", "numeric"):
                continue
            others = [o for o in versions if o.id != v.id]
            mine = {u.unit_hash for u in doc_units(s, v.content_sha)}
            for o in others:
                theirs = {u.unit_hash for u in doc_units(s, o.content_sha)}
                if mine and mine < theirs:
                    audit.append(s, "system", "version.reclassified_as_excerpt", "lineage", lin.id,
                                 {"content": v.content_sha, "parent": o.content_sha})
                    s.delete(v)
                    versions.remove(v)
                    break
        s.flush()
    if not versions:
        return
    contents = {v.content_sha: s.get(Content, v.content_sha) for v in versions}
    times = {v.content_sha: content_time(s, v.content_sha) for v in versions}
    old_head = next((v.id for v in versions if v.is_head), None)

    # 1. collapse render-identical variants. A compressed rendition belongs with its original
    #    (lossy images change the render hash, not the revision), and the copy that still lives
    #    at a source represents the group, so the head always points at something readable.
    def group_key(v: Version) -> str:
        c = contents[v.content_sha]
        orig = (c.meta or {}).get("rendition_of")
        base = contents.get(orig) if orig else None
        if base is not None:
            return base.normalized_hash or orig
        return c.normalized_hash or v.content_sha
    live = {sha for sha in contents if s.scalar(select(File.id).where(File.sha256 == sha, File.status == "active").limit(1))}
    groups: dict[str, list[Version]] = {}
    for v in versions:
        groups.setdefault(group_key(v), []).append(v)
    reps = []
    for g in groups.values():
        g.sort(key=lambda v: (v.content_sha not in live, times[v.content_sha] or clock.now(), v.content_sha))
        reps.append(g[0])
        for other in g[1:]:
            other.variant_of_id = g[0].id
            other.is_head = False
            other.conflict = False
        g[0].variant_of_id = None

    # 2. choose ordering basis
    schemes = {v.revision_scheme for v in reps}
    docctl = schemes <= {"ericsson", "rstate", "numeric"} and len(schemes) == 1 and all(v.revision_key for v in reps)
    if docctl:
        basis = "revision"
        conf = "high" if all(v.revision_source in HIGH_TRUST for v in reps) else "medium"
        if not lin.doc_number and any(v.revision_source == "filename" for v in reps):
            conf = "low"         # "Offer_X" / "Offer_Y": a letter in a name may be a revision or an audience
        keyf = lambda v: (tuple(v.revision_key), times[v.content_sha] or clock.now())  # noqa: E731
    elif schemes == {"filename"}:
        basis = "filename"
        keyf = lambda v: (tuple(v.revision_key), times[v.content_sha] or clock.now())  # noqa: E731
        by_name = sorted(reps, key=keyf)
        by_time = sorted(reps, key=lambda v: (times[v.content_sha] or clock.now()))
        distinct_tokens = len({tuple(v.revision_key) for v in reps}) == len(reps)
        conf = "medium" if (by_name == by_time and distinct_tokens) else "low"
    else:
        basis, conf = "mtime", "low"
        keyf = lambda v: (times[v.content_sha] or clock.now(), v.content_sha)  # noqa: E731
    reps.sort(key=keyf)

    # 3. conflicts: different content claiming the same revision
    conflict = False
    if basis in ("revision", "filename"):
        seen: dict[tuple, Version] = {}
        for v in reps:
            v.conflict = False
        for v in reps:
            k = tuple(v.revision_key)
            if k in seen:
                v.conflict = seen[k].conflict = True
                conflict = True
            else:
                seen[k] = v

    # 4. ordinals, parents, head, diffs, REVISION_OF edges
    prev = None
    for i, v in enumerate(reps, start=1):
        v.ordinal = i
        v.is_head = i == len(reps)
        v.parent_version_id = prev.id if prev else None
        if prev is not None:
            d = diff(unit_views(s, prev.content_sha), unit_views(s, v.content_sha))
            v.diff_from_parent = d
            rel = s.scalar(select(Relation).where(Relation.kind == "REVISION_OF", Relation.a_sha == prev.content_sha,
                                                  Relation.b_sha == v.content_sha))
            details = {"lineage": lin.id, "from": prev.revision_label, "to": v.revision_label, "diff": d["summary"]}
            if rel is None:
                s.add(Relation(kind="REVISION_OF", a_sha=prev.content_sha, b_sha=v.content_sha,
                               score=d["similarity"], details=details))
            else:
                rel.score, rel.details = d["similarity"], details
        else:
            v.diff_from_parent = {}
        prev = v
    for v in versions:
        if v.variant_of_id:
            rep = next(r for r in reps if r.id == v.variant_of_id)
            v.ordinal = rep.ordinal
    # drop REVISION_OF edges that no longer reflect adjacency
    adj = {(reps[i].content_sha, reps[i + 1].content_sha) for i in range(len(reps) - 1)}
    shas = [v.content_sha for v in versions]
    for rel in s.scalars(select(Relation).where(Relation.kind == "REVISION_OF", Relation.a_sha.in_(shas))):
        if (rel.a_sha, rel.b_sha) not in adj:
            s.delete(rel)

    lin.ordering_basis, lin.ordering_confidence, lin.has_conflict = basis, conf, conflict
    lin.updated_at = clock.now()
    new_head = reps[-1]
    if old_head != new_head.id:
        audit.append(s, "system", "lineage.head_changed", "lineage", lin.id,
                     {"doc_number": lin.doc_number, "head_revision": new_head.revision_label,
                      "head_content": new_head.content_sha,
                      "versions": len(reps), "basis": basis, "confidence": conf})
    s.flush()


def superseded_by_release(lin: Lineage, v: Version) -> Version | None:
    """For document-control lineages: the first released revision after v."""
    reps = sorted((x for x in lin.versions if not x.variant_of_id), key=lambda x: x.ordinal)
    for x in reps:
        if x.ordinal > v.ordinal and x.released:
            return x
    return None
