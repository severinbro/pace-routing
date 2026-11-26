#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 1 )); then
  echo "Usage: bash ./upload.sh SESSION_ID" >&2
  exit 2
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
exec sudo docker compose exec -T uploader \
  python3 /app/capture/bin/upload_sessions.py request "$1"
