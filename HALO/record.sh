#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 1 )); then
  echo "Usage: bash ./record.sh DURATION_SECONDS" >&2
  exit 2
fi

duration="$1"
if [[ ! "$duration" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || \
   ! awk -v value="$duration" 'BEGIN { exit !(value >= 1) }'; then
  echo "DURATION_SECONDS must be at least 1 second." >&2
  exit 2
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
mkdir -p -- data
exec 9>data/.record.lock
if ! flock -n 9; then
  echo "Another timed recording is already running." >&2
  exit 1
fi

sudo docker compose up -d redis extractor uploader
sudo docker compose run --rm capture once "$duration"
