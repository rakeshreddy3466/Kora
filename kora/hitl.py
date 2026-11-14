"""Human in the loop.

KORA automates the *unambiguous* (identical bytes, render-identical pages,
document-control revisions) and routes everything else to a person, with
the evidence needed to decide quickly. A review task names who may decide
(role), what is blocked until then, and the allowed answers; every decision
is attributed and audited, and has a defined effect.

Generated automatically:
  near_duplicate     same words, no shared slides       -> same document or not?
  lineage_order      versions ordered by weak evidence   -> confirm order (enables compaction)
  lineage_conflict   two contents claim one revision     -> which is authoritative?
  transcript_review  machine (ASR) transcript            -> approve before the recording may be distilled
  pii_review         personal data found in derived text -> keep (with redaction for AI use) or confirm not personal
Requested by people:
  erasure_request    GDPR Art. 17 for a person           -> DPO approves; KORA then erases from derived data
  (deletion plans have their own approval flow, see governance/executor.py)
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, clock
from .dedup import classes as C
from .models import Content, Lineage, Principal, Relation, ReviewTask, Unit, Version
from .privacy import pii
from .queries import display_name
from .security.auth import Identity


class ReviewError(Exception):
    pass


def open_task(s: Session, *, key: str, kind: str, subject_type: str, subject_id: str, question: str,
              options: list[str], evidence: dict, role: str = "analyst", priority: int = 50,
              blocks: str | None = None) -> ReviewTask:
    t = s.scalar(select(ReviewTask).where(ReviewTask.key == key))
    if t is not None:
        if t.status == "open":
            t.evidence = evidence                       # keep evidence fresh, never reopen a decided task
            t.question = question
        return t
    try:
        with s.begin_nested():
            t = ReviewTask(key=key, kind=kind, subject_type=subject_type, subject_id=subject_id, question=question,
                           options=options, evidence=evidence, required_role=role, priority=priority, blocks=blocks)
            s.add(t)
            s.flush()
    except IntegrityError:
        t = s.scalar(select(ReviewTask).where(ReviewTask.key == key))
    return t


def known_names(s: Session) -> list[str]:
    from .knowledge.notes import people_named
    names = {p.display_name.split(" (")[0] for p in s.scalars(select(Principal))}
    names |= {sp for sp in s.scalars(select(Unit.speaker).where(Unit.speaker.is_not(None)).distinct())}
    names |= set(people_named(s))                    # "Slide responsible: ..." in speaker notes
    return sorted(n for n in names if n and not n.startswith("person-"))


def generate(s: Session, sha: str) -> list[ReviewTask]:
    """Create the review tasks implied by one content's current state (idempotent)."""
    out = []
    c = s.get(Content, sha)
    if c is None:
        return out
    for r in s.scalars(select(Relation).where(Relation.kind == C.REL_NEAR,
                                              or_(Relation.a_sha == sha, Relation.b_sha == sha))):
        out.append(open_task(
            s, key=f"near:{r.a_sha}:{r.b_sha}", kind="near_duplicate", subject_type="relation", subject_id=r.id,
            question=f"Are '{display_name(s, r.a_sha)}' and '{display_name(s, r.b_sha)}' the same document?",
            options=["same_document", "different_documents"],
            evidence={"jaccard": r.score, "a": r.a_sha, "b": r.b_sha, "note": "similar wording, no identical slides"},
            priority=40, blocks="nothing is deleted for near-duplicates; decision is recorded for the owners"))
    v = s.scalar(select(Version).where(Version.content_sha == sha))
    if v is not None:
        lin = s.get(Lineage, v.lineage_id)
        reps = [x for x in lin.versions if not x.variant_of_id]
        if lin.has_conflict:
            conf = [x for x in reps if x.conflict]
            out.append(open_task(
                s, key=f"conflict:{lin.id}:{'-'.join(sorted(x.content_sha[:12] for x in conf))}", kind="lineage_conflict",
                subject_type="lineage", subject_id=lin.id,
                question=f"{len(conf)} different files claim revision {conf[0].revision_label!r} of {lin.key}. Which is authoritative?",
                options=[f"authoritative:{x.id}" for x in conf],
                evidence={"candidates": [{"version": x.id, "name": display_name(s, x.content_sha),
                                          "content": x.content_sha} for x in conf]},
                priority=20, blocks="compaction of every version in this lineage"))
        elif lin.ordering_confidence == "low" and len(reps) >= 2:
            out.append(open_task(
                s, key=f"order:{lin.id}:{len(reps)}", kind="lineage_order", subject_type="lineage", subject_id=lin.id,
                question=f"Are these versions of one document, in this order? " + " -> ".join(
                    f"{display_name(s, x.content_sha)}" for x in sorted(reps, key=lambda x: x.ordinal))
                    + ". Or are they different documents that share slides?",
                options=["confirm_order", "order_is_wrong", "not_the_same_document"],
                evidence={"basis": lin.ordering_basis,
                          "versions": [{"ordinal": x.ordinal, "revision": x.revision_label,
                                        "name": display_name(s, x.content_sha)} for x in reps]},
                priority=30, blocks="compaction of superseded versions (needs >= medium confidence)"))
    inherited = (c.media or {}).get("transcript_inherited_from")
    if c.detected_type == "video" and str((c.media or {}).get("transcript_source") or "").startswith("asr") \
            and not (inherited and s.scalar(select(ReviewTask.id).where(ReviewTask.key.like(f"transcript:{inherited}%")))):
        segs = list(s.scalars(select(Unit).where(Unit.content_sha == sha, Unit.kind == "segment").order_by(Unit.ordinal)))
        out.append(open_task(
            s, key=f"transcript:{sha}:{(c.media or {}).get('transcript_source')}", kind="transcript_review",
            subject_type="content", subject_id=sha,
            question=f"Machine transcript of '{display_name(s, sha)}': is it good enough to stand in for the recording?",
            options=["approve", "reject"],
            evidence={"source": c.media.get("transcript_source"), "segments": len(segs),
                      "engine": (c.media.get("asr") or {}).get("engine"),
                      "speakers": (c.media.get("asr") or {}).get("speakers"),
                      "sample": [{"t": u.start_s, "text": u.text[:160]} for u in segs[:: max(1, len(segs) // 5)][:5]]},
            priority=25, blocks="distilling this recording (capsule check 'transcript_reviewed')"))
    names = known_names(s)
    counts: dict[str, int] = {}
    found = []
    for u in s.scalars(select(Unit).where(Unit.content_sha == sha).order_by(Unit.kind, Unit.ordinal)):
        # names are expected in meetings, so only hard identifiers are flagged there; a name that speaker
        # notes label as the slide's responsible person identifies an employee and is flagged too
        named = ((u.meta or {}).get("people") or []) if u.kind == "note" else []
        for f in pii.find(u.text or "", names=named):
            kind = "NAME_IN_NOTES" if f.kind == "NAME" else f.kind
            counts[kind] = counts.get(kind, 0) + 1
            found.append({"kind": kind, "masked": f.masked, "unit": u.kind, "ordinal": u.ordinal})
    firsts = {}                                                 # one example of every kind first, then more
    for f in found:
        firsts.setdefault(f["kind"], f)
    samples = list(firsts.values())
    samples += [f for f in found if f not in samples][:max(0, 5 - len(samples))]
    authors = [str(v) for k, v in (c.meta or {}).items() if k in ("author", "last_modified_by", "sender", "creator")
               and v and str(v).strip().lower() not in ("", "anonymous", "unknown")]
    if authors:
        counts["AUTHOR_METADATA"] = len(authors)
    orig = (c.meta or {}).get("rendition_of")
    if counts and orig and s.scalar(select(ReviewTask.id).where(ReviewTask.key == f"pii:{orig}")):
        counts = {}                                            # the original's review decides for its rendition
    key = f"pii:{sha}"
    prev = s.scalar(select(ReviewTask).where(ReviewTask.key == key)) if counts else None
    if prev is not None and prev.status != "open":
        # decided before new kinds of personal data were detected (e.g. names in speaker notes):
        # a decision covers only what the reviewer saw, so the new kinds get their own review
        new = sorted(set(counts) - set((prev.evidence or {}).get("counts", {})))
        if new:
            key = f"pii:{sha}:" + "+".join(new).lower()
    if counts:
        out.append(open_task(
            s, key=key, kind="pii_review", subject_type="content", subject_id=sha,
            question=f"Personal data found in '{display_name(s, sha)}'. "
                     "Keep it redacted for AI use, or confirm it is not personal data?",
            options=["redact_for_ai_use", "not_personal_data"],
            evidence={"counts": counts, "samples": samples, "names_in_directory": len(names)},
            role="dpo", priority=15, blocks="export of this content to AI datasets"))
    from .knowledge.notes import freshness
    fr = freshness(c)
    if fr and fr["past"] and not (c.meta or {}).get("rendition_of"):
        out.append(open_task(
            s, key=f"stale:{sha}:{fr['best_before']}", kind="expired_content", subject_type="content", subject_id=sha,
            question=f"'{display_name(s, sha)}' is past its Best Before date ({fr['label'] or fr['best_before']}) on "
                     f"{fr['slides_past']} of {fr['slides']} slides. Retire it, ask the slide owner to update it, "
                     "or keep it as it is?",
            options=["retire", "ask_owner_to_update", "keep_as_is"],
            evidence={"best_before": fr["best_before"], "written_as": fr["label"], "slides_past_best_before": fr["slides_past"],
                      "slides": fr["slides"],
                      "people_named_in_notes": ((c.meta or {}).get("speaker_notes") or {}).get("people_named", 0)},
            role="analyst", priority=35,
            blocks="nothing is deleted automatically; 'retire' marks the deck as a candidate on the Files page"))
    return out


# --------------------------------------------------------------------------- decisions
def _apply(s: Session, t: ReviewTask, decision: str, ident: Identity) -> dict:
    if t.kind == "near_duplicate":
        r = s.get(Relation, t.subject_id)
        if r is not None:
            r.details = {**(r.details or {}), "human_decision": decision, "decided_by": ident.principal_id}
        return {"recorded": decision}
    if t.kind == "lineage_order":
        lin = s.get(Lineage, t.subject_id)
        if decision == "not_the_same_document":
            from .versioning.lineage import split
            return split(s, lin, ident.principal_id)
        if decision == "confirm_order":
            lin.ordering_confidence = "high"
            lin.ordering_basis = f"{lin.ordering_basis}+human"
        else:
            lin.has_conflict = True                       # blocks compaction until fixed at the source
        lin.evidence = {**(lin.evidence or {}), "order_review": {"decision": decision, "by": ident.principal_id}}
        return {"ordering_confidence": lin.ordering_confidence, "blocked": lin.has_conflict}
    if t.kind == "lineage_conflict":
        from .versioning.lineage import reorder
        keep = decision.split(":", 1)[1]
        lin = s.get(Lineage, t.subject_id)
        dropped = []
        for v in list(lin.versions):
            if v.conflict and v.id != keep:
                dropped.append(v.content_sha)
                s.delete(v)
        s.flush()
        lin.evidence = {**(lin.evidence or {}), "conflict_review": {"kept": keep, "by": ident.principal_id,
                                                                    "detached": dropped}}
        reorder(s, lin)
        return {"kept": keep, "detached": dropped, "has_conflict": lin.has_conflict}
    if t.kind == "transcript_review":
        from .capsule import builder
        review = {"decision": decision, "by": ident.principal_id, "at": clock.now().isoformat()}
        c = s.get(Content, t.subject_id)
        c.meta = {**(c.meta or {}), "transcript_review": review}
        cap = builder.build(s, c.sha256)
        # the same words live on in compressed renditions: one review decides for all of them
        for r in s.scalars(select(Content).where(Content.detected_type == "video")):
            if (r.media or {}).get("transcript_inherited_from") == c.sha256:
                r.meta = {**(r.meta or {}), "transcript_review": {**review, "via": c.sha256}}
                builder.build(s, r.sha256)
        return {"capsule_valid": cap.valid}
    if t.kind == "pii_review":
        c = s.get(Content, t.subject_id)
        c.meta = {**(c.meta or {}), "pii_review": {"decision": decision, "by": ident.principal_id}}
        for r in s.scalars(select(Content).where(Content.detected_type == c.detected_type)):
            if (r.meta or {}).get("rendition_of") == c.sha256:
                r.meta = {**(r.meta or {}), "pii_review": {"decision": decision, "by": ident.principal_id, "via": c.sha256}}
        return {"ai_export": "redacted" if decision == "redact_for_ai_use" else "allowed"}
    if t.kind == "expired_content":
        c = s.get(Content, t.subject_id)
        c.meta = {**(c.meta or {}), "freshness_review": {"decision": decision, "by": ident.principal_id,
                                                         "at": clock.now().isoformat()}}
        return {"recorded": decision}
    if t.kind == "erasure_request":
        if decision != "approve_erasure":
            return {"erased": False}
        from .privacy import dsar
        return dsar.erase(s, ident, t.subject_id)
    raise ReviewError(f"no effect defined for {t.kind}")


def decide(s: Session, ident: Identity, task_id: str, decision: str, comment: str = "") -> ReviewTask:
    t = s.scalars(select(ReviewTask).where(ReviewTask.id == task_id).with_for_update()).first()
    if t is None:
        raise ReviewError("unknown review task")
    if t.status != "open":
        raise ReviewError(f"task already {t.status}")
    if not ident.has(t.required_role):
        raise PermissionError(f"this decision requires role '{t.required_role}'")
    if t.kind == "erasure_request" and t.evidence.get("requested_by") == ident.principal_id:
        raise PermissionError("separation of duties: the requester cannot approve the erasure")
    if decision not in t.options:
        raise ReviewError(f"decision must be one of {t.options}")
    effect = _apply(s, t, decision, ident)
    t.status, t.decision, t.decided_by, t.decided_at, t.comment = "decided", decision, ident.principal_id, clock.now(), comment[:1000]
    audit.append(s, ident.principal_id, "review.decided", "review", t.id,
                 {"kind": t.kind, "subject": t.subject_id, "decision": decision, "effect": effect})
    return t


def stats(s: Session) -> dict:
    rows = s.execute(select(ReviewTask.kind, ReviewTask.status)).all()
    out: dict = {}
    for kind, status in rows:
        out.setdefault(kind, {}).setdefault(status, 0)
        out[kind][status] += 1
    return out
