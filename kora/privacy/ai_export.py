"""Data activation: export knowledge as an AI-ready dataset (JSONL), safely.

Every record is a unit of knowledge (slide, transcript passage, keyframe OCR)
with provenance (content, document number, revision, page/timestamp, speaker
pseudonym) so RAG answers and training data stay traceable to the source.

Rules (GDPR purpose limitation + minimisation):
  * only sources whose config sets ai_use_allowed (default: false)
  * PII redacted (e-mail, phone, personnummer, IBAN, IP, names of people)
    unless a DPO decided the content holds no personal data
  * speakers exported as stable pseudonyms, never names
  * only the head revision of each lineage, and each slide body once
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..hitl import known_names
from ..knowledge.notes import is_past
from ..models import Content, File, Source, Unit, Version
from ..security.auth import Identity
from . import pii
from .dsar import pseudonym


def export(s: Session, ident: Identity, out: Path) -> dict:
    if not ident.has("analyst"):
        raise PermissionError("analyst role required")
    allowed_sources = {src.name for src in s.scalars(select(Source)) if (src.config or {}).get("ai_use_allowed")}
    names = known_names(s)
    speaker_ids = {}
    n_rec, n_red, seen_bodies, skipped = 0, 0, set(), {"not_allowed": 0, "superseded": 0, "duplicate_slide": 0}
    with open(out, "w") as fh:
        for c in s.scalars(select(Content).where(Content.extracted.is_(True))):
            srcs = {f.source_name for f in s.scalars(select(File).where(File.sha256 == c.sha256, File.status != "purged"))}
            if not srcs & allowed_sources:
                skipped["not_allowed"] += 1
                continue
            v = s.scalar(select(Version).where(Version.content_sha == c.sha256))
            if v is not None and not v.is_head:
                skipped["superseded"] += 1
                continue
            redact = (c.meta or {}).get("pii_review", {}).get("decision") != "not_personal_data"
            for u in s.scalars(select(Unit).where(Unit.content_sha == c.sha256).order_by(Unit.kind, Unit.ordinal)):
                if not (u.text or "").strip():
                    continue
                if u.kind in ("page", "slide"):
                    if u.body_hash in seen_bodies:
                        skipped["duplicate_slide"] += 1
                        continue
                    seen_bodies.add(u.body_hash)
                text = pii.redact(u.text, names) if redact else u.text
                n_red += text != u.text
                spk = None
                if u.speaker:
                    spk = speaker_ids.setdefault(u.speaker, pseudonym(u.speaker))
                rec = {"id": u.id, "kind": u.kind, "text": text, "speaker": spk,
                       "provenance": {"content": c.sha256, "doc_number": (c.meta or {}).get("doc_number"),
                                      "revision": v.revision_label if v else None, "ordinal": u.ordinal,
                                      "start_s": u.start_s, "end_s": u.end_s,
                                      "slide": (u.meta or {}).get("slide"),
                                      **({"best_before": (u.meta or {})["best_before"],
                                          "past_best_before": is_past((u.meta or {})["best_before"])}
                                         if (u.meta or {}).get("best_before") else {})}}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_rec += 1
    res = {"records": n_rec, "redacted_records": n_red, "skipped": skipped, "sources": sorted(allowed_sources)}
    audit.append(s, ident.principal_id, "ai_dataset.exported", "dataset", out.name, res)
    return res
