#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
exec 9>data/.record.lock
flock -n 9 || { echo 'Another recording is active' >&2; exit 1; }
sudo -n docker compose up -d redis extractor uploader
sudo -n docker compose run --rm -e HALO_STOP_FILE=/data/.ui_stop capture once 1800
