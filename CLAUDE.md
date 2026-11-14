# KORA: project context for Claude Code

KORA (Knowledge Optimization, Recovery & Assurance) finds duplicate, superseded and
over-sized files across content sources, proves what can be removed or compressed
without losing knowledge, and removes it only through a governed, reversible plan.
Built for the Ericsson "Data Storage Optimization" hackathon challenge.

## Layout
- `kora/` Python package (modular monolith + DB-backed job queue)
  - `pipeline.py` job handlers: scan → hash → extract → relate → capsule → index → optimize
  - `extraction/` runs inside the sandbox (`security/sandbox.py`): pdf, pptx, docx, msg, video, image, optimize
  - `dedup/`, `versioning/` duplicate classes, lineages, slide store, reconstruction proofs
  - `optimization/engine.py` compression with a measured quality budget
  - `governance/` plans, proof-carrying checks, executor saga (quarantine, purge, restore)
  - `hitl.py` human review tasks; `privacy/` GDPR (PII, DSAR, erasure, AI export)
  - `api/` HTTP API and dashboard read models; `ui/` the dashboard; `ai/` context packs + MCP server
- `policies/default.json` every threshold lives here, not in code
- `tests/` pytest, all fast (inline sandbox); `scripts/` demo + real-data scenarios
- `docs/` architecture, dedup & deletion, versioning, GDPR, HITL, evaluation, results

## Commands
```bash
pip install -e ".[dev]"            # once; system tools: ffmpeg, tesseract, libreoffice (optional)
pytest -q                          # must stay green
kora demo                          # synthetic end-to-end demo
kora ingest-folder <dir> --name ericsson-drive --owner data-owner
kora report                        # findings + savings
kora plan create duplicates|revisions|subsets|media|recordings|compress
kora reviews list                  # human-in-the-loop queue
kora fetch-models                  # Whisper + speaker-turn models from GitHub releases (once)
kora retranscribe                  # re-run speech recognition for recordings without a platform transcript
kora optimize                      # measure compression renditions (before/after previews)
kora dashboard                     # interactive dashboard on 127.0.0.1:8080 (API + worker + UI)
kora mcp                           # MCP server for AI assistants (needs KORA_API_KEY)
docker compose up                  # API + read-only workers + single write-capable executor
```

## Rules (do not break these)
1. **Real Ericsson data never leaves this machine.** Do not upload it, paste file contents
   into issues, or commit it. `reports/real_scenario.json` contains personal data from the
   corpus; it is git-ignored and must stay local.
2. **Never run `plan execute`, `purge` or `erase` against the original folder.** Copy first
   (`cp -a`, keeps mtimes so the TOCTOU guard does not block everything) and point KORA at the copy.
3. **Human decisions belong to the human.** Transcript reviews, plan approvals and erasure
   four-eyes approvals are made by the user with `kora reviews decide` / `kora plan approve`.
   Do not decide review tasks or approve plans on the user's behalf.
4. Thresholds change in `policies/default.json` with a reason in the commit message; never
   hard-code them.
5. Every new deletion or compression path needs: a hard check in `governance/checks.py`, a
   restore path in `executor.py`, and a test that tampers with the source mid-flight.
6. Keep tests fast; long media work belongs in `scripts/`, not in `tests/`.
