#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

failed=0
for command_name in rpicam-vid rpicam-hello ffmpeg sha256sum awk; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf 'OK       %s: %s\n' "$command_name" "$(command -v "$command_name")"
  else
    printf 'FEHLT    %s\n' "$command_name"
    failed=1
  fi
done

printf '\nKonfiguration: %s\n' "$CONFIG_FILE"
printf 'Datenspeicher: %s\n' "${DATA_ROOT:-nicht gesetzt}"
printf '\nKameraerkennung:\n'
if command -v rpicam-hello >/dev/null 2>&1; then
  rpicam-hello --list-cameras || failed=1
fi

printf '\nUpload: not configured\n'
exit "$failed"
