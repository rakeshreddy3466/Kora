# Results on Ericsson's sample files

Corpus: the 10 files from the challenge's Drive folder, **329.4 MB** (314.2 MiB). All sizes below are decimal MB.

| File | Type | Size |
|---|---|---|
| GraalVMs_Ahead-of-Time_Compilation__Source_.mp4 | talk, 16.0 min, 1080p 30 fps, 1.8 Mbps, no transcript | 220.5 MB |
| Jury_and_prizes.mp4 | Teams recording, 6.0 min, 720p, Swedish/English, no transcript | 88.7 MB |
| SD-WAN_Orchestration_commercial_presentation_final.pptx / _final2.pptx | two revisions of one Ericsson deck | 3.7 + 3.5 MB |
| C2L_Presentation_Hackathon_RevB.pdf, Ericsson_Response_Introduction…pdf | presentations | 3.5 + 8.8 MB |
| Challenge_Ericsson.docx | the brief | 0.3 MB |
| The_chat_history_of_the_recording_with_time_stamp_kept.msg | Teams chat mailed as Outlook message | 83 KB |
| Brief.png, Copy_of_Brief.png | identical images | 2 × 229 KB |

Reproduce:

```
kora ingest-folder <folder> --name ericsson-drive --workers 2
python scripts/real_data_scenario.py --subject "<chat participant>"
python scripts/real_compression.py            # on a copy: replaces files in place
python scripts/reliability_check.py <folder> --workers 3
```

The ingest settings were `KORA_PPTX_RENDERER=libreoffice` and `KORA_SANDBOX=subprocess`. The reliability check ran on PostgreSQL.

## Pipeline

* **Processing:** 10 files / 314 MB in **361 s on 2 CPUs**. 49 jobs, 0 failures, 0 retries. Every parser ran in the sandbox.
* **Crash test on this data (PostgreSQL, 3 workers):** one worker was SIGKILLed at 60 s, in the middle of a video extraction. The job was re-claimed and completed exactly once, and all 8 invariants held: exactly-once extraction, consistent units, one head per lineage, no duplicate relations, valid audit chain, idempotent rescan. Total time was 348 s.
* **Rescan before planning:** re-hashes the files and re-extracts nothing (9 extractions for 9 distinct contents).

## What KORA found, per brief item

| Brief item | Result on Ericsson's files |
|---|---|
| duplicates ("download copies") | `Copy_of_Brief.png` = `Brief.png` byte for byte. A governed plan quarantined the copy, left a stub pointing to the original, and the deletion is restorable for 30 days |
| revisions with minor changes | the two SD-WAN decks share one Ericsson document number. **The file names contradict document control: `final` is Rev C and `final2` is Rev B.** KORA reads the deck's custom properties and orders them B → C with high confidence. Ordering by name would have made the *older* deck the head |
| per-slide hash | 25/28 slides each, **including 3 hidden slides per deck**, rendered via LibreOffice and hashed. Every slide differs between B and C because the revision letter sits in the footer, so the footer-insensitive body hash is what relates them |
| slide store + recovery proof | default policy keeps released revisions (B is released), so nothing is deleted. What-if with a head-only policy: Rev B can be compacted, keeping **0.71 MB** (a page pack of all 25 slides) and freeing **2.75 MB of 3.46 MB (79 %)**. All 25 pages were rebuilt and re-rendered to their recorded hashes |
| video → text + frames | GraalVM talk: **220.5 MB → 2.9 MB capsule (98.7 %; 99.4 % without the speech track)**, 17 keyframes, all with OCR text. Jury recording: **88.7 MB → 0.76 MB (99.1 %; 99.7 %)** |
| transcripts | neither recording had a platform transcript. The offline fallback (PocketSphinx, English) produced **unusable text** (e.g. "…some fish overlap that we go to a cobalt ago…"). The reviewer rejected both, so **KORA refused to archive either recording**. This is the human-in-the-loop gate working as intended, and the main open gap (see below) |
| slide ↔ frame link | the talk's slides are not in the corpus, and the jury video shows a Teams screen ("Jury deliberation 2023-03-30 16:04 UTC"), so there are correctly **no** links. KORA does not invent them |
| chat with timestamps | 11 messages parsed with author and time, searchable ("Wi-Fi is unlicensed" → its author, 16:54). The chat's wall-clock times (15:41–17:54) share no clock with the 6-minute recording, so it is **not** aligned to the video |
| search | keyframe OCR finds the talk's content (e.g. "Analysis of Findings … Native images …" at 524 s), plus slides and chat. Machine-transcript hits are labelled "not reviewed" |

## Compression on this data (round 4: no transcript needed)

Every rendition was produced in the sandbox, measured, and then replaced the original
through a `compress` plan: two approvers (scripted in this reproducible run), original copied to quarantine and
hash-verified, conditional in-place swap. Files keep their path and name and still open.

| File | Before | After | Saved | Measured quality (all hard gates passed) |
|---|---|---|---|---|
| GraalVM talk (16 min) | 220.5 MB | **15.2 MB** | **93 %** | SSIM 0.9993; OCR recovers 93 % of slide words on 11 keyframes; duration Δ 0.03 s; speech kept |
| Jury recording (6 min) | 88.7 MB | **32.0 MB** | **64 %** | SSIM 0.9889; duration Δ 0.004 s; speech kept; OCR 100 % (only 1 text keyframe: mostly camera) |
| Ericsson_Response_Introduction PDF | 8.8 MB | **6.1 MB** | **30 %** | text identical on all 19 pages; worst page mean-SSIM 0.976 at 144 dpi; per-page fallback kept 2 images original |
| C2L deck PDF | 3.5 MB | **2.4 MB** | **33 %** | text identical on all 26 pages; worst page mean-SSIM 0.974 at 144 dpi |
| **Total** | **321.5 MB** | **55.7 MB** | **265.8 MB (83 % of these files)** | |

* **Time:** the 16-minute 1080p talk took 4.4 min to transcode *and* measure on 2 CPUs; the
  jury video and both PDFs took 104 s together.
* **Re-extraction after the swap:** both recordings were re-indexed from the rendition
  (keyframes, OCR) and *inherited* their transcripts with provenance kept as
  `asr:pocketsphinx`, so they still require human review. No second ASR pass.
* **Reversibility on real data:** the C2L deck was restored from quarantine and
  re-hashed **byte-identical** to the original. Audit chain valid (74 events).
* **Not compressed, by design:** the two PPTX decks (1.6 MB of media, below the
  worthwhile threshold), the PNGs (one is an exact copy, removed by dedup instead) and the
  docx/msg (too small). Engineering diagrams are never re-encoded lossily.
* Side-by-side frames and the worst PDF page were checked by eye: slide text is
  equally sharp. The numbers above are what the jury can verify with
  `kora optimize` on the copy.

## Transcripts and speakers on this data (round 6: offline Whisper)

PocketSphinx produced unusable text (see the table above), so KORA now runs Whisper (small,
int8 ONNX, via sherpa-onnx) inside the sandbox, with speaker turns. Both models come from
GitHub releases (`kora fetch-models`) and run offline.

| Recording | Before (PocketSphinx) | Now (Whisper + speaker turns) | Time on 2 CPUs |
|---|---|---|---|
| GraalVM talk, 16 min | "…some fish overlap that we go to a cobalt ago…" | 90 passages, 2 speakers: "Hello, and welcome to our thesis presentation. We're going to talk about the GraalVM's ahead-of-time compilation benefits and challenges in production." | about 8 min (incl. keyframes and OCR) |
| Jury recording, 6 min | unusable | 42 passages, 2 speakers (host, presenter): "Before I announce the winner, let me start by saying that…" | about 3.5 min |

* The first run detected some accented English as Swedish and Arabic and "translated" it. Now
  languages with less than a quarter of the speech are re-decoded in the main language.
* The first clustering found 7 speakers in a two-presenter talk. Short split-off voices are now
  merged into the previous speaker, which gives 2.
* Remaining errors are names and terms ("GROAL VM"). That is why a person still approves a
  machine transcript before it can stand in for the video.

| Capsule (measured) | Talk | Jury |
|---|---|---|
| original | 220.5 MB | 88.7 MB |
| capsule with speech track | **2.93 MB (98.7 %)** | **0.76 MB (99.1 %)** |
| text + slide pictures only | 1.22 MB (99.4 %) | 0.24 MB (99.7 %) |
| recreated video from the capsule | 2.65 MB, built in 7.8 s | – |
| one slide as a text description | 1.4–2.5 kB instead of a 105–135 kB picture; 91–98 % of the words come back in the recreation | – |

The recordings plan still waits for a person to approve each transcript (`transcript_reviewed`).
That is the human-in-the-loop gate, not a bug.

## Human in the loop on this data
The pipeline opened **7 review tasks**:

* 5 privacy reviews for the DPO: e-mail and phone in the chat signature, an e-mail in the C2L deck, and author metadata (Ericsson user signums) in the decks and the docx
* 2 transcript reviews

Both transcripts were rejected with a reason, and that rejection is what blocks the recordings' archive move.

## GDPR on this data

| Check | Result |
|---|---|
| AI export (opt-in source) | 195 records, 13 redacted. A re-scan of the export finds **0** remaining personal data. Chat authors appear as `[NAME]`, speakers as pseudonyms |
| access request (Art. 15) for a chat participant | 9 chat messages written by him and 1 slide mentioning him |
| erasure (Art. 17) | the requesting DPO's own approval was **refused** (four eyes); a second DPO approved. **0** mentions remain in derived data or search. This needed accent-insensitive matching: the chat spells the surname without an accent, the slide with one |
| audit log | 57 events, chain valid, **no file names** stored |
| known gap | names visible *in keyframe pixels* (e.g. the presenter's name on the GraalVM slides) are pseudonymised in OCR text but not blurred in the JPEG |

## Defects the real data exposed, all fixed and covered by tests

1. Ericsson document numbers with an inner space (such as `1/221 09-ABC…`) and pipe-separated footer revisions were not recognised.
2. Revision order came from file names because DocNo/Revision live in OOXML custom properties. **This would have proposed compacting the newer deck.** Custom properties now outrank file names.
3. Hidden slides made the PPTX render "fail" (page-count mismatch). They are now rendered from an unhidden temporary copy, so the recovery proof covers every slide.
4. ASR segments ending at a 5-minute chunk boundary got `end < start`.
5. Capsule validation counted only pages and slides, so `.msg` and `.png` capsules showed as invalid.
6. **Storing one PDF per page made the slide store larger than the decks** (14.2 MB for a 3.5 MB deck). The fix has three parts:
   * pages are materialised lazily, only when a compaction needs them
   * they are stored as one page pack (0.71 MB)
   * a hard `net_saving` check refuses deletions that free less than 10 %, and discards anything stored for them
7. `.docx` and `.msg` (Teams chat) were not supported.
8. Author metadata (signums) is personal data and was not flagged.
9. Erasure missed accented name variants.
10. A schema change broke an existing database. Additive migrations now run on start.
11. Plans built on stale source state were (correctly) refused by the live eTag check. The scenario now rescans first.
12. **MuPDF printed a warning to stdout** on the C2L deck and corrupted the sandbox reply. The sandbox now answers on a private channel.
13. **PyMuPDF's built-in image rewrite damaged one C2L page** (worst-page SSIM 0.965) and grew the file. The quality budget caught it; KORA now rewrites each image XObject itself and falls back per page.
14. **A compressed PDF lost its "rendition of" marker on re-extraction**, so KORA measured its own output again (the net-saving gate refused it: 0 %). The lineage marker is now kept and renditions are never re-compressed.
15. **After compression, the dashboard showed new findings that were not real.** The compressed PDF claimed "Rev B" a second time and opened a lineage-conflict review. Each rendition was also reported as a near-duplicate or "other encoding" of its own original, and each recording asked for a second transcript review. Now a rendition joins its original's version group, twin relations are never created, and one transcript review decides for the original and all its renditions.
16. **Before/after previews disappeared once a file was replaced**, because the original no longer had a live copy to check permissions against. Previews are now readable by anyone who can read either the original or the rendition.

## Measured savings (honest)

| Category | Bytes | Status |
|---|---|---|
| exact copies | 0.23 MB | **done** in the governance scenario (quarantined with stub, restorable) |
| compression within the quality budget | **265.8 MB** | **done** on a copy: 2 approvers, executed, files in place; originals in quarantine for 30 days, then purged |
| superseded released revision | 2.75 MB | only if policy allows head-only (a records decision) |
| recordings → capsules + archive | 309.1 MB → 3.7 MB (98.8 %) | transcripts now exist (offline Whisper); waiting for a person to approve them. Then archiving replaces compression for the recordings |
| KORA's own footprint for live documents | ≈ 0.7 MB | text, hashes and thumbnails for search; no second copy |

**Proven and executed today: 266.0 MB of 329.4 MB = 80.8 % of the corpus**, with every
file still in place, readable and restorable. Each file is counted once
(removal > archive > compression). The bytes are freed when the 30-day grace period ends
and purge deletes the quarantined originals. With a reviewed transcript the recordings
can go further (archive + capsule): about **97 %** of hot storage.
