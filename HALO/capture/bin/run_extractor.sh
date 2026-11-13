#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

ensure_data_dirs
export DATA_ROOT
exec python3 "$SCRIPT_DIR/session_queue.py" worker
