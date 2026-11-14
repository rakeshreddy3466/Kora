#!/usr/bin/env bash
# Start KORA in Docker: create .env with random secrets (first time only), build, start, and print
# the website link.   Usage:  bash scripts/docker-up.sh [/path/to/folder/to/process]
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "Docker is not installed: get Docker Desktop from https://www.docker.com/products/docker-desktop/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running: start Docker Desktop, wait for it to say 'running', then run this again."; exit 1; }

key() { python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())" 2>/dev/null || openssl rand -base64 32; }
pw()  { python3 -c "import secrets;print(secrets.token_urlsafe(24))" 2>/dev/null || openssl rand -hex 24; }

if [ ! -f .env ]; then
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(pw)|" \
      -e "s|^KORA_CAPSULE_SIGNING_KEY=.*|KORA_CAPSULE_SIGNING_KEY=$(key)|" \
      -e "s|^KORA_AUDIT_MAC_KEY=.*|KORA_AUDIT_MAC_KEY=$(key)|" .env.example > .env
  chmod 600 .env
  echo "Created .env with random passwords and keys (keep it private; it is git-ignored)."
fi
if [ -n "${1:-}" ]; then
  [ -d "$1" ] || { echo "Not a folder: $1"; exit 1; }
  dir="$(cd "$1" && pwd)"
  if grep -q '^KORA_IMPORT_DIR=' .env; then
    tmp="$(mktemp)"; grep -v '^KORA_IMPORT_DIR=' .env > "$tmp"; cat "$tmp" > .env; rm -f "$tmp"
  fi
  echo "KORA_IMPORT_DIR=$dir" >> .env
  echo "Folder to process: $dir (read-only, at /import inside KORA)"
fi
[ -d samples ] || { echo "No samples folder: using an empty one as /import"; mkdir -p samples; }

echo "Building and starting (the first build downloads about 1 GB, mostly the speech models)..."
docker compose up -d --build

port="$(grep -E '^KORA_PORT=' .env | cut -d= -f2)"; port="${port:-8080}"
printf "Waiting for the website"
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then break; fi
  printf "."; sleep 2
done
echo
link="$(docker compose logs web 2>/dev/null | grep -o "http://127.0.0.1:[0-9]*/ui/#p=[A-Za-z0-9_-]*" | tail -1 | sed "s|127.0.0.1:[0-9]*|127.0.0.1:${port}|")"
if [ -n "$link" ]; then
  echo "KORA is running. Open this link (it carries the demo identities, so keep it private):"
  echo "$link"
  echo "API docs: http://127.0.0.1:${port}/docs   Stop: docker compose down"
  if [ "$(uname)" = "Darwin" ]; then open "$link" || true; fi
else
  echo "The website did not come up yet. See: docker compose logs web"
fi
