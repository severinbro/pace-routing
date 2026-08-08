#!/bin/bash
# Generates a self-signed TLS certificate for the PACE Pi hotspot.
# Run this on the Pi before starting the containers:
#   bash generate_cert.sh
#
# The browser will show a "not secure" warning on first visit — the admin
# must click "Advanced → Proceed" once. After that the Geolocation API
# will work because the origin is a secure context (HTTPS).

set -e

CERT_DIR="nginx/certs"
mkdir -p "$CERT_DIR"

echo "Generating self-signed certificate..."

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/pace.key" \
    -out "$CERT_DIR/pace.crt" \
    -days 3650 \
    -subj "/C=DE/O=PACE/CN=10.42.0.1" \
    -addext "subjectAltName=IP:10.42.0.1"

echo "Done. Certificate valid for 10 years."
echo "  Certificate: $CERT_DIR/pace.crt"
echo "  Private key:  $CERT_DIR/pace.key"
