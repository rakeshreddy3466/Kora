# The brief, item by item: what KORA does and where it is shown

Every row has been run. "Real" means Ericsson's 10 sample files (329 MB); "synthetic" means the
generated test corpus (used where the sample files do not contain the case, e.g. a deck shown in
a recording). Dashboard page in brackets.

## Recordings

| Brief | Status | How | Evidence |
|---|---|---|---|
| Text of a recording, as plain text | ✔ | Teams/Stream transcript (VTT/SRT) when it exists; otherwise offline Whisper (small, int8 ONNX via sherpa-onnx) in the sandbox, language detected per 30 s chunk, stray-language chunks re-decoded in the main language. A person approves a machine transcript before it may stand in for the video | Real: GraalVM talk 16 min → 90 passages in about 8 min on 2 CPUs ("Hello, and welcome to our thesis presentation…"); jury recording → 42 passages [Recordings] |
| …with each speaker identified | ✔ (anonymous) | Platform transcripts give names. Offline: speaker turns from pyannote segmentation and speaker embeddings that exist only in memory, giving "Speaker 1, 2…". Short split-off voices are merged. Names are attached by a person, never guessed from a voice (GDPR) | Real: talk has 2 presenters, so 2 speakers; jury: host and presenter, 2 speakers [Recordings] |
| Frame of a slide: a screenshot | ✔ | keyframe on every slide change (1 fps scene check) | Real: 17 slide pictures for the talk [Recordings] |
| …compressed | ✔ | JPEG keyframes; the talk's 17 pictures are 1.2 MB in total | Real |
| …identified by document number and slide number | ✔ | frame → slide by perceptual hash, OCR-text fallback for picture-in-picture | Synthetic: frames show `1/1551-LZA 701 0001` rev A, slide 3. Real: the decks shown in the two videos are not in the sample, so correctly **no** link is made |
| …linked to a single source of truth | ✔ | the link names the lineage, revision and whether it is current; the slide store holds each unique slide once | Synthetic [Recordings, Versions] |
| …described in a text prompt so it can be recreated | ✔ | the slide area is cut out of the meeting window, OCR'd with layout; spec (text blocks, positions, sizes, colours, picture regions) and a plain-language prompt; recreated from the text alone and OCR-checked | Real: text slides 1.4–2.5 kB of text instead of a 105–135 kB picture, 91–98 % of the words recovered; chart-heavy slides recover far less, which the page shows [Recordings → Describe this slide] |
| Event compressed into ASCII text, an image, a slide id or a voice sample | ✔ | the knowledge capsule: transcript (JSON + VTT), keyframes (JPEG), unit hashes (slide ids), speech track (Opus 16 kbps), signed manifest | Real: talk 220.5 MB → **2.93 MB capsule (98.7 %)**, 1.22 MB without the voice (99.4 %); jury 88.7 MB → 0.76 MB (99.1 %) |
| Text time-linked to the frame ("as you see here") | ✔ | every passage carries the frame on screen and that frame's slide | Real and synthetic [Recordings: "What was said here"] |
| A search returns the matching frame and text | ✔ | BM25 + embedding rerank over slides, frames and passages, ACL-filtered, with doc number, revision and time | [Search & AI] |
| Can the recording be recreated as a video? | ✔ | slide pictures + speech + transcript as subtitles, nothing from the original file | Real: the talk recreated as **2.65 MB in 7.8 s** [Recordings → Recreate as video] |

## Documents

| Brief | Status | How | Evidence |
|---|---|---|---|
| Identify and handle duplicated data | ✔ | nine duplicate classes (exact copy, re-saved, superseded revision, excerpt, other encoding, recording → capsule, compressed, partial overlap, near-duplicate) plus "chosen by a person"; only proven classes can be removed, through plans with approvals, quarantine, restore | Real: `Copy_of_Brief.png` removed with a pointer stub; synthetic: every class [Files, Plans] |
| PDFs and PowerPoints broken into slides, each with a hash | ✔ | per page/slide: exact render hash and footer-insensitive body hash; document number, revision and file name as metadata (OOXML custom properties outrank names) | Real: SD-WAN decks 25 slides each incl. 3 hidden slides [Versions] |
| Presentations with several revisions, small changes | ✔ | lineages ordered by document control with confidence, slide diffs, unique slides kept once, older revision rebuilt and re-rendered as proof before removal | Real: `final` = Rev C, `final2` = Rev B (file names lie); Rev B compactable, freeing 79 % with a 25/25-page proof |
| "(1).pdf" download copies | ✔ | exact copy, survivor chosen by system precedence, owner and retention | Synthetic [Files] |
| Presentations sharing some slides | ✔ | shared slides found and stored once; never deleted | Synthetic [Files: "shares slides"] |

## The challenge

| Brief | Status | Evidence |
|---|---|---|
| Storage reduction | ✔ | Real, executed: **266 MB of 329 MB (80.8 %)** freed by compression and duplicate removal, files still in place. With reviewed transcripts the recordings can become capsules: about **97 %** of hot storage |
| Data activation: searchable and consumable by AI | ✔ | search; AI context packs with citations; MCP server for assistants; JSONL dataset export; opt-in per source by a DPO [Search & AI] |
| Distilling, compressing, duplicates across systems | ✔ | capsules; quality-budgeted compression; SharePoint, OneDrive and Play sources (filesystem connector; Graph is further work) |
| Leaver: analyse, compare, delete only if a duplicate exists | ✔ | departure assessment: deletes only when a copy survives in an approved repository with an active owner and nobody loses access; unique files handed over; open action items listed [Plans → Someone is leaving] |

## The pitch's five questions

| Question | Where |
|---|---|
| 1. Use cases addressed | this file; the dashboard |
| 2. How it was solved technically | [ARCHITECTURE.md](ARCHITECTURE.md), [DEDUPLICATION_AND_DELETION.md](DEDUPLICATION_AND_DELETION.md), [VERSIONING.md](VERSIONING.md) |
| 3. Scenario-based savings | measured on real files above; scenario calculator on the Overview page (measured ratios × a storage mix); [REAL_DATA_RESULTS.md](REAL_DATA_RESULTS.md) |
| 4. Future-proof data strategy for AI | [DATA_STRATEGY.md](DATA_STRATEGY.md), [AI_READINESS.md](AI_READINESS.md) |
| 5. Further work, unresolved problems | README "Limitations and further work", [EVALUATION.md](EVALUATION.md) |

## What remains open (said plainly)

* **Names for speakers** without a platform transcript stay anonymous until a person names them. That is deliberate (no voiceprints), but it is a manual step.
* **Real Microsoft Graph and Ericsson Play connectors** are not built; the filesystem connector simulates their metadata (owner, ACL, retention, legal hold).
* **Transcript accuracy**: Whisper small mishears names and terms ("GROAL VM" for GraalVM). A domain word list or a larger model on a GPU host would fix most of it.
* **Chart-heavy slides** are not recreatable from text; the description says so with its measured recall.
* **Faces and names inside keyframe pixels** are not blurred.
