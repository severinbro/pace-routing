#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="$SCRIPT_DIR/certs"
mkdir -p "$CERTS_DIR"

CRT="$CERTS_DIR/server.crt"
KEY="$CERTS_DIR/server.key"

if [[ -f "$CRT" && -f "$KEY" ]]; then
  echo "Zertifikate existieren bereits in $CERTS_DIR:"
  ls -lh "$CRT" "$KEY"
  exit 0
fi

echo "Erzeuge selbstsigniertes TLS-Zertifikat für SENSE (pace-sense.local)..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout "$KEY" \
  -out "$CRT" \
  -subj "/CN=pace-sense.local/O=PACE/OU=SENSE" \
  -addext "subjectAltName = DNS:pace-sense.local,DNS:localhost,IP:127.0.0.1,IP:10.42.10.1"

chmod 600 "$KEY"
chmod 644 "$CRT"
echo "Fertig. Zertifikat: $CRT, Schlüssel: $KEY"
