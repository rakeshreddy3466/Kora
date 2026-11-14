"""Knowledge Recovery Capsule: a compact, verifiable, signed package that lets
the organisation search and understand material without keeping every large
original readily accessible.

manifest (canonical JSON) ->  manifest_hash = sha256(manifest)
                              signature     = HMAC-SHA256(capsule key, manifest_hash)
Every artifact in the manifest is a content-addressed blob, so verifying the
capsule = verifying the signature + re-hashing each artifact.
"""
from __future__ import annotations

import hmac as _hmac
import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import CAPSULE_SCHEMA, __version__, clock
from ..config import get_settings
from ..dedup import classes as C
from ..knowledge import facts as factx
from ..models import Capsule, Content, Lineage, Relation, SlideBlob, Unit, Version
from ..queries import display_name, files_of
from ..storage import get_blobstore
from ..util import canonical_json, hmac_hex, sha256_bytes
from . import validator


def _locations(s: Session, sha: str) -> list[dict]:
    return [{"file_id": f.id, "system": f.system, "site": f.site, "path": f.path, "status": f.status,
             "owner": f.owner_id} for f in files_of(s, sha, None)]


def _relations(s: Session, sha: str) -> list[dict]:
    out = []
    for r in s.scalars(select(Relation).where(or_(Relation.a_sha == sha, Relation.b_sha == sha))):
        other = r.b_sha if r.a_sha == sha else r.a_sha
        role = "a" if r.a_sha == sha else "b"
        out.append({"kind": r.kind, "role": role, "other": other, "other_name": display_name(s, other),
                    "score": r.score})
    return sorted(out, key=lambda x: (x["kind"], x["other"]))


def build(s: Session, sha: str) -> Capsule:
    store = get_blobstore()
    c = s.get(Content, sha)
    units = list(s.scalars(select(Unit).where(Unit.content_sha == sha).order_by(Unit.kind, Unit.ordinal)))
    artifacts: list[dict] = []
    retained, transformed, omitted = [], [], []

    if c.detected_type == "video":
        segs = [u for u in units if u.kind == "segment"]
        frames = [u for u in units if u.kind == "frame"]
        transcript = [{"start": u.start_s, "end": u.end_s, "speaker": u.speaker, "text": u.text,
                       "frame": (u.meta or {}).get("frame_ordinal"), "slide": (u.meta or {}).get("slide")}
                      for u in sorted(segs, key=lambda x: x.ordinal)]
        if transcript:
            ref = store.put_bytes("hot", canonical_json({"segments": transcript}))
            artifacts.append({"role": "transcript", "blob": ref.key, "size": ref.size,
                              "media_type": "application/json", "segments": len(transcript),
                              "source": (c.media or {}).get("transcript_source")})
            retained.append("speaker-attributed transcript with timestamps")
        for f in sorted(frames, key=lambda x: x.ordinal):
            if f.blob_key:
                artifacts.append({"role": "keyframe", "ordinal": f.ordinal, "t": f.start_s, "until": f.end_s,
                                  "blob": f.blob_key, "size": store.size("hot", f.blob_key), "phash": f.phash,
                                  "slide": (f.meta or {}).get("slide"), "ocr_chars": len(f.text or "")})
        if frames:
            retained.append(f"{len(frames)} scene-change keyframes (JPEG), time-linked")
        audio = (c.media or {}).get("audio_blob")
        if audio:
            artifacts.append({"role": "audio", "blob": audio, "size": store.size("hot", audio),
                              "codec": "opus", "bitrate_kbps": 16, "channels": 1})
            transformed.append("audio re-encoded to Opus 16 kbps mono (speech intelligible, not archival)")
        if (c.media or {}).get("audio_erased"):
            omitted.append("speech audio removed after a data-subject erasure request (GDPR Art. 17)")
        omitted.append(f"original video stream ({c.media.get('vcodec')} {c.media.get('width')}x{c.media.get('height')}, "
                       f"{round((c.media.get('bit_rate') or 0) / 1000)} kbps)")
    else:
        for u in units:
            if u.kind not in ("page", "slide", "image"):
                continue
            sb = s.get(SlideBlob, u.unit_hash) if u.unit_hash else None
            own = sb is not None and sb.first_content_sha == sha       # count shared pages once, at their first owner
            tsize = store.size("hot", u.thumb_key) if u.thumb_key and store.exists("hot", u.thumb_key) else 0
            artifacts.append({"role": u.kind, "ordinal": u.ordinal, "unit_hash": u.unit_hash, "body_hash": u.body_hash,
                              "text_hash": u.text_hash, "slide_blob": sb.blob_key if sb else None,
                              "size": (sb.size_bytes if own else 0) + tsize, "thumb": u.thumb_key})
        for u in units:
            if u.kind == "message":
                artifacts.append({"role": "message", "ordinal": u.ordinal, "author": u.speaker,
                                  "time": (u.meta or {}).get("time"), "text_hash": u.text_hash, "size": 0})
        retained.append("per-page text, unit hashes, thumbnails; page PDFs only when a compaction needs them")
        if c.detected_type == "pptx" and not any(a.get("slide_blob") for a in artifacts):
            omitted.append("slide rendering (PPTX not rendered; enable KORA_PPTX_RENDERER=libreoffice)")
        omitted.append("original container bytes (fonts subset/object layout may differ on rebuild)")

    version = s.scalar(select(Version).where(Version.content_sha == sha))
    lineage = s.get(Lineage, version.lineage_id) if version else None
    acl = sorted({a for f in files_of(s, sha, None) for a in (f.acl or [])})
    facts = factx.extract(s, sha)
    manifest = {
        "schema": CAPSULE_SCHEMA,
        "kora_version": __version__,
        "created_at": clock.now().isoformat(),
        "source": {"sha256": sha, "size_bytes": c.size_bytes, "type": c.detected_type,
                   "duration_s": c.duration_s, "locations": _locations(s, sha)},
        "metadata": {k: v for k, v in (c.meta or {}).items() if k not in ("toc",)},
        "versioning": ({"lineage": lineage.key, "doc_number": lineage.doc_number, "revision": version.revision_label,
                        "ordinal": version.ordinal, "is_head": version.is_head, "variant_of": version.variant_of_id,
                        "ordering": f"{lineage.ordering_basis}/{lineage.ordering_confidence}"}
                       if version else None),
        "artifacts": artifacts,
        "facts": facts,
        "relations": _relations(s, sha),
        "acl_snapshot": acl,
        "recovery_manifest": {"retained": retained, "transformed": transformed, "omitted": omitted,
                              "classes": C.RECOVERY},
    }
    mh = sha256_bytes(canonical_json(manifest))
    sig = hmac_hex(get_settings().capsule_signing_key, mh.encode())
    cap = s.get(Capsule, sha)
    if cap is None:
        cap = Capsule(content_sha=sha, schema=CAPSULE_SCHEMA, manifest=manifest, manifest_hash=mh, signature=sig)
        s.add(cap)
    else:
        cap.manifest, cap.manifest_hash, cap.signature, cap.created_at = manifest, mh, sig, clock.now()
    cap.size_bytes = sum(int(a.get("size") or 0) for a in artifacts) + len(canonical_json(manifest))
    s.flush()
    cap.validation = validator.validate(s, cap)
    cap.valid = cap.validation["valid"]
    return cap


def verify_signature(cap: Capsule) -> bool:
    mh = sha256_bytes(canonical_json(json.loads(json.dumps(cap.manifest))))
    return mh == cap.manifest_hash and _hmac.compare_digest(
        hmac_hex(get_settings().capsule_signing_key, mh.encode()), cap.signature)
