# GDPR and privacy by design

Meeting recordings and slide decks contain personal data: names, voices, faces,
e-mail addresses, sometimes phone numbers or personnummer. Turning them into
searchable text *increases* how easily that data can be found. KORA is
therefore built so the controller (Ericsson) can meet its obligations by
default. Organisational parts (DPIA sign-off, notices, legal basis) remain
the controller's decision. This document states which is which.

## Processing record (Art. 30, draft)

| Item | Content |
|---|---|
| Purposes | storage optimisation; knowledge management (search); records governance; optional: AI training/RAG datasets |
| Legal basis (to be confirmed by DPO) | legitimate interest (Art. 6(1)(f)) for storage optimisation and search, with a balancing test. **AI training is a separate purpose**: off by default per source (`ai_use_allowed`), requires its own assessment |
| Data subjects | employees, consultants, partners appearing in recordings and documents |
| Categories | identity (names, e-mail), voice, image (keyframes), work content. **No biometric identification**: KORA never stores voiceprints or face templates and never matches a voice to a person. Speaker *names* come only from the meeting platform's own transcript labels. Without a platform transcript, offline speaker turns ("Speaker 1", "Speaker 2") are computed from voice embeddings that exist only in memory during clustering inside the sandbox and are discarded; the labels are anonymous, and attaching a name is a human decision. Set `KORA_DIARIZATION=off` where even transient processing is not wanted (a DPIA decision) |
| Recipients | none outside the deployment. Extraction, OCR, ASR and embeddings run locally. **No external AI API**, no third-country transfer |
| Retention | derived data follows the retention label of the source item. Quarantine: 30 days. Deleted items leave a tombstone **without content** |
| Security (Art. 32) | RBAC, ACL-filtered access, encryption at rest (AES-256-GCM option + bucket SSE), sandboxed parsers, MAC'd audit log, least-privilege workers |

## Controls implemented (and tested)

| Principle / right | Implementation | Test |
|---|---|---|
| Data minimisation (5(1)(c)) | PII detectors (e-mail, phone, Luhn-validated personnummer and samordningsnummer, IBAN, IP, known names); AI export redacts them | `test_pii_detection_is_precise`, `test_ai_export_is_redacted_pseudonymised_and_deduplicated` |
| Purpose limitation (5(1)(b)) | AI export only from sources explicitly flagged `ai_use_allowed`; only head revisions; speakers as pseudonyms | same |
| Storage limitation (5(1)(e)) | deletion plans, grace period, purge, content-free tombstones; duplicate copies of personal files removed on departure | `test_lifecycle_*`, `test_departure_assessment` |
| Confidentiality | search, lineages, capsules and blobs filtered by source ACL; capsule keeps the ACL snapshot after originals are gone | `test_search_is_evidence_based_and_permission_filtered`, `test_acl_filtered_views` |
| Right of access (Art. 15) | `GET /v1/dsar/{person}/export`: owned files, spoken segments, mentions, actions performed, processing description | `test_dsar_export_and_four_eyes_erasure` |
| Right to erasure (Art. 17) | `POST /v1/dsar/{person}/erasure` opens a **DPO review**; the requester cannot approve. On approval: speaker labels and name/e-mail mentions are pseudonymised, the person's **voice audio is removed** from capsules, the search index is rebuilt, orphaned blobs are deleted, and the directory entry is pseudonymised | same |
| Art. 17(3)(e) exemption | content under legal hold is skipped and reported, not silently erased | same (rev A on hold keeps its slide text) |
| Accountability (5(2)) | hash-chained, MAC'd audit log containing **ids and hashes only, never file names or people's names**, so the immutable log never needs erasing | `test_audit_log_holds_no_file_names` |
| Speaker notes | decks carry "Slide responsible: <name>  Best Before: <date>" in their notes. Notes become their own units: the named person is flagged for **DPO review** (`NAME_IN_NOTES`), redacted for AI use, included in the access export (`named_as_slide_responsible`) and pseudonymised on erasure. The raw notes are moved out of slide metadata, so there is exactly one copy. A review decided before names in notes were detected gets a follow-up review: a decision covers only what the reviewer saw | `test_speaker_notes.py` |
| Storage limitation, freshness | a deck past its notes' Best Before date is flagged in search, in AI context packs ("past its best-before date") and on the Files page, and an analyst decides: retire, ask the owner to update, or keep. Nothing is deleted automatically | `test_privacy_review_and_freshness_review` |
| Human oversight | every privacy decision (PII handling, erasure) requires the `dpo` role | `test_*` above |

## Known gaps (stated honestly)

* **Pixels.** Names or faces visible *inside* keyframe images are not redacted. OCR text is pseudonymised, but the JPEG is not. Erasure reports this and it needs manual review. Next step: blur OCR word boxes that match the person.
* **Systems of record.** Erasure covers KORA's derived data. The original file in SharePoint, OneDrive or Play must be handled there; KORA lists it in the access export so the owner can act.
* **Name detection** uses the directory, transcript speaker labels and the people speaker notes name as responsible. Other names of external people are not detected. Access export and erasure are keyed on directory entries, so a slide owner who is not in the directory cannot be looked up by id yet.
* **The DPIA itself** (Art. 35: systematic processing of employee recordings) is the controller's document. This file is the technical input to it.
* **ASR model.** PocketSphinx is English-only. Swedish meetings need Whisper served from an approved, EU-hosted model gateway.
