#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_command rpicam-vid
require_uint LEFT_CAMERA_INDEX
require_uint RIGHT_CAMERA_INDEX
require_uint VIDEO_WIDTH
require_uint VIDEO_HEIGHT
require_uint VIDEO_FPS
require_uint VIDEO_BITRATE
record_duration="${1:-$CLIP_DURATION_SECONDS}"
require_positive_number record_duration
awk -v value="$record_duration" 'BEGIN { exit !(value >= 1) }' || die "Aufnahmedauer muss mindestens 1 Sekunde betragen."
[[ "$LEFT_CAMERA_INDEX" != "$RIGHT_CAMERA_INDEX" ]] || die "Linke und rechte Kamera dürfen nicht denselben Index verwenden."
IMU_ENABLED="${IMU_ENABLED:-false}"
[[ "$IMU_ENABLED" == true || "$IMU_ENABLED" == false ]] || die "IMU_ENABLED muss true oder false sein."
THERMAL_ENABLED="${THERMAL_ENABLED:-false}"
[[ "$THERMAL_ENABLED" == true || "$THERMAL_ENABLED" == false ]] || die "THERMAL_ENABLED muss true oder false sein."
LIGHT_ENABLED="${LIGHT_ENABLED:-false}"
[[ "$LIGHT_ENABLED" == true || "$LIGHT_ENABLED" == false ]] || die "LIGHT_ENABLED muss true oder false sein."
THERMAL_DEVICE="${THERMAL_DEVICE:-/dev/v4l/by-id/usb-Generic_USB_Camera_200901010001-video-index0}"
if [[ "$IMU_ENABLED" == true ]]; then
  require_command python3
  IMU_I2C_BUS="${IMU_I2C_BUS:-1}"
  IMU_ADDRESS="${IMU_ADDRESS:-0x4a}"
  IMU_INTERVAL_SECONDS="${IMU_INTERVAL_SECONDS:-0.05}"
  require_uint IMU_I2C_BUS
  require_positive_number IMU_INTERVAL_SECONDS
fi
LIGHT_I2C_BUS="${LIGHT_I2C_BUS:-1}"
LIGHT_ADDRESS="${LIGHT_ADDRESS:-0x23}"
LIGHT_INTERVAL_SECONDS="${LIGHT_INTERVAL_SECONDS:-0.2}"
if [[ "$LIGHT_ENABLED" == true ]]; then
  require_command python3
  require_uint LIGHT_I2C_BUS
  require_positive_number LIGHT_INTERVAL_SECONDS
  awk -v value="$LIGHT_INTERVAL_SECONDS" 'BEGIN { exit !(value >= 0.2) }' || die "LIGHT_INTERVAL_SECONDS muss mindestens 0.2 sein."
  [[ "$LIGHT_ADDRESS" == 0x23 || "$LIGHT_ADDRESS" == 0x5c || "$LIGHT_ADDRESS" == 0x5C ]] || die "LIGHT_ADDRESS muss 0x23 oder 0x5c sein."
fi
ensure_data_dirs
load_camera_args

host_name="$(hostname -s | tr -cd 'A-Za-z0-9._-')"
session_id="$(date -u +'%Y%m%dT%H%M%S.%3NZ')-${host_name:-pi}-$$"
partial_dir="$DATA_ROOT/sessions/.${session_id}.partial"
session_dir="$DATA_ROOT/sessions/$session_id"
mkdir -p -- "$partial_dir/video" "$partial_dir/logs"

left_pid=''
right_pid=''
imu_pid=''
thermal_pid=''
light_pid=''
cleanup() {
  local code=$?
  trap - EXIT INT TERM
  [[ -n "$left_pid" ]] && kill "$left_pid" 2>/dev/null || true
  [[ -n "$right_pid" ]] && kill "$right_pid" 2>/dev/null || true
  [[ -n "$imu_pid" ]] && kill "$imu_pid" 2>/dev/null || true
  [[ -n "$thermal_pid" ]] && kill "$thermal_pid" 2>/dev/null || true
  [[ -n "$light_pid" ]] && kill "$light_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  if (( code != 0 )); then
    log "Unvollständige Aufnahme bleibt zur Diagnose erhalten: $partial_dir"
  fi
  exit "$code"
}
trap cleanup EXIT INT TERM

duration_ms="$(awk -v seconds="$record_duration" 'BEGIN { printf "%.0f", seconds * 1000 }')"
duration_json="$(awk -v milliseconds="$duration_ms" 'BEGIN { printf "%.3f", milliseconds / 1000 }')"

if [[ "$IMU_ENABLED" == true ]]; then
  python3 "$SCRIPT_DIR/imu_logger.py" \
    --output "$partial_dir/imu.csv" \
    --ready "$partial_dir/.imu_ready" \
    --bus "$IMU_I2C_BUS" \
    --address "$IMU_ADDRESS" \
    --interval "$IMU_INTERVAL_SECONDS" \
    2>"$partial_dir/logs/imu.log" &
  imu_pid=$!
  for ((attempt=0; attempt<50; attempt++)); do
    [[ -f "$partial_dir/.imu_ready" ]] && break
    kill -0 "$imu_pid" 2>/dev/null || die "IMU konnte nicht gestartet werden; siehe logs/imu.log."
    sleep 0.2
  done
  [[ -f "$partial_dir/.imu_ready" ]] || die "IMU-Start hat zu lange gedauert; siehe logs/imu.log."
  rm -- "$partial_dir/.imu_ready"
fi
light_status="disabled"
if [[ "$LIGHT_ENABLED" == true ]]; then
  light_status="unavailable"
  python3 "$SCRIPT_DIR/light_logger.py" \
    --output "$partial_dir/light.csv" \
    --ready "$partial_dir/.light_ready" \
    --bus "$LIGHT_I2C_BUS" \
    --address "$LIGHT_ADDRESS" \
    --interval "$LIGHT_INTERVAL_SECONDS" \
    2>"$partial_dir/logs/light.log" &
  light_pid=$!
  for ((light_attempt=0; light_attempt<25; light_attempt++)); do
    [[ -f "$partial_dir/.light_ready" ]] && break
    kill -0 "$light_pid" 2>/dev/null || break
    sleep 0.2
  done
  if [[ -f "$partial_dir/.light_ready" ]]; then
    rm -- "$partial_dir/.light_ready"
    light_status="recording"
  else
    kill -TERM "$light_pid" 2>/dev/null || true
    wait "$light_pid" 2>/dev/null || true
    light_pid=''
    log "BH1750 did not start; continuing camera capture (logs/light.log)."
  fi
fi
thermal_status="disabled"
if [[ "$THERMAL_ENABLED" == true ]]; then
  thermal_status="unavailable"
  mkdir -p -- "$partial_dir/.thermal.partial"
  if [[ -c "$THERMAL_DEVICE" ]]; then
    ffmpeg -hide_banner -loglevel info -nostats -y \
      -f v4l2 -timestamps mono2abs -input_format yuyv422 \
      -video_size 256x384 -framerate 25 -i "$THERMAL_DEVICE" \
      -copyts -fps_mode passthrough -c:v ffv1 -f nut \
      "$partial_dir/.thermal.partial/thermal.nut" \
      2>"$partial_dir/logs/thermal.log" &
    thermal_pid=$!
    for ((thermal_attempt=0; thermal_attempt<50; thermal_attempt++)); do
      [[ -s "$partial_dir/.thermal.partial/thermal.nut" ]] && break
      kill -0 "$thermal_pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$thermal_pid" 2>/dev/null && [[ -s "$partial_dir/.thermal.partial/thermal.nut" ]]; then
      thermal_status="recording"
    else
      kill -TERM "$thermal_pid" 2>/dev/null || true
      wait "$thermal_pid" 2>/dev/null || true
      thermal_pid=''
      log "Thermal camera did not start; continuing stereo capture (logs/thermal.log)."
    fi
  else
    log "Thermal camera not found at $THERMAL_DEVICE; continuing stereo capture."
  fi
fi
started_at_utc_ns="$(date +%s%N)"

capture_one() {
  local side="$1" camera_index="$2" sync_role="$3" output="$4" camera_log="$5"
  shift 5
  exec rpicam-vid \
    --camera "$camera_index" \
    --width "$VIDEO_WIDTH" \
    --height "$VIDEO_HEIGHT" \
    --framerate "$VIDEO_FPS" \
    --bitrate "$VIDEO_BITRATE" \
    --timeout "$duration_ms" \
    --sync "$sync_role" --verbose 2 \
    --codec h264 --libav-format matroska \
    --profile high --inline --flush --nopreview \
    "${CAMERA_COMMON_ARGV[@]}" "$@" \
    --output "$output" 2>"$camera_log"
}

log "Starte Stereo-Session $session_id für ${record_duration}s."
capture_one right "$RIGHT_CAMERA_INDEX" client "$partial_dir/video/right.mkv" "$partial_dir/logs/right.log" "${RIGHT_CAMERA_ARGV[@]}" &
right_pid=$!
sleep 0.5
kill -0 "$right_pid" 2>/dev/null || die "Rechte Sync-Client-Kamera endete vor Serverstart; siehe logs/right.log."
capture_one left "$LEFT_CAMERA_INDEX" server "$partial_dir/video/left.mkv" "$partial_dir/logs/left.log" "${LEFT_CAMERA_ARGV[@]}" &
left_pid=$!

left_status=0
right_status=0
stop_requested=false
if [[ -n "${HALO_STOP_FILE:-}" ]]; then
  while kill -0 "$left_pid" 2>/dev/null || kill -0 "$right_pid" 2>/dev/null; do
    if [[ -f "$HALO_STOP_FILE" ]]; then
      stop_requested=true
      log "Graceful stop requested for $session_id."
      kill -INT "$left_pid" "$right_pid" 2>/dev/null || true
      break
    fi
    sleep 0.5
  done
fi
wait "$left_pid" || left_status=$?
wait "$right_pid" || right_status=$?
left_pid=''
right_pid=''

if [[ "$stop_requested" == false ]]; then
  (( left_status == 0 )) || die "Linke Aufnahme fehlgeschlagen (Status $left_status); siehe logs/left.log."
  (( right_status == 0 )) || die "Rechte Aufnahme fehlgeschlagen (Status $right_status); siehe logs/right.log."
fi
[[ -s "$partial_dir/video/left.mkv" ]] || die "Linkes Video ist leer."
[[ -s "$partial_dir/video/right.mkv" ]] || die "Rechtes Video ist leer."
for side in left right; do
  ffprobe -v error -show_entries stream=codec_type -of csv=p=0 \
    "$partial_dir/video/$side.mkv" | grep -qx video || die "$side video did not finalize cleanly."
done
if [[ -n "$thermal_pid" ]]; then
  thermal_was_running=false
  if kill -0 "$thermal_pid" 2>/dev/null; then
    thermal_was_running=true
    kill -TERM "$thermal_pid" 2>/dev/null || true
    thermal_exit=0
    wait "$thermal_pid" || thermal_exit=$?
  else
    thermal_exit=1
    wait "$thermal_pid" 2>/dev/null || true
  fi
  thermal_pid=''
  # FFmpeg returns 255 for an intentional SIGTERM even after writing a valid trailer.
  if [[ "$thermal_was_running" == true ]] &&
     [[ -s "$partial_dir/.thermal.partial/thermal.nut" ]] &&
     ffprobe -v error -show_entries stream=width,height -of csv=p=0 \
       "$partial_dir/.thermal.partial/thermal.nut" >/dev/null 2>&1; then
    thermal_status="recorded"
  else
    thermal_status="interrupted"
    log "Thermal recording interrupted; stereo session remains valid (logs/thermal.log)."
  fi
fi
if [[ "$IMU_ENABLED" == true ]]; then
  imu_status=0
  kill -TERM "$imu_pid" 2>/dev/null || true
  wait "$imu_pid" || imu_status=$?
  imu_pid=''
  (( imu_status == 0 )) || die "IMU-Aufzeichnung fehlgeschlagen (Status $imu_status); siehe logs/imu.log."
  [[ -s "$partial_dir/imu.csv" ]] || die "IMU-Datei ist leer."
  (( $(wc -l < "$partial_dir/imu.csv") > 1 )) || die "IMU-Datei enthält keine Messwerte."
fi
if [[ -n "$light_pid" ]]; then
  light_exit=0
  if kill -0 "$light_pid" 2>/dev/null; then
    kill -TERM "$light_pid" 2>/dev/null || true
  fi
  wait "$light_pid" || light_exit=$?
  light_pid=''
  if [[ -s "$partial_dir/light.csv" ]] && (( $(wc -l < "$partial_dir/light.csv") > 1 )); then
    if (( light_exit == 0 )); then
      light_status="recorded"
    else
      light_status="interrupted"
      log "BH1750 stopped early; partial light.csv retained (logs/light.log)."
    fi
  else
    light_status="unavailable"
  fi
fi

finished_at_utc_ns="$(date +%s%N)"
duration_json="$(awk -v start="$started_at_utc_ns" -v finish="$finished_at_utc_ns" 'BEGIN { printf "%.3f", (finish - start) / 1000000000 }')"
python3 "$SCRIPT_DIR/frame_sync.py" capture "$partial_dir" "$session_id" \
  "$started_at_utc_ns" "$finished_at_utc_ns" \
  "$LEFT_CAMERA_INDEX" "$RIGHT_CAMERA_INDEX" \
  "$VIDEO_WIDTH" "$VIDEO_HEIGHT" "$VIDEO_FPS" "$VIDEO_BITRATE" \
  "$duration_json" "$IMU_ENABLED" "$THERMAL_ENABLED" "$thermal_status" "$THERMAL_DEVICE" \
  "$LIGHT_ENABLED" "$light_status" "$LIGHT_I2C_BUS" "$LIGHT_ADDRESS" "$LIGHT_INTERVAL_SECONDS" || die "Kamera-Zeitstempel fehlen oder sind keine plausible UTC; siehe Kamera-Logs."
if [[ "$stop_requested" == true ]]; then
  python3 - "$partial_dir/capture.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data['stopped_early'] = True
data['stop_reason'] = 'requested'
path.write_text(json.dumps(data, indent=2) + '\n')
PY
fi
touch "$partial_dir/.capture_complete"
mv -- "$partial_dir" "$session_dir"
trap - EXIT INT TERM
log "Aufnahme abgeschlossen: $session_dir"
printf '%s\n' "$session_dir"
