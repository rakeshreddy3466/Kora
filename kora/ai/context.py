"""RAG context packs: what an AI assistant may read to answer one person's question.

The same rules as the AI dataset export, applied per question and per person:
  * the asker's own permissions (search is ACL-filtered; capsules keep the ACL snapshot
    after the original is deleted, so deleted files stay usable but never leak)
  * only sources a governance decision opted in (`ai_use_allowed`)
  * personal data redacted, speakers as stable pseudonyms
  * every chunk carries a citation: document number, revision (and whether it is the
    current one), page or timestamp, and where the original lives (or that only the
    capsule is left)
  * the audit log records that a context pack was served, never the question text
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..hitl import known_names
from ..models import Content, File, Source
from ..privacy import pii
from ..privacy.dsar import pseudonym
from ..search.index import search
from ..security.acl import can_read
from ..security.auth import Identity
from ..util import sha256_bytes

INSTRUCTIONS = ("Answer only from the numbered sources. Cite them as [n]. Prefer sources marked current; "
                "if a source is a superseded revision, an unreviewed machine transcript, or past its "
                "best-before date, say so. "
                "If the sources do not answer the question, say that you do not know.")


def ai_sources(s: Session) -> set[str]:
    return {src.name for src in s.scalars(select(Source)) if (src.config or {}).get("ai_use_allowed")}


def _cite(h: dict) -> dict:
    t = h.get("type")
    c = {"type": t, "content": h["content"],
         "where": [f"{l['system']}:{l['path']}" for l in h.get("locations", [])] or [h.get("original") or "capsule only"]}
    if t in ("slide", "speaker_notes"):
        v = h.get("version") or {}
        c.update(document=h.get("document"), page=h.get("page"), doc_number=h.get("doc_number"),
                 revision=v.get("revision"), current=v.get("is_head", True),
                 latest=v.get("latest_revision"), in_latest=v.get("in_latest"))
        if h.get("best_before"):
            c.update(best_before=h["best_before"], past_best_before=h.get("past_best_before", False))
    elif t == "transcript":
        c.update(recording=h.get("recording"), start_s=h.get("start_s"), end_s=h.get("end_s"),
                 quality=h.get("transcript_quality"),
                 slide_on_screen=(h.get("slide") or {}).get("doc_number"))
    elif t == "frame":
        c.update(t=h.get("t"))
    elif t == "message":
        c.update(document=h.get("document"), time=h.get("time"))
    return c


def _label(c: dict) -> str:
    t = c["type"]
    if t in ("slide", "speaker_notes"):
        rev = f" rev {c['revision']}" + ("" if c.get("current", True) else f" (superseded by {c.get('latest')})") \
            if c.get("revision") else ""
        where = "speaker notes of slide" if t == "speaker_notes" else "page"
        stale = f" (past its best-before date, {c['best_before']})" if c.get("past_best_before") else ""
        return f"{c.get('doc_number') or c.get('document')}{rev}, {where} {c.get('page')}{stale}"
    if t == "transcript":
        m, sec = divmod(int(c.get("start_s") or 0), 60)
        return f"{c.get('recording')} at {m}:{sec:02d} ({c.get('quality')})"
    if t == "frame":
        m, sec = divmod(int(c.get("t") or 0), 60)
        return f"recording frame at {m}:{sec:02d}"
    return str(c.get("document") or t)


def context(s: Session, ident: Identity, q: str, k: int = 8) -> dict:
    allowed = ai_sources(s)
    names = known_names(s)
    hits = search(s, ident, q, limit=max(1, min(k, 25)) * 2)["results"]
    chunks, excluded = [], {"source_not_opted_in": 0}
    for h in hits:
        files = list(s.scalars(select(File).where(File.sha256 == h["content"])))
        mine = [f for f in files if can_read(ident.acl_tokens, f.acl)]
        # the opt-in must cover a copy this person can read: a share in a private OneDrive does not
        # inherit the team site's AI decision (deleted originals: the capsule's sources decide)
        srcs = {f.source_name for f in (mine or [f for f in files if f.status != "active"])}
        if not srcs & allowed:
            excluded["source_not_opted_in"] += 1
            continue
        c = s.get(Content, h["content"])
        keep_pii = (c.meta or {}).get("pii_review", {}).get("decision") == "not_personal_data"
        text = h.get("text") or ""
        text = text if keep_pii else pii.redact(text, names)
        speaker = h.get("speaker")
        cite = _cite(h)
        chunks.append({"n": len(chunks) + 1, "text": text,
                       "speaker": pseudonym(speaker) if speaker else None,
                       "cite": cite, "label": _label(cite), "score": h.get("score")})
        if len(chunks) >= k:
            break
    block = "\n\n".join(f"[{ch['n']}] {ch['text']}\n    source: {ch['label']}" for ch in chunks)
    audit.append(s, ident.principal_id, "ai.context_served", "query", sha256_bytes(q.encode())[:16],
                 {"chunks": len(chunks), "excluded": excluded})
    return {"query": q, "instructions": INSTRUCTIONS, "chunks": chunks, "context_block": block,
            "excluded": excluded, "sources_opted_in": sorted(allowed)}
