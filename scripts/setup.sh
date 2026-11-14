#!/usr/bin/env bash
# One-time setup for running KORA locally (macOS or Linux). Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
OS="$(uname -s)"

if [ "$OS" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is needed first: open https://brew.sh, run its one-line installer, then run this task again."
    exit 1
  fi
  say "System tools: ffmpeg (video) and tesseract (OCR) via Homebrew"
  brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
  brew list tesseract >/dev/null 2>&1 || brew install tesseract
  if [ ! -d /Applications/LibreOffice.app ]; then
    echo "Optional, for exact PowerPoint slide pictures: brew install --cask libreoffice"
  fi
  PY="$(command -v python3.12 || command -v python3.11 || true)"
  if [ -z "$PY" ]; then
    say "Installing Python 3.12 (Homebrew)"
    brew install python@3.12
    PY="$(brew --prefix)/bin/python3.12"
  fi
else
  for t in ffmpeg tesseract; do
    command -v "$t" >/dev/null 2>&1 || { echo "Please install $t first (Ubuntu: sudo apt install ffmpeg tesseract-ocr)"; exit 1; }
  done
  PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "KORA needs Python 3.11 or newer ($PY is older)."; exit 1; }

say "Python environment in .venv ($("$PY" --version))"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
say "Installing KORA and its Python packages (a few minutes the first time)"
.venv/bin/python -m pip install -q -e ".[dev,whisper]"

export KORA_DATA_DIR="${KORA_DATA_DIR:-$PWD/.kora-data}"
say "Speech models, downloaded once (about 400 MB from GitHub releases), then used offline"
.venv/bin/kora fetch-models || echo "Model download failed. KORA still works with platform transcripts; retry later: .venv/bin/kora fetch-models"

if [ ! -d samples ]; then
  say "Synthetic sample data (Ericsson-style decks, PDFs, a recording)"
  .venv/bin/python scripts/make_samples.py samples
fi

say "Quick self-test"
.venv/bin/python -m pytest -q -x tests/test_units.py

say "Ready. In VS Code: Terminal > Run Task > 'KORA 2: Start the website (dashboard)'"
