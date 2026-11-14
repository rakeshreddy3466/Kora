#!/usr/bin/env python3
"""Governance, human-in-the-loop and GDPR scenario on an ingested real corpus.

Run after:  kora ingest-folder <ericsson files> --name ericsson-drive --owner data-owner
Usage:      python scripts/real_data_scenario.py --subject "<name of a chat participant>"
Writes <data_dir>/reports/real_scenario.json (contains personal data from the corpus: keep it local)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select

from kora import audit, bootstrap, clock, db, hitl
from kora.config import get_settings
from kora.governance import executor as ex, planner
from kora.governance.policy import get_policy, override_policy
from kora.jobs import Worker
from kora.models import AuditEvent, Capsule, Content, DeletionPlan, File, ReviewTask, Source, Unit
from kora.privacy import ai_export, dsar, pii
from kora.queries import display_name
from kora.search.index import search
from kora.security.auth import Identity

OWNER = Identity("data-owner", frozenset({"analyst", "viewer"}), ("hackathon",))
ANNA = Identity("rev-anna", frozenset({"approver", "analyst"}), ("hackathon",))
ERIK = Identity("op-erik", frozenset({"operator"}), ())
MARIA = Identity("dpo-maria", frozenset({"dpo", "analyst"}), ())
LARS = Identity("dpo-lars", frozenset({"dpo"}), ())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True, help="display name of the data subject for the GDPR steps")
    subject = ap.parse_args().subject
    bootstrap.init(json_logs=False)
    logging.getLogger().setLevel(logging.ERROR)
    out: dict = {}
    with db.session_scope() as s:
        for pid, name in (("rev-anna", "Anna Reviewer"), ("op-erik", "Erik Operator"), ("dpo-maria", "Yashwitha DPO"),
                          ("dpo-lars", "Second DPO"), ("subject", subject)):
            bootstrap.upsert_principal(s, {"id": pid, "display_name": name, "groups": ["hackathon"]})

    # 0 -- plans are built on fresh source state: rescan (unchanged content is not re-extracted)
    with db.session_scope() as s:
        bootstrap.scan_all(s)
    Worker(lease_s=600).drain()

    # 1 -- human review queue as produced by the pipeline
    with db.session_scope() as s:
        tasks = list(s.scalars(select(ReviewTask).where(ReviewTask.status == "open").order_by(ReviewTask.priority)))
        out["reviews_opened"] = [{"kind": t.kind, "role": t.required_role, "question": t.question} for t in tasks]
        # transcripts: a reviewer reads the evidence sample and rejects unusable machine output
        out["transcript_decisions"] = []
        for t in [t for t in tasks if t.kind == "transcript_review"]:
            sample = [x["text"][:100] for x in t.evidence.get("sample", [])]
            hitl.decide(s, ANNA, t.id, "reject", "machine transcript unintelligible; keep original until a "
                                                 "platform or Whisper transcript exists")
            out["transcript_decisions"].append({"recording": display_name(s, t.subject_id), "sample": sample,
                                                "decision": "reject"})
        # personal data: the DPO keeps it out of AI datasets
        out["pii_decisions"] = []
        for t in [t for t in tasks if t.kind == "pii_review"]:
            hitl.decide(s, MARIA, t.id, "redact_for_ai_use", "names, contact data and author metadata")
            out["pii_decisions"].append({"content": display_name(s, t.subject_id), "found": t.evidence["counts"]})

    # 2 -- search before any change
    with db.session_scope() as s:
        out["search"] = {}
        for q in ("ahead of time compilation native image", "Wi-Fi is unlicensed", "Connect to Learn tech concept",
                  "SD-WAN orchestration"):
            res = search(s, OWNER, q, 3)["results"]
            out["search"][q] = [{"type": r["type"], "where": r.get("document") or r.get("recording"),
                                 "quality": r.get("transcript_quality"), "t": r.get("t") or r.get("start_s"),
                                 "text": r["text"][:120]} for r in res]

    # 3 -- governed deletion of the duplicate brief image
    with db.session_scope() as s:
        p = planner.create(s, OWNER, "duplicates", title="Duplicate brief image")
        ex.submit(s, OWNER, p.id)
        ex.approve(s, ANNA, p.id, "same bytes as Brief.png")
        ex.execute(s, ERIK, p.id)
        pid = p.id
    Worker(lease_s=600).drain()
    with db.session_scope() as s:
        p = s.get(DeletionPlan, pid)
        src = s.get(Source, "ericsson-drive")
        root = Path(src.config["root"])
        out["duplicates_plan"] = {"status": p.status, "items": [
            {"victim": s.get(File, it.file_id).path, "state": it.state,
             "survivor": s.get(File, it.survivor_file_id).path if it.survivor_file_id else None,
             "stub_exists": bool(it.stub_path and (root / it.stub_path).exists())} for it in p.items]}

    # 4 -- what-if: compact released revision B under a head-only policy (rolled back)
    base_policy = get_policy().raw
    override_policy(revisions={"keep": "head_only", "keep_last_n_unreleased": 0})
    with db.session_scope() as s:
        sp = s.begin_nested()
        p = planner.create(s, OWNER, "revisions", title="what-if head_only")
        out["whatif_revisions"] = [{"victim": r["victim"]["path"], "size": r["victim"]["size_bytes"],
                                    "deletable": r["deletable"], "bytes_reclaimable": r["bytes_reclaimable"],
                                    "proof": next((c["detail"] for c in r["checks"] if c["name"] == "recovery_path_verified"), None),
                                    "lineage": next((c["detail"] for c in r["checks"] if c["name"] == "lineage_consistent"), None)}
                                   for r in p.evidence["items"]]
        sp.rollback()
    override_policy(revisions=base_policy["revisions"])

    # 5 -- recordings: blocked by the human decision, with the measured capsule numbers
    with db.session_scope() as s:
        p = planner.create(s, OWNER, "recordings", title="recordings")
        out["recordings_plan"] = [{"recording": r["victim"]["path"], "deletable": r["deletable"],
                                   "blocked_by": [c["name"] + ": " + c["detail"] for c in r["checks"]
                                                  if c["hard"] and not c["passed"]]} for r in p.evidence["items"]]
        ex.cancel(s, OWNER, p.id)
        out["recording_capsules"] = []
        for c in s.scalars(select(Content).where(Content.detected_type == "video")):
            cap = s.get(Capsule, c.sha256)
            arts = cap.manifest["artifacts"]
            audio = sum(a.get("size", 0) for a in arts if a["role"] == "audio")
            frames = [a for a in arts if a["role"] == "keyframe"]
            out["recording_capsules"].append({
                "recording": display_name(s, c.sha256), "minutes": round(c.duration_s / 60, 1),
                "original_bytes": c.size_bytes, "capsule_bytes": cap.size_bytes, "audio_bytes": audio,
                "keyframes": len(frames), "keyframes_with_ocr_text": sum(1 for a in frames if a.get("ocr_chars")),
                "reduction": round(1 - cap.size_bytes / c.size_bytes, 4),
                "reduction_without_audio": round(1 - (cap.size_bytes - audio) / c.size_bytes, 4)})

    # 6 -- AI dataset: opt-in source, redacted, pseudonymised
    with db.session_scope() as s:
        src = s.get(Source, "ericsson-drive")
        src.config = {**src.config, "ai_use_allowed": True}
    exp = get_settings().data_dir / "exports"
    exp.mkdir(exist_ok=True)
    with db.session_scope() as s:
        res = ai_export.export(s, OWNER, exp / "knowledge.jsonl")
        names = hitl.known_names(s)
    recs = [json.loads(l) for l in (exp / "knowledge.jsonl").read_text().splitlines()]
    leaks = [f.kind for r in recs for f in pii.find(r["text"], names)]
    out["ai_export"] = {**res, "pii_left_in_export": leaks,
                        "chat_sample": [r["text"] for r in recs if r["kind"] == "message"][:4]}

    # 7 -- GDPR: access request, then erasure with four eyes
    with db.session_scope() as s:
        acc = dsar.export(s, MARIA, "subject")
        out["dsar_access"] = {"spoken_or_written": len(acc["spoken_segments"]), "mentions": len(acc["mentions"]),
                              "examples": [x["text"][:80] for x in acc["spoken_segments"][:3]]}
        t = dsar.request_erasure(s, MARIA, "subject", "data subject asked to be removed from KORA's derived data")
        try:
            hitl.decide(s, MARIA, t.id, "approve_erasure")
            out["four_eyes"] = "BROKEN: requester approved own request"
        except PermissionError as e:
            out["four_eyes"] = f"refused: {e}"
        hitl.decide(s, LARS, t.id, "approve_erasure", "verified identity and scope")
        left = [u.text[:60] for u in s.scalars(select(Unit)) if dsar.name_pattern(subject).search(u.text or "")]
        hits = search(s, OWNER, subject, 10)["results"]
        out["dsar_erasure"] = {"remaining_mentions_in_derived_data": left,
                               "search_hits_naming_him": [h["text"][:60] for h in hits
                                                          if dsar.name_pattern(subject).search(h["text"])],
                               "pseudonym": dsar.pseudonym("subject")}
        v = audit.verify(s)
        names_in_audit = [f.filename for f in s.scalars(select(File))
                          if f.filename in json.dumps([e.details for e in s.scalars(select(AuditEvent))])]
        out["audit"] = {"events": v.events, "ok": v.ok, "file_names_in_log": names_in_audit}
    rd = get_settings().data_dir / "reports"
    rd.mkdir(exist_ok=True)
    (rd / "real_scenario.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
