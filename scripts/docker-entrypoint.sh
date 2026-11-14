#!/bin/sh
# Prepare the writable places on the /data volume, then run the container's command.
set -e
mkdir -p "${TMPDIR:-/data/tmp}" "${KORA_DATA_DIR:-/data}/secrets"
exec "$@"
