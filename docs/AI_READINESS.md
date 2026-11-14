# Using KORA's output as input for AI

Deleting files is half of the brief. The other half: "deleted content is lost, also for AI".
KORA turns every file it reads into knowledge an AI can use *safely*, and keeps that
knowledge after the original is compressed, archived or deleted.

## What KORA produces that an AI can use

| Layer | What it is | Why it matters for AI |
|---|---|---|
| **Units** | every slide, page, keyframe (with OCR), transcript passage (with speaker and time) and chat message, as text | the natural chunk size for retrieval; no re-parsing of PPTX/PDF/MP4 |
| **Provenance** | per unit: document number, revision, page or timestamp, which slide was on screen, where the file lives | answers can cite "1/1551-LZA 701 0001 rev B, slide 3" or "review meeting at 12:40" |
| **Versions** | lineages with the current revision marked, slide diffs between revisions | the assistant answers from the current revision and can say what changed |
| **Duplicates collapsed** | each slide body indexed once, with "also in" pointers | no five identical chunks drowning the one that matters |
| **Knowledge capsules** | signed manifest of a file's text, frames, transcript and links | survive deletion: a deleted file still answers questions |
| **Quality labels** | platform transcript vs machine transcript (reviewed or not), ordering confidence | the AI can say how far to trust a passage |
| **Permissions** | ACL per file, ACL snapshot in the capsule | an assistant only sees what the asking person may read |

## Three ways to feed it to AI (all built)

1. **Retrieval for an assistant (RAG): `GET /v1/ai/context?q=…`**
   Returns a *context pack*: numbered passages, each with a citation and a quality note,
   plus instructions ("answer only from the sources, cite [n], prefer current revisions,
   say when a transcript is unreviewed"). It runs with the asking person's permissions.
   The dashboard's *Search and AI* page shows exactly what an assistant would receive.

2. **MCP server for assistants such as Claude: `kora mcp`**
   Read-only tools `search_knowledge`, `document_versions` and `get_capsule`, over stdio.
   The API key in `KORA_API_KEY` decides whose permissions apply. Configuration:
   ```json
   { "mcpServers": { "kora": { "command": "kora", "args": ["mcp"],
       "env": { "KORA_DATA_DIR": "/path/to/kora-data", "KORA_API_KEY": "kora_… (viewer key)" } } } }
   ```
   It cannot plan, approve or delete anything.

3. **Dataset export: `POST /v1/ai-dataset` (`kora ai-export`)**
   JSONL for fine-tuning, evaluation sets or a vector database. Head revisions only, each
   slide once, people's names and contact data redacted, speakers as stable pseudonyms,
   provenance on every record.

## The rules that make it safe (GDPR by design)

* **Opt-in per source.** `ai_use_allowed` is off by default. Only a data-protection officer
  changes it (dashboard or `POST /v1/sources/{name}/ai-use`), with a reason, and it is audited.
  The opt-in must cover a copy the *asker* can read: a share in someone's OneDrive does not
  inherit the team site's decision.
* **Minimisation.** E-mail, phone, personnummer (Luhn-checked), IBAN, IP and known names are
  redacted unless a DPO decided the content holds no personal data.
* **Erasure reaches AI data.** A completed erasure request removes the person from units,
  capsules and the search index, so later context packs and exports no longer contain them.
* **The question is not logged.** The audit log records that a context pack was served
  (and how many passages were withheld), never the question text.

## What a future pipeline adds (not built yet)

* Swap the offline hashing embedder for an approved embedding model and an ANN index
  (pgvector HNSW): `fingerprint/embed.py` is the single seam.
* An LLM summariser that writes a per-capsule abstract with the same provenance fields, so
  an assistant can answer "what did the March review decide?" from one record.
* Evaluation sets generated from decisions and action items already extracted with provenance.
