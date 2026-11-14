# Running KORA on your Mac with Claude Code

Claude Code works in your terminal, inside the project folder, so it can install the
system tools, download the Whisper speech model, read the Ericsson files where they
already are, and run the full pipeline and Docker stack. The data never has to leave
your laptop, which is also the cleanest GDPR story for the demo.

## 1. Install (once)
```bash
xcode-select --install                                # git + compilers, if not already there
curl -fsSL https://claude.ai/install.sh | bash        # or: brew install --cask claude-code
claude --version && claude doctor
```
Needs macOS 13+ and a paid Claude plan (Pro or higher); the first `claude` run opens a browser sign-in.
If `ANTHROPIC_API_KEY` is set in your shell, Claude Code bills the API instead of your plan.

## 2. Open the project
```bash
cd ~/Downloads && unzip kora.zip && cd kora
claude
```
Claude Code reads `CLAUDE.md` automatically: the layout, commands and the rules (data stays
local, never execute plans on originals, humans make review/approval decisions).

## 3. Prompts to paste, in order

**A. Environment**
> Install everything KORA needs on this Mac: Homebrew packages ffmpeg, tesseract and
> libreoffice; a Python 3.12 venv with `pip install -e ".[dev,whisper]"`.
> Run `KORA_DATA_DIR=~/kora-work/data kora fetch-models` (Whisper + speaker turns, about 400 MB,
> downloaded once from GitHub releases, then used offline). Then run `pytest -q` and show me the summary.

**B. Real data, on a copy**
> Copy `~/Downloads/drive-download-20261113T131714Z-1-001` to `~/kora-work/sources` with `cp -a`.
> Set `KORA_DATA_DIR=~/kora-work/data` and run `kora ingest-folder ~/kora-work/sources --name ericsson-drive --owner data-owner`.
> Then `kora report` and `kora findings`, and summarise duplicates, lineages and recordings for me.

**C. Transcripts (you decide)**
> Show me the open transcript review tasks with their evidence samples (`kora reviews list`).
> Do not decide them. I will read them and run `kora reviews decide` myself.

**D. Compression**
> Run `python scripts/real_compression.py --no-execute` on the copy and show me the
> measured quality checks for each rendition. Open the rendered worst page of each PDF
> and a frame from each video side by side so I can check them.

After you have looked at the renditions yourself, approve with two different reviewer identities
and execute: `KORA_DATA_DIR=~/kora-work/data python scripts/real_compression.py`.

**E. Governance + GDPR scenario**
> Run `python scripts/real_data_scenario.py --subject "<a participant from the chat>"` on the copy.
> Keep `reports/real_scenario.json` local. Summarise the plans, blocks and DSAR results.

**F. Dashboard and demo**
> Run `KORA_DATA_DIR=~/kora-work/data kora dashboard --port 8090` and keep it running. Then start
> `docker compose up` in a second terminal and check that its API answers at http://127.0.0.1:8080/docs.
> Help me record a 3-minute screen demo following docs/PITCH.md.

In the dashboard, **you** click through the approvals with the approver identities, so the
separation of duties is real on screen.

## 4. What Claude Code should not do
- Approve plans or decide reviews: those are the human-in-the-loop steps judges look for.
- Run `execute`, `purge` or `dsar erase` on `~/Downloads/...` originals.
- Commit or upload anything from `reports/` or the source folder.
