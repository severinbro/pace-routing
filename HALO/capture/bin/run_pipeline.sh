#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

mode="${1:-once}"
[[ "$mode" == "once" || "$mode" == "daemon" ]] || die "Aufruf: run_pipeline.sh [once|daemon] [DAUER_SEKUNDEN]"
(( $# <= 2 )) || die "Zu viele Argumente. Aufruf: run_pipeline.sh [once|daemon] [DAUER_SEKUNDEN]"
record_duration="${2:-}"
if [[ -n "$record_duration" ]]; then
  [[ "$mode" == "once" ]] || die "Eine Dauer kann nur mit 'once' verwendet werden."
  require_positive_number record_duration
fi
ensure_data_dirs
MAX_LOCAL_SESSIONS="${MAX_LOCAL_SESSIONS:-100}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-10}"
require_uint MAX_LOCAL_SESSIONS
require_uint RETRY_DELAY_SECONDS
(( MAX_LOCAL_SESSIONS > 0 )) || die "MAX_LOCAL_SESSIONS muss größer als 0 sein."

run_once() {
  local session_dir
  session_dir="$("$SCRIPT_DIR/capture_stereo.sh" "$record_duration")" || return
  if ! DATA_ROOT="$DATA_ROOT" python3 "$SCRIPT_DIR/session_queue.py" enqueue "$session_dir"; then
    log "Queue unavailable; the extractor will recover this session from disk: $session_dir"
  fi
}

if [[ "$mode" == "once" ]]; then
  run_once
  exit 0
fi

log "HALO camera capture started."
while true; do
  session_count="$(find "$DATA_ROOT/sessions" -mindepth 1 -maxdepth 1 -type d ! -name '.*.partial' | wc -l)"
  if (( session_count >= MAX_LOCAL_SESSIONS )); then
    log "Lokales Limit von $MAX_LOCAL_SESSIONS Sessions erreicht; Aufnahme pausiert."
    sleep "$RETRY_DELAY_SECONDS"
    continue
  fi
  if ! run_once; then
    log "Pipeline-Zyklus fehlgeschlagen; neuer Versuch in ${RETRY_DELAY_SECONDS}s."
    sleep "$RETRY_DELAY_SECONDS"
  fi
done
