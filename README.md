# KORA: Knowledge Optimization, Recovery & Assurance

*Ericsson challenge, Campor × BUILD Hackathon 2026: Data Storage Optimization*

KORA frees storage without losing information. It turns recordings and decks into
compact, searchable, AI-ready **Knowledge Recovery Capsules**, understands **versions**,
classifies **nine kinds of duplication**, **compresses survivors within a measured
quality budget**, and deletes or replaces only through a **governed,
evidence-carrying, reversible** process. Every deletion or replacement comes with proof that the
information is still recoverable, and the hash-chained audit trail shows it happened.

**On Ericsson's own 10 files: 266 MB of 329 MB (80.8 %) freed, executed through a governed plan,
every file still in place, openable and restorable.**

> Typical approach: compress and delete.  KORA: **compact and prove.**

**Team KORA, Group 15:** Abhilash, Jagadish, Rakesh, Nandu, Yashwitha

## For the jury: where to look

| | |
|---|---|
| **Full technical documentation (PDF)** | [docs/KORA_Technical_Documentation.pdf](docs/KORA_Technical_Documentation.pdf): architecture, key decisions, risks and mitigations, measured accuracy, limitations |
| **Run it on a laptop** | `pip install -e ".[dev]"`, then `kora dashboard`. Step by step, including VS Code: [START_HERE.md](START_HERE.md) |
| **Run it as a service** | `bash scripts/docker-up.sh` (PostgreSQL, web app, 2 workers, separate deletion executor) |
| **Results on Ericsson's files** | [docs/REAL_DATA_RESULTS.md](docs/REAL_DATA_RESULTS.md) |
| **How duplicates are found and deleted safely** | [docs/DEDUPLICATION_AND_DELETION.md](docs/DEDUPLICATION_AND_DELETION.md), [docs/VERSIONING.md](docs/VERSIONING.md) |
| **Architecture** | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **People in the loop, GDPR, AI** | [docs/HUMAN_IN_THE_LOOP.md](docs/HUMAN_IN_THE_LOOP.md), [docs/GDPR.md](docs/GDPR.md), [docs/AI_READINESS.md](docs/AI_READINESS.md), [docs/DATA_STRATEGY.md](docs/DATA_STRATEGY.md) |
| **Use cases, evaluation, limitations** | [docs/USE_CASES.md](docs/USE_CASES.md), [docs/EVALUATION.md](docs/EVALUATION.md) |
| **Tests** | `pytest -q` (84 tests) |

Ericsson's sample files are **not** in this repository: they contain personal data and stay on our machines.
The `samples/` folder is a synthetic Ericsson-style corpus, so everything here runs without them.

## Dashboard

```bash
kora dashboard            # API + worker + dashboard on http://127.0.0.1:8080, opens the browser
```

One page per job, all running against the same API and permissions:

* **Recordings:** each recording as text and pictures: a strip of slide pictures, "what was said here" for each slide with speaker turns, the linked document and revision. *Describe this slide as text* produces a prompt that recreates the slide (with its measured word recall). *Recreate as video* rebuilds the recording from the capsule alone.
* **Overview:** one bar showing the whole store: freed already, removable, saved by compression, to archive, and what stays. Below it: what waits for a person, what KORA found, every plan it can prove now, and a **scenario calculator** (ratios measured on your files × a storage mix you choose).
* **Files:** every file with what KORA found (copies, current or superseded revision, excerpt, near-duplicate, compressible, legal hold). **Select files and choose *Review deletion*** to delete manually. KORA first looks for a reason a machine could prove (another copy, a newer revision), shows the impact (only copy, who loses access, current revision), keeps the knowledge searchable, and sends it to two approvers. File owners can do this for their own files.
* **Plans:** drafts, approval, execution, quarantine and restore, with every check's evidence per item. The creator can never approve. Includes the **leaver assessment**: what may go, what is handed over, and the open action items.
* **Compression:** a before/after slider per recording and PDF with the measured quality budget.
* **Versions, Reviews, Search & AI, Audit:** revision chains, the human review queue, cited search and the exact context an AI assistant would receive, and the verified audit chain.

The sidebar switches between demo identities (analyst, two approvers, operator, two DPOs, file owners), so separation of duties can be shown live. The server binds to localhost, the page loads nothing from the internet, and a strict CSP is set. Demo keys travel in the URL fragment, which is never sent to the server.

## Quick start

```bash
pip install -e ".[dev]"                     # Python 3.11+, plus ffmpeg and (optional) tesseract
python scripts/make_samples.py samples      # synthetic Ericsson-style corpus (20 MB, 25 files)
KORA_DATA_DIR=.kora kora demo --samples samples --fresh
open .kora/report.html                       # measured results; .kora/rebuilt_from_capsule.mp4
pytest -q                                    # 84 tests: pipeline, versioning, guards, compression, manual deletion, HITL, GDPR, API, MCP
```

Run it on real files (a folder, or the zip Google Drive produces):

```bash
kora ingest-folder ~/Downloads/Brief.zip --name ericsson --workers 3      # prints and saves a measured report
kora reviews list                                                        # what a human must decide
kora optimize                                                            # measure renditions against the quality budget
kora plan create compress                                                # replace survivors in place (2 approvals)
python scripts/reliability_check.py samples --workers 3                  # chaos test: concurrent workers + SIGKILL
```

Run it as a service:

```bash
kora init --samples samples
kora key carol --roles analyst,viewer        # prints a key once; only its hash is stored
kora worker &                                # or: bash scripts/docker-up.sh (Postgres + web + workers + executor)
kora serve                                   # http://127.0.0.1:8080/docs
```

## Results on Ericsson's own sample files

10 files / 329 MB (two recordings, two revisions of an Ericsson deck, PDFs, the brief, a Teams chat, a duplicated image): **[docs/REAL_DATA_RESULTS.md](docs/REAL_DATA_RESULTS.md)**.

* **Compression, executed:** GraalVM talk 220.5 → 15.2 MB (SSIM 0.999, OCR still reads 93 % of slide words), jury recording 88.7 → 32.0 MB, PDFs −30 % and −33 % with identical text on every page. 265.8 MB freed through a 2-approver plan; one file restored byte-identical to prove the way back.

* **Processing and reliability:** processed in 361 s on 2 CPUs with 0 failures. Crash test on this data: a worker killed mid-video, the work recovered exactly once.
* **File names lie:** `final` is Rev C and `final2` is Rev B. KORA orders by document control.
* **Recordings:** 220.5 MB → 2.9 MB and 88.7 MB → 0.76 MB capsules. The offline transcript was unusable, so a human rejected it and **KORA refused to archive**.
* **Duplicates:** the duplicate image was removed with a pointer stub.
* **Compaction what-if:** Rev B frees 79 % with a 25/25-page recovery proof.
* **GDPR:** personal data was kept out of the AI export (0 left), and the erasure was complete across accent variants.
* **Defects:** 16 real-data defects were found and fixed.

### Running on your own machine (e.g. for Whisper transcripts)

Easiest: open the folder in **Claude Code** and follow [docs/CLAUDE_CODE_PLAYBOOK.md](docs/CLAUDE_CODE_PLAYBOOK.md);
`CLAUDE.md` gives it the project rules (data stays local, plans run on a copy, people make the review decisions). By hand:

```bash
brew install ffmpeg tesseract && brew install --cask libreoffice     # macOS
pip install -e ".[dev,whisper]" && kora fetch-models                # Whisper + speaker turns (GitHub releases), offline afterwards
export KORA_DATA_DIR=~/kora-run KORA_PPTX_RENDERER=libreoffice KORA_MEDIA_TIMEOUT_S=7200
kora ingest-folder ~/Downloads/drive-download-…-001 --name ericsson-drive --workers 2
kora reviews list            # approve or reject the Whisper transcripts
python scripts/real_data_scenario.py
```

## What the demo shows (measured on the synthetic corpus)

| | Result |
|---|---|
| Ingestion | 23 files from SharePoint, OneDrive and Ericsson Play; 142 idempotent jobs, about 32 s on SQLite and PostgreSQL alike |
| Versions | `1/1551-LZA 701 0001` ordered **PA1 → PA2 → A → B** (confidence high) with slide diffs; a re-saved A is recognised as a *variant*; a 3-page excerpt is a *subset*, not a fake revision; `Team_Update → _Final → _Final_v2` ordered from file names (confidence medium) |
| Relationship graph | exact copies, container variant, contained subsets, partial overlaps, near-duplicates, media variant, knowledge overlaps, revision edges, and "recording presents doc X rev A" |
| Recording → capsule | 19.5 MB → **410 KB** (97.9 %, or 98.6 % without the speech track): 9 keyframes each linked to its slide, a speaker-attributed transcript, decisions and action items with timestamps |
| Challenge scenario, 2 h / ~1 GB | capsule **2.1 MB (99.80 %)**, or 13.4 MB (98.70 %) keeping 16 kbps speech, from measured per-minute costs |
| Recreate the video? | yes: slides + speech + subtitles, 86 s, 720p, **282 KB** (`kora rebuild-video`) |
| Search | "as you see here, the new edge data centre…" returns Alice at 17 s, with slide 3 "Current topology" of `1/1551-LZA 701 0001` rev A on screen. An external partner only ever sees the one file shared with him |
| Departure (Bob) | 13 files → 4 deletable duplicates, 1 on legal hold, 1 blocked because deleting it would revoke a partner's access, 1 near-duplicate for review, 1 unique doc + 1 unique recording assigned to his manager, 2 open action items surfaced |
| Governance | dry-run evidence → approvals (never by the creator) → quarantine with pointer stubs → restore → 30-day grace → re-verified purge with tombstones; 167 audit events, chain valid |
| Refusals (by design) | legal hold; permission gap; file edited after approval; survivor deleted before execution; two plans that would delete each other's survivor; recording without transcript; revision whose rebuild proof fails; lineage with a revision conflict |

## Human in the loop and GDPR

KORA automates only what it can prove. Low-confidence version orders, revision
conflicts, near-duplicates, machine transcripts and personal-data findings become
review tasks for a named role, and the dependent step waits
([`docs/HUMAN_IN_THE_LOOP.md`](docs/HUMAN_IN_THE_LOOP.md)). Privacy by design
([`docs/GDPR.md`](docs/GDPR.md)):

* all processing is local, with no external AI API and no biometrics
* PII detection, including Luhn-validated personnummer, with redaction for AI exports
* access export and erasure (Art. 15/17) with four-eyes DPO approval, voice removal and the legal-hold exemption
* an audit log that holds ids only, so it never needs erasing

## Reliability, demonstrated

`scripts/reliability_check.py` on PostgreSQL and SQLite with 3 concurrent workers; the script waits until a worker is *holding a job* and SIGKILLs it (runs interrupted `content.extract` and, after compression was added, a `content.optimize` transcode). Sandboxed children die with their worker (Linux `PDEATHSIG`; elsewhere bounded by the CPU rlimit). Every run checks that:

* all jobs succeed
* each content is extracted exactly once
* the crashed job is re-claimed and completed
* every lineage has one head
* there are no duplicate relations
* the audit chain is valid
* a rescan creates no new work

All three runs passed.

## Documentation

* [`docs/DEDUPLICATION_AND_DELETION.md`](docs/DEDUPLICATION_AND_DELETION.md): the nine duplicate classes, how each is detected, what may be deleted and how, survivor selection, every check, the lifecycle, invariants, and the departure workflow
* [`docs/VERSIONING.md`](docs/VERSIONING.md): slide identity (exact vs footer-insensitive), lineages, Ericsson revision ordering with confidence, variants, conflicts, excerpts, slide diffs, slide store and recovery proofs
* [`docs/REAL_DATA_RESULTS.md`](docs/REAL_DATA_RESULTS.md): everything measured on Ericsson's files, including the defects they exposed
* [`docs/EVALUATION.md`](docs/EVALUATION.md): strict scoring against the seven judging criteria, fixes made, remaining gaps
* [`docs/PITCH.md`](docs/PITCH.md): 5-minute script, built vs reused, likely jury questions
* [`docs/CLAUDE_CODE_PLAYBOOK.md`](docs/CLAUDE_CODE_PLAYBOOK.md): run everything on your own Mac with Claude Code (install, Whisper, real data on a copy, Docker, demo)
* [`docs/USE_CASES.md`](docs/USE_CASES.md): **every line of the brief, what KORA does, and where it is shown**
* [`docs/DATA_STRATEGY.md`](docs/DATA_STRATEGY.md): the future-proof data strategy in one page
* [`docs/AI_READINESS.md`](docs/AI_READINESS.md): how KORA's output feeds AI (context packs, MCP server, dataset export) and the GDPR rules around it
* [`docs/GDPR.md`](docs/GDPR.md), [`docs/HUMAN_IN_THE_LOOP.md`](docs/HUMAN_IN_THE_LOOP.md)
* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components, reliability (transactional outbox queue, leases with fencing, sagas), security (RBAC, separation of duties, ACL-aware search, sandboxed parsers, audit chain), scaling path

## Pitch checklist (what the challenge asks for)

1. **Use cases addressed.** Recording → text + speaker attribution + keyframes; keyframe identified by document number and slide number and linked to the single source of truth; slides hashed for instant comparison; duplicates across SharePoint, OneDrive and Play; revisions with minor changes; employee departure; search returning frame + text; recreating the recording.
2. **How it was solved technically.** See the architecture doc. Main points: content-addressed extraction, two-level slide identity, an indexed relationship graph (no O(n²)), document-control-aware lineages, signed capsules, policy-as-code, proof-carrying deletion, and compression that is only applied with a measured quality budget (SSIM, OCR readability, per-page text identity) through the same governed, reversible plan.
3. **Scenario-based savings** are measured, not assumed: `GET /v1/reports/savings` dry-runs every planner in a rolled-back transaction and subtracts KORA's own overhead (capsules, slide store).
4. **Future-proof data strategy.** Systems of record stay authoritative. Capsules are a durable, model-agnostic knowledge layer: provenance per fact, ACL snapshot, signed manifests, open formats (JSON, JPEG, Opus, PDF pages). Tiered storage (hot/archive/quarantine) with lifecycle rules. Every derived fact traces back to source, page, slide or timestamp, so it can be used for RAG and training without losing lineage or permissions.
5. **Further work and unresolved problems.** Listed below.

## Limitations and further work (honest list)

* **Connectors.** A filesystem connector simulates SharePoint/OneDrive/Play libraries, including owner, ACL, retention label and legal hold. Production needs Microsoft Graph (delta queries, `quickXorHash`, `If-Match` eTags) and the Ericsson Play API. The connector interface already separates read-only from conditional write access.
* **Transcription.** Uses Teams/Stream VTT/SRT sidecars first. Otherwise offline Whisper small (int8 ONNX) with anonymous speaker turns; PocketSphinx only as a last resort. On Ericsson's recordings the text is good but names and terms are misheard ("GROAL VM"), so a person approves every machine transcript. Speaker names are never inferred from voices.
* **Frame ↔ slide matching** assumes a full-screen share. Picture-in-picture or cropped layouts need slide-region detection (feature matching / homography).
* **PPTX.** Slides are identified structurally. Render-equivalent recovery of PPTX needs `KORA_PPTX_RENDERER=libreoffice`; without it, PPTX revisions are (correctly) not compactable.
* **Embeddings** are an offline feature-hashing model, so nothing leaves the machine. Swap in an approved model behind `fingerprint/embed.py` for better semantic overlap.
* **Facts** (decisions, action items) come from deterministic patterns with provenance. An LLM extractor can be added but must emit the same provenance fields.
* **Scale.** pHash and embedding scans are vectorised brute force, fine to about 10⁵ items. Beyond that, use BK-trees/multi-index hashing and an ANN index (pgvector HNSW / OpenSearch).
* **Compression budgets** are global per type. Per-site budgets (e.g. stricter for legal recordings) are a policy-file change, not code. Audio is re-encoded mono 64 kbps: fine for speech, not for music or multi-speaker stereo.
* **Docker:** the full stack (PostgreSQL, web, 2 workers, executor) was built and run with `scripts/docker-up.sh`; a folder was added through the website and processed in the containers, Whisper included. Base images came from a local stand-in (Docker Hub is not reachable from the build sandbox), so the first real `docker pull` happens on your machine. The S3 adapter was tested against `moto`.
* **ACLs** are modelled as allow-lists of users and groups. SharePoint permission inheritance breaks and deny entries are not modelled.

## Layout

```
kora/            application package (see docs/ARCHITECTURE.md for the module map)
kora/policies/   default.json, the versioned governance policy (incl. compression budgets)
kora/ui/         the dashboard (plain HTML/CSS/JS, no build step, no external requests)
kora/ai/         AI context packs and the MCP server
CLAUDE.md        project rules for Claude Code
scripts/         make_samples.py (synthetic corpus), real_data_scenario.py, real_compression.py, reliability_check.py
tests/           84 tests (units, platform, pipeline/versioning, governance, compression, manual deletion, HITL + GDPR, API, dashboard, MCP)
docs/            design documents
Dockerfile, docker-compose.yml, .env.example, scripts/docker-up.sh
```
