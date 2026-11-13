#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${PACE_CONFIG:-/etc/halo-camera/capture.env}"

if [[ ! -r "$CONFIG_FILE" ]]; then
  CONFIG_FILE="${PACKAGE_ROOT}/config/capture.env"
fi
if [[ ! -r "$CONFIG_FILE" ]]; then
  echo "Configuration missing. Copy config/capture.env.example to config/capture.env or mount /etc/halo-camera/capture.env." >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

log() {
  printf '%s [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "${0##*/}" "$*" >&2
}

die() {
  log "FEHLER: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Benötigtes Programm nicht gefunden: $1"
}

require_uint() {
  local name="$1" value="${!1:-}"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name muss eine nichtnegative ganze Zahl sein."
}

require_positive_number() {
  local name="$1" value="${!1:-}"
  [[ "$value" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || die "$name muss eine positive Zahl sein."
  awk -v value="$value" 'BEGIN { exit !(value > 0) }' || die "$name muss größer als 0 sein."
}

ensure_data_dirs() {
  : "${DATA_ROOT:?DATA_ROOT fehlt in der Konfiguration}"
  mkdir -p -- "$DATA_ROOT/sessions"
}

load_camera_args() {
  read -r -a CAMERA_COMMON_ARGV <<< "${CAMERA_COMMON_ARGS:-}"
  read -r -a LEFT_CAMERA_ARGV <<< "${LEFT_CAMERA_ARGS:-}"
  read -r -a RIGHT_CAMERA_ARGV <<< "${RIGHT_CAMERA_ARGS:-}"
}
