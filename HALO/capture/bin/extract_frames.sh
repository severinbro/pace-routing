#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command ffmpeg
require_command sha256sum
require_command python3
require_positive_number FRAME_INTERVAL_SECONDS
require_uint JPEG_QUALITY
IMAGE_ROTATION="${IMAGE_ROTATION:-0}"
[[ "$IMAGE_ROTATION" == 0 || "$IMAGE_ROTATION" == 180 ]] || die "IMAGE_ROTATION muss 0 oder 180 sein."

session_dir="${1:-}"
[[ -n "$session_dir" ]] || die "Aufruf: extract_frames.sh SESSION_VERZEICHNIS"
session_dir="$(realpath -e -- "$session_dir")"
sessions_root="$(realpath -m -- "$DATA_ROOT/sessions")"
case "$session_dir" in
  "$sessions_root"/*) ;;
  *) die "Session liegt nicht unter $sessions_root: $session_dir" ;;
esac
[[ -f "$session_dir/.capture_complete" ]] || die "Session ist nicht als vollständig aufgenommen markiert: $session_dir"
[[ -s "$session_dir/video/left.mkv" && -s "$session_dir/video/right.mkv" ]] || die "Ein oder beide Videos fehlen."

if [[ -f "$session_dir/.extraction_complete" ]]; then
  log "Frames wurden bereits extrahiert: $session_dir"
  exit 0
fi
if [[ -f "$session_dir/.extraction_failed" ]]; then
  die "Deterministischer Zeitstempel-Fehler; siehe $session_dir/.extraction_failed"
fi

frames_tmp="$session_dir/.frames.partial"
rm -rf -- "$frames_tmp"
mkdir -p -- "$frames_tmp/left" "$frames_tmp/right"
plan_status=0
python3 "$SCRIPT_DIR/frame_sync.py" plan "$session_dir" "$FRAME_INTERVAL_SECONDS" \
  2>"$session_dir/logs/timing.log" || plan_status=$?
if (( plan_status != 0 )); then
  if (( plan_status == 3 )); then
    cp -- "$session_dir/logs/timing.log" "$session_dir/.extraction_failed"
  fi
  die "Quellvideo-Zeitstempel können nicht geplant werden; siehe logs/timing.log."
fi

extract_one() {
  local side="$1"
  local frame_filter
  frame_filter="$(<"$session_dir/logs/select_${side}.txt")"
  if [[ "$IMAGE_ROTATION" == 180 ]]; then
    frame_filter+=",hflip,vflip"
  fi
  ffmpeg -hide_banner -loglevel info -nostats -y \
    -i "$session_dir/video/${side}.mkv" \
    -vf "$frame_filter" \
    -fps_mode passthrough -q:v "$JPEG_QUALITY" -start_number 0 \
    "$frames_tmp/${side}/frame_%06d.jpg" \
    2>"$session_dir/logs/extract_${side}.log"
}

log "Extrahiere Stereo-Frames im Abstand von ${FRAME_INTERVAL_SECONDS}s."
extract_one left &
left_pid=$!
extract_one right &
right_pid=$!
left_status=0
right_status=0
wait "$left_pid" || left_status=$?
wait "$right_pid" || right_status=$?
(( left_status == 0 && right_status == 0 )) || die "FFmpeg-Extraktion fehlgeschlagen."

map_status=0
python3 "$SCRIPT_DIR/frame_sync.py" extract "$session_dir" "$frames_tmp" \
  "$FRAME_INTERVAL_SECONDS" "${IMU_INTERVAL_SECONDS:-0.05}" \
  2>"$session_dir/logs/timing.log" || map_status=$?
if (( map_status != 0 )); then
  if (( map_status == 3 )); then
    cp -- "$session_dir/logs/timing.log" "$session_dir/.extraction_failed"
  fi
  die "Zeitstempel-Mapping fehlgeschlagen; siehe logs/timing.log, Videos und partielle JPEGs bleiben erhalten."
fi
if [[ -f "$session_dir/capture.json" ]] && python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1])).get("thermal_enabled", False))' "$session_dir/capture.json"; then
  if ! THERMAL_COLOR_MIN_C="${THERMAL_COLOR_MIN_C:-0}" THERMAL_COLOR_MAX_C="${THERMAL_COLOR_MAX_C:-60}" \
    python3 "$SCRIPT_DIR/thermal_sync.py" "$session_dir" "$frames_tmp" 2>"$session_dir/logs/thermal_timing.log"; then
    die "Thermal mapping failed; see logs/thermal_timing.log. Stereo extraction can be retried."
  fi
fi
if [[ -f "$session_dir/capture.json" ]] && python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1])).get("light_enabled", False))' "$session_dir/capture.json"; then
  if ! python3 "$SCRIPT_DIR/light_sync.py" "$session_dir" 2>"$session_dir/logs/light_timing.log"; then
    die "Light timing mapping failed; see logs/light_timing.log."
  fi
fi
[[ ! -e "$session_dir/frames" ]] || die "Zielordner frames existiert bereits; manuelle Prüfung nötig."
mv -- "$frames_tmp" "$session_dir/frames"
mv -- "$session_dir/.pairs.partial" "$session_dir/pairs.csv"
mv -- "$session_dir/.frames_manifest.partial" "$session_dir/frames.csv"
mv -- "$session_dir/.sync_report.partial" "$session_dir/sync_report.json"
if [[ -f "$session_dir/.thermal_manifest.partial" ]]; then
  mv -- "$session_dir/.thermal_manifest.partial" "$session_dir/thermal.csv"
  mv -- "$session_dir/.thermal_metadata.partial" "$session_dir/thermal.json"
fi

(
  cd -- "$session_dir"
  checksum_paths=(capture.json pairs.csv frames.csv sync_report.json frames video)
  if [[ -f imu.csv ]]; then
    checksum_paths+=(imu.csv)
  fi
  if [[ -f light.csv ]]; then
    checksum_paths+=(light.csv)
  fi
  if [[ -f thermal.csv ]]; then
    checksum_paths+=(thermal.csv thermal.json)
  fi
  find "${checksum_paths[@]}" -type f -print0 |
    sort -z |
    xargs -0 sha256sum > SHA256SUMS
)
touch "$session_dir/.extraction_complete"
touch "$session_dir/.transfer_pending"
if [[ -f "$session_dir/thermal.json" ]] &&
   python3 -c 'import json,sys; sys.exit(bool(json.load(open(sys.argv[1])).get("source_error")))' "$session_dir/thermal.json"; then
  rm -f -- "$session_dir/.thermal.partial/thermal.nut"
  rmdir -- "$session_dir/.thermal.partial" 2>/dev/null || true
fi
log "Extraktion mit validierten Zeitstempel-Mappings abgeschlossen: $session_dir"
