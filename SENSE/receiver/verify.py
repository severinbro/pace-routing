#!/usr/bin/env python3
"""Session verification module for SENSE receiver.

Validates extracted HALO session directories against Protocol v1 invariants:
- Mandatory capture and sync manifests
- IMU data format integrity
- SHA256SUMS cryptographic verification
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("halo-verify")

MANDATORY_FILES = (
    "capture.json",
    "pairs.csv",
    "frames.csv",
    "sync_report.json",
    "SHA256SUMS",
    ".capture_complete",
    ".extraction_complete",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_manifest(session_dir: Path) -> Tuple[bool, str]:
    """Verify all files against the SHA256SUMS file in the session."""
    sums_file = session_dir / "SHA256SUMS"
    if not sums_file.is_file():
        return False, "SHA256SUMS fehlt in der Session"

    manifest_lines = sums_file.read_text(encoding="utf-8").splitlines()
    if not manifest_lines:
        return False, "SHA256SUMS ist leer"

    checked_count = 0
    for line_idx, line in enumerate(manifest_lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            parts = line.split(" ", 1)
        if len(parts) != 2:
            return False, f"Ungültiges Format in SHA256SUMS Zeile {line_idx}"

        expected_hash, rel_path = parts[0].strip(), parts[1].strip()
        target_file = (session_dir / rel_path).resolve()
        
        # Security: protect against path traversal inside SHA256SUMS
        if not target_file.is_relative_to(session_dir.resolve()):
            return False, f"Dateipfad außerhalb der Session in Zeile {line_idx}: {rel_path}"
        if not target_file.is_file():
            return False, f"Datei in SHA256SUMS existiert nicht: {rel_path}"

        actual_hash = sha256_file(target_file)
        if actual_hash.lower() != expected_hash.lower():
            return False, f"Prüfsummen-Abweichung bei {rel_path}: erwartet {expected_hash}, erhalten {actual_hash}"
        checked_count += 1

    return True, f"Integrität von {checked_count} Dateien erfolgreich verifiziert"


def verify_session_structure(session_dir: Path) -> Tuple[bool, str]:
    """Verify that all required HALO session files and stereo frames exist."""
    session_dir = session_dir.resolve()
    if not session_dir.is_dir():
        return False, f"Session-Verzeichnis nicht gefunden: {session_dir}"

    for mandatory in MANDATORY_FILES:
        path = session_dir / mandatory
        if not path.is_file():
            return False, f"Pflichtdatei fehlt: {mandatory}"

    # Verify stereo frames exist
    for side in ("left", "right"):
        side_dir = session_dir / "frames" / side
        if not side_dir.is_dir():
            return False, f"Frame-Ordner fehlt: frames/{side}"
        frames = list(side_dir.glob("frame_*.jpg"))
        if not frames:
            return False, f"Keine JPEGs in frames/{side} vorhanden"

    # Verify capture.json schema and session ID
    try:
        capture_meta = json.loads((session_dir / "capture.json").read_text(encoding="utf-8"))
        if not isinstance(capture_meta, dict):
            return False, "capture.json ist kein gültiges JSON-Objekt"
        if capture_meta.get("schema_version") not in (1, 2):
            return False, f"Nicht unterstützte capture.json schema_version: {capture_meta.get('schema_version')}"
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"Fehler beim Parsen von capture.json: {exc}"

    # Verify imu.csv if present
    imu_file = session_dir / "imu.csv"
    if imu_file.exists():
        if not imu_file.is_file():
            return False, "imu.csv ist keine reguläre Datei"
        with imu_file.open(encoding="utf-8") as f:
            first_line = f.readline()
        if not first_line.startswith("utc_ns,"):
            return False, "imu.csv Header-Format ungültig (erwartet Spalte utc_ns)"

    # Verify SHA256SUMS against extracted files
    ok, msg = verify_manifest(session_dir)
    if not ok:
        return False, msg

    return True, "Session ist vollständig und integer"


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python3 verify.py <SESSION_DIR>")
        sys.exit(1)
    target = Path(sys.argv[1])
    is_valid, reason = verify_session_structure(target)
    if is_valid:
        print(f"OK: {reason}")
        sys.exit(0)
    else:
        print(f"FEHLER: {reason}", file=sys.stderr)
        sys.exit(1)
