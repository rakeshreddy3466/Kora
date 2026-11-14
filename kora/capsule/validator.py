"""Capsule validation: is the capsule complete and trustworthy enough to stand
in for the original? Hard checks gate deletion; soft checks lower confidence."""
from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..fingerprint.text import tokens
from ..governance.policy import get_policy
from ..models import Capsule, Content, SlideBlob, Unit
from ..storage import get_blobstore


def _check(name: str, passed: bool, hard: bool, detail: str, value=None) -> dict:
    return {"name": name, "passed": bool(passed), "hard": hard, "detail": detail, "value": value}


def _coverage(intervals: list[tuple[float, float]], duration: float) -> float:
    if duration <= 0:
        return 0.0
    total, cur_s, cur_e = 0.0, None, None
    for a, b in sorted(intervals):
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        total += cur_e - cur_s
    return min(1.0, total / duration)


def validate(s: Session, cap: Capsule) -> dict:
    from .builder import verify_signature
    store = get_blobstore()
    pol = get_policy()
    c = s.get(Content, cap.content_sha)
    checks = [_check("signature_valid", verify_signature(cap), True, "manifest hash + HMAC signature")]
    bad = []
    for a in cap.manifest["artifacts"]:
        key = a.get("blob") or a.get("slide_blob")
        if key and not store.verify("hot", key):
            bad.append(a.get("role") + str(a.get("ordinal", "")))
    checks.append(_check("artifacts_intact", not bad, True,
                         "all artifacts re-hash to their content address" if not bad else f"corrupt: {bad[:5]}"))

    if c.detected_type == "video":
        rp = pol["recordings"]
        segs = list(s.scalars(select(Unit).where(Unit.content_sha == c.sha256, Unit.kind == "segment")))
        frames = list(s.scalars(select(Unit).where(Unit.content_sha == c.sha256, Unit.kind == "frame")
                                .order_by(Unit.ordinal)))
        dur = c.duration_s or 0
        checks.append(_check("transcript_present", bool(segs), True,
                             f"{len(segs)} segments ({(c.media or {}).get('transcript_source') or 'no transcript source'})"))
        # Speech is never continuous, so "seconds covered by cues" understates completeness.
        # Instead: the cues must span the recording and no stretch may go untranscribed for long.
        ivals = sorted((u.start_s or 0, u.end_s or 0) for u in segs)
        speech = _coverage(ivals, dur)
        if ivals:
            span = (ivals[-1][1] - ivals[0][0]) / dur if dur else 0
            gaps = [ivals[0][0]] + [b[0] - a[1] for a, b in zip(ivals, ivals[1:])] + [dur - ivals[-1][1]]
            max_gap = max(gaps)
        else:
            span, max_gap = 0.0, dur
        ok = bool(segs) and span >= rp["min_transcript_coverage"] and max_gap <= rp.get("max_untranscribed_gap_s", 180)
        checks.append(_check("transcript_coverage", ok, True,
                             f"cues span {span:.0%} of {dur:.0f}s, longest untranscribed gap {max_gap:.0f}s, "
                             f"speech {speech:.0%}", {"span": round(span, 3), "max_gap_s": round(max_gap, 1),
                                                     "speech_ratio": round(speech, 3)}))
        src = str((c.media or {}).get("transcript_source") or "")
        if src.startswith("asr"):
            rev = (c.meta or {}).get("transcript_review") or {}
            need = rp.get("require_review_for_asr", True)
            checks.append(_check("transcript_reviewed", rev.get("decision") == "approve" or not need, need,
                                 f"machine transcript ({src}) approved by {rev['by']}" if rev.get("decision") == "approve"
                                 else f"machine transcript ({src}) awaiting human review" if not rev
                                 else f"machine transcript rejected by {rev.get('by')}"))
        ts_ok = all(0 <= (u.start_s or 0) <= (u.end_s or 0) <= dur + 1 for u in segs)
        checks.append(_check("timestamps_valid", ts_ok, True, "0 <= start <= end <= duration for every cue"))
        dec = 0
        for f in frames:
            try:
                Image.open(io.BytesIO(store.get_bytes("hot", f.blob_key))).verify()
                dec += 1
            except Exception:
                pass
        checks.append(_check("keyframes_decodable", frames and dec == len(frames), True,
                             f"{dec}/{len(frames)} keyframes decode"))
        gaps = [(b.start_s or 0) - (a.start_s or 0) for a, b in zip(frames, frames[1:])]
        if frames:
            gaps.append(dur - (frames[-1].start_s or 0))
        mg = max(gaps) if gaps else dur
        checks.append(_check("keyframe_coverage", mg <= rp["max_keyframe_gap_s"], False,
                             f"largest gap between keyframes {mg:.0f}s", round(mg, 1)))
        spk = sum(1 for u in segs if u.speaker) / len(segs) if segs else 0
        checks.append(_check("speaker_attribution", spk >= 0.9 if rp.get("require_speaker_attribution") else True,
                             bool(rp.get("require_speaker_attribution")),
                             f"{spk:.0%} of segments attributed" + ("" if spk >= 0.9 else " (attribution uncertain)"),
                             round(spk, 3)))
        linked = sum(1 for f in frames if (f.meta or {}).get("slide"))
        checks.append(_check("frame_slide_links", True, False, f"{linked}/{len(frames)} keyframes linked to a source slide",
                             linked))
        has_audio = any(a["role"] == "audio" for a in cap.manifest["artifacts"])
        checks.append(_check("audio_retained", has_audio, False,
                             "Opus 16 kbps speech track retained" if has_audio else "no audio retained"))
        # spoken content not backed by any slide = knowledge that exists only in this recording
        uniq = 0
        slide_text = {}
        for u in segs:
            sl = (u.meta or {}).get("slide")
            if not sl:
                uniq += 1
                continue
            if sl["unit_hash"] not in slide_text:
                su = s.scalar(select(Unit).where(Unit.unit_hash == sl["unit_hash"]).limit(1))
                slide_text[sl["unit_hash"]] = set(tokens(su.text if su else ""))
            st = slide_text[sl["unit_hash"]]
            tt = set(tokens(u.text))
            if tt and len(tt & st) / len(tt) < 0.2:
                uniq += 1
        checks.append(_check("unique_sections", True, False,
                             f"{uniq}/{len(segs)} transcript segments carry content not on any slide (kept in capsule)",
                             uniq))
    else:
        units = list(s.scalars(select(Unit).where(Unit.content_sha == c.sha256, Unit.kind != "note")))
        checks.append(_check("units_extracted", bool(units) and len(units) == c.unit_count, True,
                             f"{len(units)} units extracted ({', '.join(sorted({u.kind for u in units}))})"))
        if c.detected_type in ("pdf", "pptx"):
            in_store = sum(1 for u in units if u.unit_hash and s.get(SlideBlob, u.unit_hash))
            checks.append(_check("slide_store", True, False,
                                 f"{in_store}/{len(units)} pages stored; the rest are materialised and proven "
                                 "only when a compaction is planned (KORA keeps no second copy of live documents)",
                                 in_store))
    hard_ok = all(ch["passed"] for ch in checks if ch["hard"])
    w = [(1 if ch["hard"] else 0.5) for ch in checks]
    score = sum(wi for wi, ch in zip(w, checks) if ch["passed"]) / sum(w)
    return {"valid": hard_ok, "score": round(100 * score, 1), "checks": checks}
