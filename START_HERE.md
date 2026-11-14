# Run KORA in VS Code (Mac)

About 10 minutes the first time, most of it downloads. Everything runs on your Mac;
nothing is uploaded anywhere.

## 1. Open the project
1. Unzip `kora.zip` (for example into `~/Projects/kora`).
2. VS Code: **File › Open Folder…** and choose the `kora` folder.
3. When VS Code asks *"Do you want to install the recommended extensions?"*, click **Install**
   (Python, Pylance, Python Debugger, Ruff).

## 2. Set up (once)
**Terminal › Run Task… › `KORA 1: Set up (tools, Python env, speech models)`**

It installs, in order:
* `ffmpeg` and `tesseract` with Homebrew (if Homebrew is missing, it tells you how to install it)
* Python 3.12 if needed, and a private environment in `.venv`
* KORA and its Python packages
* the offline speech models (about 400 MB, once)
* the synthetic sample data, then a quick self-test

When it prints *Ready*, press **Cmd+Shift+P › Python: Select Interpreter** and pick `./.venv/bin/python`.

Optional: `brew install --cask libreoffice` renders PowerPoint slides exactly. Without it, PowerPoint
files still work, but revision compaction cannot prove a rebuild.

## 3. Start the website
**Terminal › Run Task… › `KORA 2: Start the website (dashboard)`**

Your browser opens `http://127.0.0.1:8080`. Keep that terminal running; stopping it (the trash-can
icon) stops the website. The link carries demo identities, so don't share it.

## 4. Watch it process your files
In the website, open **Pipeline**:
1. Under *Add a folder*, paste the path of the Ericsson download, e.g.
   `/Users/<you>/Downloads/drive-download-20261113T131714Z-1-001`
   (in Finder: right-click the folder, hold **Option**, choose *Copy … as Pathname*).
2. Click **Copy in and process**.
3. The stages fill up as you watch: copy in → scan → fingerprint → read (pages, slides, frames,
   OCR, speech) → relate → capsule → index → compress. *Running now* shows the current step of
   each job, for example *transcribing speech (12 of 40)*.

KORA works on its own copy in `.kora-data/sources/`, so nothing it does can touch your originals.
On a 2020-or-newer Mac the two recordings take a few minutes each. The documents take seconds.

Then look around:
| Page | What to try |
|---|---|
| **Recordings** | open the GraalVM talk, click a slide, *Describe this slide as text*, *Recreate as video* |
| **Files** | tick files, *Review deletion*: see what KORA says about each one |
| **Compression** | after *Compress* finishes in Pipeline: drag the before/after slider |
| **Plans** | create a plan as Abhilash; switch the sidebar to Jagadish and then Rakesh to approve; switch to Nandu to execute |
| **Reviews** | approve or reject the machine transcripts (this is the human-in-the-loop step) |
| **Search & AI** | search, then tick *Show what an AI assistant would receive* |
| **Overview** | the storage bar and the scenario calculator |

## API docs (Swagger)
Open `http://127.0.0.1:8080/docs` while the website runs. Every call needs a key: click **Authorize**,
paste one `key` value (it starts with `kora_`) from `.kora-data/secrets/ui-personas.json`, then
**Authorize › Close**. Each key is one demo person, so what you may call depends on whose key it is
(Abhilash = analyst, Jagadish/Rakesh = approvers, Nandu = operator, Yashwitha and a second DPO = data protection).
Swagger is served from your Mac, so it works offline too.

## Or: run it in Docker
Same website, but production-shaped: PostgreSQL, the website/API, two workers and a separate
deletion executor, each in its own locked-down container. Nothing to install except Docker Desktop.
1. Install and start **Docker Desktop** (https://www.docker.com/products/docker-desktop/).
2. **Run Task… › `KORA Docker: start (build and run everything)`** and paste the folder to process,
   or in the terminal: `bash scripts/docker-up.sh ~/Downloads/drive-download-…`
   The first build takes 5–10 minutes (about 1 GB, mostly the speech models). It writes `.env` with
   random passwords and keys, starts everything and prints the website link.
3. In the website, **Pipeline › Add a folder** already says `/import`: that is your folder, mounted
   read-only. Click **Copy in and process**.
4. Watch the workers: **Run Task… › `KORA Docker: watch the workers`**. Stop with `KORA Docker: stop`
   (your data stays) or `KORA Docker: stop and delete its data`.

Lost the link? `docker compose logs web | grep "/ui/#p="`. API docs: `http://127.0.0.1:8080/docs`.
Don't run task 2 and Docker at the same time: both use port 8080 (or set `KORA_PORT` in `.env`).

## Connect an AI assistant (MCP)
KORA is also an MCP server, so Claude can answer questions from your files, with citations and
with one person's permissions. It is read-only.
1. As Yashwitha (data protection), open **Search & AI** and allow the source under *Sources opted in for AI use*.
2. **Run Task… › `KORA: Connect an AI assistant (MCP settings)`**. It creates a read-only key with Abhilash's
   permissions (`--as <person id>` for someone else) and prints:
   * a block for Claude Desktop: **Settings › Developer › Edit Config**, paste it under `mcpServers`, restart Claude;
   * a `claude mcp add …` line for Claude Code.
3. Ask: *"Using KORA, what did the GraalVM talk conclude about native images? Cite sources."*

The assistant gets three tools: `search_knowledge` (cited passages), `document_versions` (which revision
is current) and `get_capsule` (a recording or document as text, even after the original is deleted).
In Docker, run in the `kora` folder: `docker compose exec web kora mcp-config --as ui-analyst --docker --compose "$PWD/docker-compose.yml"`.
It prints the same settings, starting the server with `docker compose exec`.

## 5. Other things in Terminal › Run Task…
| Task | What it does |
|---|---|
| `KORA 3a: Process the synthetic demo data` | the generated Ericsson-style corpus (every duplicate class) |
| `KORA 3b: Process a folder` | same as *Add a folder*, from the terminal, with a summary report |
| `KORA: Transcribe recordings (offline Whisper)` | re-run speech recognition |
| `KORA: Measure compression` | make and measure smaller versions now |
| `KORA: Report (findings and savings)` | text summary in the terminal |
| `KORA: Run tests` | the test suite (also in the Testing panel, the flask icon) |
| `KORA: Start from scratch (delete .kora-data)` | wipes KORA's workspace, never your originals |

## 6. Debugging (to see how it works inside)
**Run and Debug** panel (Cmd+Shift+D):
* `Debug: website (dashboard)` runs the website under the debugger. Sandboxed parsing runs
  in-process here, so breakpoints in `kora/extraction/*.py` are hit.
* `Debug: process a folder` processes one folder step by step.
* `Debug: current test file` runs the open test file under the debugger.

Good places for a first breakpoint:
| File | Function | What happens there |
|---|---|---|
| `kora/pipeline.py` | `extract` | a file is read in the sandbox and becomes units |
| `kora/extraction/video.py` | `extract` | slide changes, OCR, speech, speaker turns |
| `kora/dedup/engine.py` | `relate` | duplicates and versions are found |
| `kora/governance/checks.py` | `evaluate` | the proof each deletion must pass |
| `kora/governance/executor.py` | `quarantine_item` | the deletion saga |

## If something goes wrong
* **`brew: command not found`**: install Homebrew from https://brew.sh, then run task 1 again.
* **Port 8080 in use**: edit the port in `.vscode/tasks.json` (task 2), or stop the other program.
* **Recordings show "no transcript"**: the speech-model download failed; run
  `KORA_DATA_DIR=.kora-data .venv/bin/kora fetch-models` in the VS Code terminal.
* **A recording says "timed out"**: a reading job may take up to 2 hours by default. For longer
  meetings raise `KORA_MEDIA_TIMEOUT_S` in `.vscode/kora.env` and in your terminal, then restart task 2.
  A failed job is retried automatically (up to 5 times), so you can also just wait.
* **Stopped the website while a recording was being read?** Nothing is lost: when you start task 2
  again it prints *Picking up 1 job(s)…* and reads that recording again from the start.
* **Want a clean start**: run the *Start from scratch* task, then task 2 again.
