#!/usr/bin/env python3
"""SENSE HALO Protocol v1 Upload Receiver Service.

Implements the server endpoint for receiving head-mounted stereo camera and IMU
sessions from HALO over HTTP/HTTPS:
- PUT /halo-upload/v1/sessions/<SESSION_ID>
- GET /healthz
"""

from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tarfile
import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from verify import verify_session_structure

LOG = logging.getLogger("halo-receiver")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")


def write_progress(root: Path, session_id: str, state: str, received: int,
                   expected: int, started: float, error: str = "") -> None:
    """Publish a small atomic snapshot for the local dashboard."""
    metrics = root / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    now = time.time()
    elapsed = max(now - started, 0.001)
    previous_path = metrics / f"{session_id}.json"
    try:
        previous = json.loads(previous_path.read_text())
    except (OSError, ValueError):
        previous = {}
    if previous.get("state") in ("receiving", "verifying") and received >= previous.get("bytes_received", 0):
        interval = max(now - previous.get("updated_at", now), 0.001)
        current_rate = (received - previous.get("bytes_received", 0)) / interval
    else:
        current_rate = 0.0
    payload = {"session_id": session_id, "state": state,
               "bytes_received": received, "bytes_expected": expected,
               "elapsed_seconds": elapsed, "bytes_per_second": current_rate,
               "average_bytes_per_second": received / elapsed,
               "updated_at": now, "error": error}
    temporary = metrics / f".{session_id}.json.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(metrics / f"{session_id}.json")

# Global locks to ensure atomicity for incoming uploads
SESSION_LOCKS = {}
SESSION_LOCKS_GUARD = threading.Lock()


def get_session_lock(session_id: str) -> threading.Lock:
    with SESSION_LOCKS_GUARD:
        if session_id not in SESSION_LOCKS:
            SESSION_LOCKS[session_id] = threading.Lock()
        return SESSION_LOCKS[session_id]


def load_token(token_file: Path) -> Optional[str]:
    if not token_file.is_file():
        LOG.warning("Token file does not exist: %s", token_file)
        return None
    content = token_file.read_text(encoding="utf-8").strip()
    return content if content else None


class ReceiverHandler(BaseHTTPRequestHandler):
    server_version = "HALO-SENSE-Receiver/1.0"

    @property
    def receiver_root(self) -> Path:
        return Path(os.environ.get("RECEIVER_ROOT", "/data")).resolve()

    @property
    def token_path(self) -> Path:
        return Path(os.environ.get("UPLOAD_TOKEN_FILE", "/config/upload.token")).resolve()

    @property
    def max_upload_bytes(self) -> int:
        return int(os.environ.get("MAX_UPLOAD_BYTES", "0"))

    def send_json_response(self, status: int, data: dict):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str):
        LOG.warning("Client error (%d): %s", status, message)
        self.send_json_response(status, {"error": message, "status": status})

    def authenticate(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Missing or invalid Authorization header (Bearer required)")
            return False

        bearer_token = auth_header[7:].strip()
        expected_token = load_token(self.token_path)
        if not expected_token:
            LOG.error("Server token not configured at %s", self.token_path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Server authentication not configured")
            return False

        if bearer_token != expected_token:
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid authentication token")
            return False

        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json_response(HTTPStatus.OK, {
                "status": "healthy",
                "service": "sense-receiver",
                "protocol_version": 1,
                "utc": datetime.now(timezone.utc).isoformat()
            })
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, f"Path not found: {parsed.path}")

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Match /halo-upload/v1/sessions/<SESSION_ID>
        prefix = "/halo-upload/v1/sessions/"
        if not path.startswith("/halo-upload/v1/sessions/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, f"Endpoint not found: {path}")
            return

        session_id = path[len(prefix):]
        if not session_id or not SESSION_NAME_RE.match(session_id):
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid session ID: '{session_id}'")
            return

        if not self.authenticate():
            return

        # Protocol header checks
        protocol_ver = self.headers.get("X-HALO-Protocol-Version")
        if protocol_ver != "1":
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"Unsupported protocol version: {protocol_ver}")
            return

        header_session = self.headers.get("X-HALO-Session-ID")
        if header_session and header_session != session_id:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"X-HALO-Session-ID ({header_session}) does not match URL ({session_id})")
            return

        expected_sha256 = (self.headers.get("X-HALO-Archive-SHA256") or "").lower()
        if not SHA256_HEX_RE.match(expected_sha256):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Missing or invalid X-HALO-Archive-SHA256 header")
            return

        content_length_str = self.headers.get("Content-Length")
        if not content_length_str or not content_length_str.isdigit():
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length header required")
            return

        content_length = int(content_length_str)
        if self.max_upload_bytes > 0 and content_length > self.max_upload_bytes:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"Upload exceeds maximum allowed size ({self.max_upload_bytes} bytes)")
            return

        # Handle upload with session-level lock for concurrency safety
        lock = get_session_lock(session_id)
        with lock:
            self.handle_session_upload(session_id, expected_sha256, content_length)

    def handle_session_upload(self, session_id: str, expected_sha256: str, content_length: int):
        root = self.receiver_root
        staging_dir = root / "staging"
        incoming_dir = root / "incoming"
        staging_dir.mkdir(parents=True, exist_ok=True)
        incoming_dir.mkdir(parents=True, exist_ok=True)

        existing_incoming = incoming_dir / session_id
        if existing_incoming.is_dir():
            receipt_file = existing_incoming / ".transfer_complete"
            if receipt_file.is_file():
                try:
                    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                    stored_sha = receipt.get("archive_sha256", "").lower()
                    # Nginx streams request bodies to this server. Read the full retry
                    # before responding so the proxy can finish forwarding it.
                    retry_hash = hashlib.sha256()
                    remaining = content_length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            self.send_error_json(HTTPStatus.BAD_REQUEST, "Incomplete upload body")
                            return
                        retry_hash.update(chunk)
                        remaining -= len(chunk)
                    if retry_hash.hexdigest() != expected_sha256:
                        self.send_error_json(HTTPStatus.BAD_REQUEST, "Archive SHA-256 digest mismatch")
                        return
                    if stored_sha == expected_sha256:
                        LOG.info("Session %s already committed with matching hash. Returning 200 OK (idempotent).", session_id)
                        self.send_json_response(HTTPStatus.OK, {
                            "state": "committed",
                            "session_id": session_id,
                            "archive_sha256": stored_sha
                        })
                        return
                    else:
                        LOG.warning("Session %s exists with different hash (%s != %s).", session_id, stored_sha, expected_sha256)
                        self.send_error_json(HTTPStatus.CONFLICT, f"Session {session_id} already exists with different content")
                        return
                except (json.JSONDecodeError, OSError) as exc:
                    LOG.error("Failed reading existing receipt for %s: %s", session_id, exc)

        # Stream payload to staging/.<session_id>.tar.partial while computing sha256
        partial_tar = staging_dir / f".{session_id}.tar.partial"
        archive_tar = staging_dir / f"{session_id}.tar"
        target_extract_dir = staging_dir / session_id

        hasher = hashlib.sha256()
        bytes_read = 0
        chunk_size = 1024 * 1024  # 1MB buffer
        started = time.time()
        last_progress = 0.0
        write_progress(root, session_id, "receiving", 0, content_length, started)

        LOG.info("Receiving session %s (%d bytes expected)...", session_id, content_length)
        try:
            with partial_tar.open("wb") as out_file:
                while bytes_read < content_length:
                    remaining = content_length - bytes_read
                    to_read = min(chunk_size, remaining)
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        raise IOError(f"Premature end of stream after {bytes_read}/{content_length} bytes")
                    out_file.write(chunk)
                    hasher.update(chunk)
                    bytes_read += len(chunk)
                    if time.time() - last_progress >= 1:
                        write_progress(root, session_id, "receiving", bytes_read, content_length, started)
                        last_progress = time.time()
                out_file.flush()
                os.fsync(out_file.fileno())

            computed_sha = hasher.hexdigest().lower()
            if computed_sha != expected_sha256:
                LOG.error("Archive digest mismatch for %s: computed %s, expected %s", session_id, computed_sha, expected_sha256)
                partial_tar.unlink(missing_ok=True)
                write_progress(root, session_id, "failed", bytes_read, content_length, started, "Archive digest mismatch")
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Archive SHA-256 digest mismatch")
                return

            partial_tar.replace(archive_tar)
            write_progress(root, session_id, "verifying", bytes_read, content_length, started)

            # Unpack TAR securely
            if target_extract_dir.exists():
                shutil.rmtree(target_extract_dir)
            target_extract_dir.mkdir(parents=True, exist_ok=True)

            LOG.info("Extracting archive %s to staging...", session_id)
            with tarfile.open(archive_tar, "r") as bundle:
                for member in bundle.getmembers():
                    # Security checks: prevent Zip-Slip / Path Traversal
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Unsafe path in TAR: {member.name}")
                    if member.issym() or member.islnk():
                        raise ValueError(f"Symlinks/hardlinks forbidden in TAR: {member.name}")
                    if not (member.isreg() or member.isdir()):
                        raise ValueError(f"Unsupported file type in TAR: {member.name}")
                    dest = (target_extract_dir / member_path).resolve()
                    if not dest.is_relative_to(target_extract_dir.resolve()):
                        raise ValueError(f"Path escapes extraction directory: {member.name}")
                    bundle.extract(member, target_extract_dir)

            # Clean up TAR file after unpacking
            archive_tar.unlink(missing_ok=True)

            # Verify session structure & cryptographic manifest
            is_valid, reason = verify_session_structure(target_extract_dir)
            if not is_valid:
                LOG.error("Session %s failed verification: %s", session_id, reason)
                shutil.rmtree(target_extract_dir, ignore_errors=True)
                write_progress(root, session_id, "failed", bytes_read, content_length, started, reason[:200])
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"Session verification failed: {reason}")
                return

            # Atomic commit: write .transfer_complete receipt and move to incoming/
            receipt = {
                "state": "committed",
                "session_id": session_id,
                "archive_sha256": computed_sha,
                "transferred_at_utc": datetime.now(timezone.utc).isoformat()
            }
            receipt_file = target_extract_dir / ".transfer_complete"
            receipt_file.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

            # Move from staging to incoming
            final_target = incoming_dir / session_id
            if final_target.exists():
                shutil.rmtree(final_target)
            target_extract_dir.replace(final_target)

            LOG.info("Session %s successfully committed to %s", session_id, final_target)
            write_progress(root, session_id, "committed", bytes_read, content_length, started)
            self.send_json_response(HTTPStatus.CREATED, {
                "state": "committed",
                "session_id": session_id,
                "archive_sha256": computed_sha
            })

        except Exception as exc:
            LOG.exception("Unexpected error processing upload for %s: %s", session_id, exc)
            write_progress(root, session_id, "failed", bytes_read, content_length, started, str(exc)[:200])
            partial_tar.unlink(missing_ok=True)
            archive_tar.unlink(missing_ok=True)
            if target_extract_dir.exists():
                shutil.rmtree(target_extract_dir, ignore_errors=True)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal server error: {exc}")


def run_server():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8000"))
    server_address = (host, port)

    root = Path(os.environ.get("RECEIVER_ROOT", "/data")).resolve()
    for subdir in ("staging", "incoming", "processing", "completed", "failed"):
        (root / subdir).mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer(server_address, ReceiverHandler)
    LOG.info("Starting SENSE Receiver on %s:%d (data root: %s)", host, port, root)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down receiver...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
