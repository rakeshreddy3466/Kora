# Strict self-evaluation against the judging criteria

Scored the way a sceptical jury would: only what is **shown, run or tested**
counts, and claims count for nothing. Scale 0–5; points = score ÷ 5 × weight.

## Scores (round 6: offline Whisper with speaker turns, recordings page, slide-to-text, scenarios)

| Criterion (weight) | R1 | R2 | R3 | R4 | R5 | **Now** | Evidence | What still caps the score |
|---|---|---|---|---|---|---|---|---|
| Problem understanding (20) | 3 | 3 | 4 | 4 | 4 | **4** | real causes observed in the partner's files: file names contradict document control (`final` = Rev C, `final2` = Rev B); hidden slides; recordings without transcripts; **recordings stored at 30 fps / 1.8 Mbps for static slides**; 300-dpi photos in slide PDFs; personal data in chat signatures and author metadata | no quotes from Ericsson users (ask the 5 questions in PITCH.md) |
| Feasibility (15) | 2 | 3 | 4 | 4 | 4 | **4** | rendering, throughput (16-min 1080p talk transcoded **and** measured in 4.4 min on 2 CPUs), PostgreSQL + crash recovery, migrations, lease keep-alive for long jobs, Claude Code playbook for a local install; **Docker stack (PostgreSQL, web, 2 workers, executor) built and run: folder added in the website, Whisper in a container, a plan executed by the executor container** | Graph connector not built; Docker base images were local stand-ins here |
| Technical quality (15) | 4 | 4 | 4 | 5 | 5 | **5** | 84 tests incl. tamper-mid-flight for in-place replacement; chaos test on real data; measured quality budgets (SSIM, OCR readability, per-page text identity, windowed SSIM with per-page fallback); 16 real-data defects found, fixed and regression-tested | Whisper path implemented but not exercised on these recordings |
| User value (20) | 3 | 3 | 3 | 4 | 4 | **4** | **266 MB of 329 MB (80.8 %) freed on the partner's files, executed through a governed plan; files stay in place, open normally, and one was restored byte-identical**; plus revision order corrected, duplicate removed, chat/OCR searchable, PII kept out of AI data | no user feedback yet |
| Innovation (10) | 4 | 4 | 4 | 5 | 5 | **5** | proof-carrying deletion *and* compression: a rendition only replaces a file with a measured budget attached, including **"can OCR still read the slides?"** as an acceptance test; net-saving guard; footer-insensitive slide identity; transcript inheritance with provenance | – |
| Presentation & demo (10) | 2 | 3 | 3 | 3 | 4 | **4** | interactive dashboard on the partner's files: storage bar, before/after slider, persona switching that shows separation of duties live, manual deletion with impact, leaver assessment; reproducible scripts behind every number | not rehearsed; no recording yet |
| Partner relevance (10) | 4 | 4 | 4 | 4 | 4 | **5** | runs on the partner's own files; every brief item mapped and shown (USE_CASES.md); their recordings now give usable text with speaker turns offline, slides become text that recreates them, and the recording can be recreated as video | – |
| **Total** | **64** | **68** | **74** | **83** | **85** | **87** | | |

## From 87 to 100: what only the team can add

Everything left needs a person, a machine with internet, or a stage. The steps are in
[CLAUDE_CODE_PLAYBOOK.md](CLAUDE_CODE_PLAYBOOK.md).

| Gap | Action | Points |
|---|---|---|
| no user evidence | 15 minutes with the Ericsson mentors: the 5 questions in PITCH.md, quote them | PU 4→5: **+4** |
| user value shown to a user | a mentor looks at the compressed recording and searches the transcript: "is this good enough to delete the original?" | UV 4→5: **+4** |
| deployment not shown on stage | `bash scripts/docker-up.sh`, API docs at :8080, one plan through the API (works here; show it live) | F 4→5: **+3** |
| demo | 3-minute screen recording of the dashboard following PITCH.md, then rehearse it twice | P 4→5: **+2** |
| **Total** | | **100** |

## Changes made in round 6 (checked against every line of the brief: USE_CASES.md)

| Gap against the brief | Change | Evidence |
|---|---|---|
| recordings had no usable text offline (PocketSphinx) | offline Whisper small (int8 ONNX, sherpa-onnx), models from GitHub releases, per-chunk language with re-decoding of stray languages | real talk: 90 passages, correct opening sentence; `test_offline_whisper_with_speaker_turns` |
| "each speaker identified" was only possible with a platform transcript | anonymous speaker turns (pyannote segmentation + embeddings held in memory only), minor-speaker merge; names only by a person | real: 2 speakers in each recording |
| "slide described in a text prompt so it can be recreated" was missing | slide area cut out of the meeting window, layout + text spec, prompt, recreation rendered from text and OCR-checked | real: 91–98 % of words recovered from 1.4–2.5 kB; `test_recordings_capsule_slide_description_and_rebuild` |
| recreating the video was CLI-only | rebuild job + Recordings page with player and subtitles | real: 2.65 MB in 7.8 s |
| the dashboard had no page for the core use case | Recordings page: slide strip, "what was said here" per slide, speakers, describe, recreate | browser-tested on real data |
| scenario savings were a table | scenario calculator on Overview: measured ratios × a chosen storage mix, presets | Team site 1 TB: −74 % (compress) / −82 % (capsules); old meetings 10 TB: −95 % |
| a request held the database's write lock during seconds of OCR | the request's transaction is released before sandbox work | test above |

## Changes made in round 5 (dashboard, manual deletion, AI)

| Gap | Change | Evidence |
|---|---|---|
| no way to show the work except a terminal | `kora dashboard`: overview, files, plans, compression before/after, versions, reviews, search & AI, audit; persona switching | browser-tested on synthetic and real data; `test_ui_is_served_with_a_strict_csp` |
| people want to delete files themselves | `manual` plans: KORA prefers a provable reason (copy, revision), else `MANUAL_DELETE` with impact checks (only copy, who loses access, current revision) and a hard `knowledge_captured` gate; owners may request their own files | `test_manual_delete.py` (4 tests), `test_owner_self_service_over_http` |
| "deleted is lost for AI" was answered only by an export | `/v1/ai/context` (cited, redacted, permission-aware context packs) and `kora mcp` (MCP server for assistants); DPO opt-in per source from the dashboard | `test_ai_context_respects_opt_in_permissions_and_pii`, `test_mcp_server_speaks_the_protocol` |
| approvers had numbers but no picture | before/after previews stored with each rendition (worst PDF page, slide frames) | real-data dashboard |
| a compressed PDF claimed "Rev B" a second time and opened a conflict review | renditions join their original's group in the lineage; the live copy represents it | `test_rendition_lineage_survives_reextraction` |
| a share in a private OneDrive inherited the team site's AI opt-in | opt-in must cover a copy the asker can read | AI context test |

## Changes made in round 4 ("aim for 100": compression, removal, identification)

| Weakness found | Fix | Evidence |
|---|---|---|
| the biggest saving on real data depended on a transcript nobody had | governed compression: renditions measured against a quality budget, `compress` plan, in-place replacement saga, restore, purge | 265.8 MB freed on the partner's files; `test_compress_plan_replaces_in_place_restores_and_purges` |
| a lossy step could silently hurt readability | OCR-readability check for recordings, page-text identity + windowed SSIM per page for PDFs, per-page fallback | `test_judge_rejects_renditions_outside_budget`, `test_pdf_recompression_downsamples_images_and_keeps_text` |
| an edit after approval could be overwritten | compare-and-swap on SHA-256 at the source; blocked, edit kept | `test_compress_blocks_when_source_changes_after_approval` |
| KORA could re-compress its own output | rendition lineage kept through re-extraction; renditions never re-encoded | `test_rendition_lineage_survives_reextraction`, `test_recording_rendition_is_measured_and_never_recompressed` |
| long transcodes/ASR could outlive a job lease | `ctx.keepalive()` renews leases from a side thread; fencing still applies | chaos run killing a worker mid-transcode recovers exactly once |
| the chaos test sometimes killed an idle worker | it now kills a worker that holds a job | 2 runs: interrupted `content.extract`, `content.optimize`; all invariants hold |
| sandbox children could outlive a killed worker | `PDEATHSIG` on Linux; CPU rlimit elsewhere | no orphan ffmpeg after the chaos run |
| native-library warnings corrupted the sandbox reply | private protocol channel | real C2L deck |
| setting up Whisper/Docker/real data is fiddly | `CLAUDE.md` + `docs/CLAUDE_CODE_PLAYBOOK.md`; `kora fetch-models` for offline, sandboxed Whisper | – |

## Fixes made in round 3 (from Ericsson's real files)
See the 11 defects in [REAL_DATA_RESULTS.md](REAL_DATA_RESULTS.md#defects-the-real-data-exposed-all-fixed-and-covered-by-tests). The most important: revision order now comes from document-control custom properties, not file names. Without that fix KORA would have proposed compacting the newer deck.

## Fixes made in round 2

| Weakness found | Fix | Test |
|---|---|---|
| Recordings without a Teams transcript could not be distilled (most older recordings) | offline ASR (PocketSphinx, model bundled, nothing leaves the machine) **and** mandatory human approval of machine transcripts before distillation | `test_machine_transcript_needs_review_before_distillation` |
| Real recordings show slides in a window beside a webcam, so pHash fails | OCR-text fallback for frame → slide linking | `test_frame_links_survive_picture_in_picture_via_ocr` |
| Human oversight existed only for deletion approval | review queue with roles, evidence, blocking semantics and defined effects (near-duplicates, low-confidence order, revision conflicts, ASR transcripts, PII, erasure) | 5 tests in `test_hitl_gdpr.py` |
| GDPR: file names and people's names went into the immutable audit log | log holds ids and hashes only; names live in erasable tables | `test_audit_log_holds_no_file_names` |
| GDPR: no access or erasure handling | DSAR export; erasure with four-eyes DPO approval, voice removal, legal-hold exemption | `test_dsar_export_and_four_eyes_erasure` |
| "Feed AI" goal not safely covered | redacted, pseudonymised, provenance-tagged JSONL export, opt-in per source, head revisions only | `test_ai_export_*` |
| Reliability was asserted, not demonstrated | `scripts/reliability_check.py` (concurrency + hard crash) | 3 runs, all invariants hold |
| Drive delivers folders as zip files | guarded zip extraction (zip-slip, symlinks, size) and the `kora ingest-folder` runner | `test_zip_extraction_is_guarded` |
| Plain-text files (e.g. Ericsson's `Readme_file_input_links.txt`) were neither searchable nor checked for personal data | `text` type: extracted, indexed, PII-scanned (a test copy holding an e-mail and a phone number raised a DPO review) | runner check |
| The savings line reported "100 % freeable" by counting archive moves as deletions | report splits *removable* bytes from bytes *moved to archive* and subtracts KORA's own capsule bytes | runner check |
| Ericsson's internal document numbers (`EAB-24:000123 Uen`) were not recognised | pattern added | unit check |

## Problem understanding: causes vs symptoms

| Symptom (in the brief) | Root cause | KORA's answer |
|---|---|---|
| sites hit the storage maximum, admins delete content | recordings stored only as heavy video; there is no lighter form that keeps the knowledge | capsule (text, keyframes, slide links, optional speech); original goes to the archive tier |
| deleted content is lost for good, and for AI | deletion is binary and unverified | proof-carrying, reversible deletion; capsules stay searchable and AI-ready |
| duplicates across OneDrives | the download-and-re-upload habit; sharing copies instead of links | exact and render-identical copies replaced by a pointer to the canonical copy |
| many revisions, each with small changes | versioning by copy (`_Final_v2`) and doc-control revisions kept in full | lineages, slide diffs, slide-level storage of shared slides |
| leavers' files deleted or kept wholesale | no way to tell what is unique | a departure assessment that deletes only proven duplicates and hands unique knowledge to a named owner |

## Partner relevance: the brief, line by line

| Brief asks | Status |
|---|---|
| text from recordings, with speakers | ✔ VTT/SRT with speaker labels; ASR fallback with human review |
| slide frame: screenshot, compressed, identified by document number + slide number, linked to the single source of truth, recreatable | ✔ keyframes (JPEG); doc number + revision + page; unit hash = slide id; the slide store is the source of truth; video recreated from the capsule |
| text time-linked to the frame ("as you see here") | ✔ each segment points to the frame and slide on screen |
| search returns matching frame and text | ✔ evidence search, ACL-filtered |
| can the recording be recreated? | ✔ slides + speech + subtitles (`kora rebuild-video`) |
| duplicates; per-slide hash; "(1).pdf"; shared slides; minor-change revisions | ✔ all nine classes |
| departure: delete only if a duplicate exists | ✔ enforced strictly (survivor in an approved repository, active owner, permission coverage) |
| scenario % savings; future-proof data strategy; further work | ✔ measured savings report; README sections |
| no sensitive data needs sharing | ✔ fully local processing; GDPR controls |

## Feasibility: next step and risks

**Next step (6 weeks, 2 engineers):** a read-only pilot on one SharePoint site.
Microsoft Graph connector with `Sites.Selected` (one site, read-only),
dry-run plans only, Whisper served from an approved GPU host. Success measures:
freeable GB, the share of plans approved without changes, reviewer minutes per GB.

| Risk | Mitigation |
|---|---|
| IT security approval of a Graph app | read-only scope; the executor is a separate app with write scope added only after the pilot |
| ASR quality on Swedish and English meetings | Whisper (multilingual) on an approved host; every machine transcript is reviewed |
| records and legal acceptance of render-equivalent recovery | policy-as-code: `archive_original=true` keeps byte-exact copies in the archive tier |
| GDPR: transcribing employees | DPIA (technical input in [GDPR.md](GDPR.md)); local processing; opt-in AI use |
| scale (millions of slides) | ANN and BK-tree indexes (scaling path in ARCHITECTURE.md) |
