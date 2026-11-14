# A future-proof data strategy for recordings and documents

**One sentence:** keep every original where it belongs, keep one governed *knowledge layer*
beside it, and let storage tiers and AI use both through rules that are written down,
versioned and audited.

## 1. Systems of record stay the source of truth
SharePoint, OneDrive and Ericsson Play keep owning files and permissions. KORA reads through
connectors, writes back only through a narrow, conditional action connector (compare-and-swap
on the file hash), and never becomes a second document store.

## 2. A knowledge layer next to every file
Every file becomes **units** (slide, page, keyframe, transcript passage, chat message) with
provenance (document number, revision, page or timestamp, speaker label, location) and a signed
**knowledge capsule**. Formats are open and model-agnostic: JSON, WebVTT, JPEG, Opus, PDF pages.
The capsule outlives the file: compressing, archiving or deleting the original never deletes
what was known.

## 3. Identity and versions are first-class
Content is addressed by SHA-256, slides by two hashes (exact render, and body without the
footer), documents by lineage (document number + revision, with confidence). That is what lets
storage, search and AI all agree on "the current revision" and "the same slide".

## 4. Storage follows value, by policy
| Tier | What lives there | Moves by |
|---|---|---|
| hot | live files (compressed within a measured quality budget), capsules, search index | `compress` plans |
| archive | originals of recordings replaced by a validated capsule | `recordings` plans |
| quarantine | anything removed in the last 30 days, byte-exact | every plan; purge after grace |
Every move is a plan with evidence, approvals and a restore path. The thresholds live in
`policies/default.json`, versioned and hashed into each plan's evidence.

## 5. Data activation for AI, with guardrails
* **Retrieval:** context packs with citations, filtered by the asker's permissions, PII
  redacted, current revisions preferred; delivered by API or MCP to assistants such as Claude.
* **Training and evaluation data:** JSONL export of head revisions, each slide once, speakers
  pseudonymised, provenance on every record.
* **Purpose limitation:** AI use is opt-in per source by a data-protection officer; erasure
  reaches derived data; the audit log never stores questions or names.

## 6. Measure, then decide
Every saving is measured on the organisation's own files (dry-run plans, quality budgets,
per-minute capsule costs), so scenario numbers are built from measurements, not assumptions.
The dashboard's scenario calculator applies those measured ratios to any storage mix.

## What it takes to adopt
1. Read-only pilot on one SharePoint site (Graph `Sites.Selected`), dry-run plans only.
2. Turn on compression and duplicate removal for that site; recordings next, once the
   transcript review step has an owner.
3. Connect assistants through MCP, one opted-in source at a time.
