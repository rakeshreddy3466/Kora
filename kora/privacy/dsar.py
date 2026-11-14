"""GDPR data-subject rights over KORA's *derived* data.

The systems of record (SharePoint, OneDrive, Play) stay authoritative for the
original files; KORA answers for what it derived from them: transcripts,
OCR text, extracted facts, capsules, search index, audio tracks.

Art. 15 access  export()          everything KORA holds that concerns a person
Art. 17 erasure request_erasure() opens a DPO review task (four eyes: the
                                  requester cannot approve); on approval erase():
   * speaker labels and name/e-mail mentions in derived text -> pseudonym
   * the person's voice: speech audio dropped from capsules of recordings
     in which they speak (the blob is deleted once nothing references it)
   * search index rebuilt for affected units, capsules re-signed
   * Art. 17(3) exemptions: content under legal hold is skipped and reported
   * the directory entry is pseudonymised; the audit log only ever held ids
Biometrics: KORA never derives voiceprints or face templates (Art. 9); speaker
names come only from the meeting platform's own transcript labels.
"""
from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import audit, clock
from ..config import get_settings
from ..models import AuditEvent, Capsule, Content, File, Principal, ReviewTask, SlideBlob, Unit
from ..search.index import index_content
from ..security.auth import Identity
from ..storage import get_blobstore
from ..util import hmac_hex


def pseudonym(person_id: str) -> str:
    return "person-" + hmac_hex(get_settings().audit_mac_key, f"pseudonym:{person_id}".encode())[:10]


def _identifiers(p: Principal) -> list[str]:
    ids = [p.display_name.split(" (")[0]] if p.display_name else []
    if p.email:
        ids.append(p.email)
    return [i for i in ids if i and not i.startswith("person-")]


_FOLD = {"a": "aáàâäãåā", "e": "eéèêëē", "i": "iíìîïī", "o": "oóòôöõøō", "u": "uúùûüū", "n": "nñń",
         "c": "cçć", "y": "yýÿ", "s": "sšś", "z": "zžź"}


def name_pattern(name: str) -> re.Pattern:
    """Accent- and case-insensitive whole-name pattern: 'Jose Nunez' also matches 'José Núñez'
    (real-data finding: the same person's name was spelled both ways in chat and on a slide)."""
    parts = []
    for ch in name:
        low = ch.lower()
        if low in _FOLD:
            parts.append("[" + _FOLD[low] + _FOLD[low].upper() + "]")
        elif ch.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    return re.compile(r"(?<!\w)" + "".join(parts) + r"(?!\w)", re.IGNORECASE)


def _mentions(s: Session, p: Principal) -> list[Unit]:
    ids = _identifiers(p)
    if not ids:
        return []
    pats = [name_pattern(i) for i in ids]
    return [u for u in s.scalars(select(Unit))
            if (u.speaker and any(pt.fullmatch(u.speaker) for pt in pats)) or any(pt.search(u.text or "") for pt in pats)]


def export(s: Session, ident: Identity, person_id: str) -> dict:
    if not ident.has("dpo"):
        raise PermissionError("dpo role required")
    p = s.get(Principal, person_id)
    if p is None:
        raise ValueError("unknown person")
    units = _mentions(s, p)
    names = _identifiers(p)
    out = {
        "person": {"id": p.id, "display_name": p.display_name, "email": p.email, "status": p.status,
                   "groups": p.groups, "team": p.team},
        "generated_at": clock.now().isoformat(),
        "files_owned": [{"file_id": f.id, "system": f.system, "path": f.path, "status": f.status}
                        for f in s.scalars(select(File).where(File.owner_id == person_id))],
        "spoken_segments": [{"content": u.content_sha, "kind": u.kind, "start_s": u.start_s, "end_s": u.end_s,
                             "time": (u.meta or {}).get("time"), "text": u.text}
                            for u in units if u.speaker and any(name_pattern(n).fullmatch(u.speaker) for n in names)],
        "mentions": [{"content": u.content_sha, "kind": u.kind, "ordinal": u.ordinal, "start_s": u.start_s,
                      "text": u.text[:500],
                      **({"named_as_slide_responsible": any(name_pattern(n).fullmatch(x) for n in names
                                                            for x in (u.meta or {}).get("people") or [])}
                         if u.kind == "note" else {})} for u in units
                     if not (u.speaker and any(name_pattern(n).fullmatch(u.speaker) for n in names))],
        "actions_performed": [{"seq": e.seq, "at": e.at.isoformat(), "action": e.action}
                              for e in s.scalars(select(AuditEvent).where(AuditEvent.actor == person_id))],
        "processing": {"purposes": ["knowledge management", "storage optimisation", "records governance"],
                       "legal_basis": "legitimate interest of the employer (Art. 6(1)(f)); see docs/GDPR.md",
                       "recipients": "none outside the deployment; no external AI services",
                       "retention": "derived data follows the source item's retention label"},
    }
    audit.append(s, ident.principal_id, "dsar.access_exported", "principal", person_id,
                 {"spoken_segments": len(out["spoken_segments"]), "mentions": len(out["mentions"])})
    return out


def request_erasure(s: Session, ident: Identity, person_id: str, reason: str) -> ReviewTask:
    from ..hitl import open_task
    p = s.get(Principal, person_id)
    if p is None:
        raise ValueError("unknown person")
    units = _mentions(s, p)
    t = open_task(s, key=f"erasure:{person_id}:{clock.now():%Y%m%d}", kind="erasure_request",
                  subject_type="principal", subject_id=person_id,
                  question=f"Erase {person_id} from KORA's derived data (transcripts, OCR text, facts, voice audio)?",
                  options=["approve_erasure", "reject"],
                  evidence={"requested_by": ident.principal_id, "reason": reason[:500],
                            "units_affected": len(units),
                            "contents_affected": len({u.content_sha for u in units})},
                  role="dpo", priority=5, blocks="erasure (GDPR Art. 17) - 1 month deadline (Art. 12(3))")
    audit.append(s, ident.principal_id, "dsar.erasure_requested", "principal", person_id, {"task": t.id})
    return t


def _blob_referenced(s: Session, key: str) -> bool:
    if s.scalar(select(Unit.id).where(or_(Unit.blob_key == key, Unit.thumb_key == key)).limit(1)):
        return True
    if s.scalar(select(SlideBlob.unit_hash).where(SlideBlob.blob_key == key).limit(1)):
        return True
    for cap in s.scalars(select(Capsule)):
        if any(a.get("blob") == key or a.get("slide_blob") == key for a in cap.manifest.get("artifacts", [])):
            return True
    return any((c.media or {}).get("audio_blob") == key for c in s.scalars(select(Content).where(Content.detected_type == "video")))


def erase(s: Session, ident: Identity, person_id: str) -> dict:
    from ..capsule import builder
    p = s.get(Principal, person_id)
    ids = _identifiers(p)
    pseudo = pseudonym(person_id)
    store = get_blobstore()
    held = {f.sha256 for f in s.scalars(select(File).where(File.legal_hold.is_(True)))}
    touched, skipped, voice = set(), set(), set()
    old_blobs = set()
    for u in _mentions(s, p):
        if u.content_sha in held:
            skipped.add(u.content_sha)                         # Art. 17(3)(e): legal claims
            continue
        pats = [name_pattern(i) for i in sorted(ids, key=len, reverse=True)]
        if u.speaker and any(pt.fullmatch(u.speaker) for pt in pats):
            u.speaker = pseudo
            voice.add(u.content_sha)
        txt = u.text or ""
        for pt in pats:
            txt = pt.sub(pseudo, txt)
        u.text = txt
        if (u.meta or {}).get("spec"):                       # derived slide description: recomputed on demand
            u.meta = {k: v for k, v in u.meta.items() if k != "spec"}
        if (u.meta or {}).get("people"):                     # "Slide responsible: ..." parsed from speaker notes
            u.meta = {**u.meta, "people": [pseudo if any(pt.fullmatch(x) for pt in pats) else x
                                           for x in u.meta["people"]]}
        touched.add(u.content_sha)
    s.flush()
    for sha in touched:
        c = s.get(Content, sha)
        cap = s.get(Capsule, sha)
        if cap:
            old_blobs |= {a["blob"] for a in cap.manifest["artifacts"] if a.get("role") in ("transcript", "audio")}
        if sha in voice and (c.media or {}).get("audio_blob"):
            c.media = {**c.media, "audio_blob": None, "audio_erased": {"reason": "data-subject erasure",
                                                                     "at": clock.now().isoformat()}}
        index_content(s, sha)
        builder.build(s, sha)
    s.flush()
    deleted = 0
    for key in old_blobs:
        if not _blob_referenced(s, key):
            store.delete("hot", key)
            deleted += 1
    p.display_name, p.email, p.status = pseudo, None, "erased"
    result = {"pseudonym": pseudo, "contents_updated": len(touched), "voice_audio_removed": len(voice - skipped),
              "blobs_deleted": deleted, "skipped_legal_hold": len(skipped),
              "not_covered": ["original files in the systems of record (owner must act there)",
                              "faces or names visible inside keyframe images (flag for manual review)"]}
    audit.append(s, ident.principal_id, "dsar.erasure_executed", "principal", person_id,
                 {k: v for k, v in result.items() if k != "pseudonym"})
    return result
