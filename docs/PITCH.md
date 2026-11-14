# Pitch and demo plan (5 minutes + Q&A)

## Before the jury: 15 minutes with the Ericsson mentors (evidence)
Ask, and write the answers down. Quote them in the pitch.
1. When a site hits its quota, who decides what to delete, and how long does it take?
2. Roughly what share of storage is recordings vs decks vs other files?
3. For superseded drafts, would a page-identical rebuild be acceptable, or must the original file be kept?
4. Are Teams transcripts usually switched on? English, Swedish, or mixed?
5. What happens to a leaver's OneDrive today?

## Script

| Time | Say | Show |
|---|---|---|
| 0:00–0:40 | Problem: sites hit the limit and admins delete. Deleted means lost, for people and for AI. The root causes are video as the only form of knowledge, copy-based versioning and download/re-upload duplicates (quote a mentor). | one slide |
| 0:40–1:20 | Idea: compact and prove. Every deletion **and every compression** carries proof that the knowledge is still recoverable, and a human approves anything uncertain. Headline: **on Ericsson's own 10 files, 266 of 329 MB (81 %) freed, every file still in place and openable.** | architecture diagram + savings table |
| 1:20–3:30 | **Live in the dashboard, on Ericsson's own files** (`kora dashboard`). **(0) Recordings:** open the GraalVM talk. The 220 MB video is now a 2.9 MB capsule: 17 slide pictures and 90 passages from 2 speakers. Click a slide and "what was said here" appears. Click *Describe this slide as text*: 1.5 kB of text recreates it, and OCR finds 91–98 % of its words. Click *Recreate as video*: 2.65 MB, built in 8 s. **(1) Overview:** the bar shows 329 MB, of which 266 MB can go, each with proof. **(2) Compression:** drag the slider on the 220 MB talk (now 15 MB) and read the budget line: "OCR still reads 93 % of the slide words, SSIM 0.999". Click *Plan compression*. **(3) Separation of duties:** as Abhilash, submit. Switch to Jagadish and approve, then to Rakesh and approve. Switch to Nandu and execute. The status goes to *In quarantine*. Restore one item and say: "byte-identical". **(4) Manual delete:** as the file owner, tick three files and click *Review deletion*. KORA explains each one: one is a copy (it keeps the team's original), one is on legal hold (blocked even though a person asked), one is unique ("only copy: its knowledge stays searchable"). **(5) Versions:** "the file called *final* is Rev C, *final2* is Rev B: file names lie, KORA reads document control". **(6) Search & AI:** a question returns the slide frame and revision; tick *Show what an AI assistant would receive* to show the cited, redacted context pack. | dashboard |
| 3:30–4:00 | Savings by scenario: on the Overview, click *Team site, 1 TB* (−74 % keeping every video, −82 % with capsules) and *Old meetings, 10 TB* (−95 %). The ratios are measured on Ericsson's own files; only the mix is an assumption. Why it is safe to trust: the first offline engine produced garbage, the reviewer rejected it, and KORA *refused* to archive. A machine transcript still needs a person's approval. Chaos test: a worker killed mid-transcode recovered exactly once. | Overview scenarios, Reviews |
| 4:00–4:30 | GDPR and AI: processing stays local. A DPO opts each source in for AI use. Names are redacted and speakers pseudonymised, erasure needs four eyes, and the audit log holds no names. Claude can use KORA through MCP with the asker's own permissions. | Search & AI page, Audit page |
| 4:30–5:00 | Limitations and next step: Graph pilot on one site, Whisper on an approved host, records approval for render-equivalent recovery. | EVALUATION.md table |

Backup: record the dashboard demo before the final round, in case Wi-Fi or the live run fails. Everything runs offline on the laptop, so no Wi-Fi is needed.

## Built vs reused (say this explicitly)
* **Built this weekend:** all of KORA: pipeline, versioning, dedup taxonomy, governance, capsules, human-in-the-loop, GDPR controls, tests, sample generator. Be open that it was written with an AI coding assistant if asked.
* **Reused (open source):** PyMuPDF, FFmpeg (x264, its SSIM filter), Tesseract, PocketSphinx, FastAPI, SQLAlchemy, NumPy, Pillow, python-pptx, ReportLab (samples only).

## Likely questions, precise answers
* **"What if the hash matches but the file is needed somewhere else?"** The survivor must be readable by every active reader of the copy (permission coverage), and the copy becomes a stub pointing to the survivor. Nothing becomes unreachable.
* **"What if someone edits the file after approval?"** The connector re-hashes at removal and refuses. We test it.
* **"Two admins clean up at the same time?"** Survivors are pinned and victims reserved, so two plans cannot delete each other's survivor. We test it.
* **"How do you know a rebuilt PDF is right?"** Every page is re-rendered and its hash compared with the hash recorded at ingestion. A corrupted page blocks the deletion.
* **"Doesn't compression lose information?"** Yes, by design, within a budget the policy states and KORA measures per file: same duration and speech, SSIM ≥ 0.95, slide text still OCR-readable; for PDFs identical text on every page and per-page SSIM ≥ 0.97. A rendition that misses any line is never offered. The original stays in quarantine for 30 days, and engineering diagrams are never re-encoded.
* **"Why is the recording 93 % smaller?"** It was a 30 fps, 1.8 Mbps screen recording of mostly static slides. 15 fps with a still-image tuned encoder keeps every slide sharp.
* **"Is the 99 % real?"** It is measured per minute on our capsules and extrapolated to the brief's 2 h / 1 GB case. With speech audio kept it is 98.7 %.
* **"GDPR?"** Processing is local; the audit log holds no names; access and erasure are implemented with DPO four-eyes approval; voice audio is removed on erasure; biometrics are never computed.
* **"Can people still delete files themselves?"** Yes. Select files in the dashboard. KORA shows what each deletion would cost and keeps the knowledge capsule. File owners can request deletion of their own files, but two approvers still decide and a legal hold always wins.
* **"How does this help AI later?"** Every slide, frame and transcript passage becomes a cited, permission-aware unit. Assistants get it through `/v1/ai/context` or the MCP server; training and evaluation sets come from the redacted JSONL export. Knowledge stays usable after the file is deleted. See AI_READINESS.md.
* **"What doesn't work yet?"** Real Graph and Play connectors; Swedish ASR; redaction of pixels in keyframes; the Docker stack is not run here.
