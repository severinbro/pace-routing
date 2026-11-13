#!/usr/bin/env python3
"""Queue and transfer completed HALO sessions to the SENSE receiver."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import tarfile
import tempfile
import time


LOG = logging.getLogger("halo_upload")
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
REQUIRED_FILES = (
    "capture.json", "frames.csv", "pairs.csv", "sync_report.json",
    ".capture_complete", ".extraction_complete",
)


class UploadError(RuntimeError):
    """Upload failed or the receiver did not confirm a committed session."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def transfer_files(session_dir: Path, include_video: bool = False) -> list[Path]:
    session_dir = session_dir.resolve(strict=True)
    if not SESSION_RE.fullmatch(session_dir.name):
        raise ValueError(f"Invalid session ID: {session_dir.name}")
    if not (session_dir / ".capture_complete").is_file():
        raise ValueError("Session capture is not complete")
    if not (session_dir / ".extraction_complete").is_file():
        raise ValueError("Session extraction is not complete")

    files = []
    for relative in REQUIRED_FILES:
        path = session_dir / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Required regular file is missing: {relative}")
        files.append(path)
    imu_path = session_dir / "imu.csv"
    if imu_path.is_file() and not imu_path.is_symlink():
        files.append(imu_path)
    light_path = session_dir / "light.csv"
    if light_path.is_file() and not light_path.is_symlink() and light_path.stat().st_size:
        files.append(light_path)
    capture_meta = json.loads((session_dir / "capture.json").read_text(encoding="utf-8"))
    if capture_meta.get("thermal_enabled"):
        for relative in ("thermal.csv", "thermal.json"):
            path = session_dir / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Thermal manifest missing: {relative}")
            files.append(path)
        with (session_dir / "thermal.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        with (session_dir / "pairs.csv").open(newline="") as stream:
            pair_count = sum(1 for _ in csv.DictReader(stream))
        if len(rows) != pair_count:
            raise ValueError("Thermal manifest row count does not match stereo pairs")
        for row in rows:
            present = row.get("thermal_status") in ("ok", "skew")
            has_color_column = "color_file" in row
            if (present != bool(row.get("raw_file")) or present != bool(row.get("preview_file"))
                    or (has_color_column and present != bool(row.get("color_file")))):
                raise ValueError("Thermal row status and files disagree")
            for key in ("raw_file", "preview_file", "color_file"):
                relative = row.get(key, "")
                if not relative:
                    continue
                pattern = (r"frames/thermal/frame_[0-9]{6}_color\.png" if key == "color_file"
                           else r"frames/thermal/frame_[0-9]{6}\.(raw|png)")
                if not re.fullmatch(pattern, relative):
                    raise ValueError(f"Invalid thermal file path: {relative}")
                path = session_dir / relative
                if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                    raise ValueError(f"Thermal file missing or empty: {relative}")
                if key == "raw_file" and path.stat().st_size != 256 * 192 * 2:
                    raise ValueError(f"Thermal grid has wrong size: {relative}")
                files.append(path)
    for side in ("left", "right"):
        frames_dir = session_dir / "frames" / side
        images = sorted(frames_dir.glob("frame_*.jpg"))
        if not images:
            raise ValueError(f"No extracted JPEG frames in frames/{side}")
        for image in images:
            if image.is_symlink() or not image.is_file() or image.stat().st_size == 0:
                raise ValueError(f"Invalid image file: {image.relative_to(session_dir)}")
            files.append(image)
    if include_video:
        for side in ("left", "right"):
            video = session_dir / "video" / f"{side}.mkv"
            if not video.is_file() or video.is_symlink():
                raise ValueError(f"Video missing: {video.relative_to(session_dir)}")
            files.append(video)
    return sorted(files, key=lambda path: path.relative_to(session_dir).as_posix())


def build_transfer_manifest(session_dir: Path, files: list[Path]) -> bytes:
    lines = [f"{sha256_file(path)}  {path.relative_to(session_dir).as_posix()}\n" for path in files]
    return "".join(lines).encode("utf-8")


def prepare_archive(session_dir: Path, outbox: Path, include_video: bool = False) -> tuple[Path, str]:
    """Create the receiver-compatible TAR and its archive SHA256.

    Videos are omitted by default. SHA256SUMS is regenerated over exactly the
    files in the TAR, since the source session manifest also covers local-only
    videos.
    """
    session_dir = session_dir.resolve(strict=True)
    files = transfer_files(session_dir, include_video=include_video)
    manifest = build_transfer_manifest(session_dir, files)
    outbox.mkdir(parents=True, exist_ok=True)
    archive_path = outbox / f"{session_dir.name}.tar"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{session_dir.name}.", suffix=".partial", dir=outbox)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with tarfile.open(temporary_path, mode="w", format=tarfile.PAX_FORMAT) as bundle:
            for path in files:
                relative = path.relative_to(session_dir).as_posix()
                info = bundle.gettarinfo(str(path), arcname=relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as stream:
                    bundle.addfile(info, stream)
            manifest_info = tarfile.TarInfo("SHA256SUMS")
            manifest_info.size = len(manifest)
            manifest_info.mode = 0o644
            manifest_info.uid = manifest_info.gid = 0
            manifest_info.uname = manifest_info.gname = ""
            manifest_info.mtime = 0
            bundle.addfile(manifest_info, io.BytesIO(manifest))
        os.replace(temporary_path, archive_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return archive_path, sha256_file(archive_path)


class VerifiedTLSConnection(http.client.HTTPSConnection):
    """Connect to a configured host/IP while validating SENSE's certificate name."""

    def __init__(self, connect_host: str, tls_name: str, port: int, context: ssl.SSLContext, timeout: int):
        super().__init__(connect_host, port=port, timeout=timeout, context=context)
        self.tls_name = tls_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            sock = self.sock
        self.sock = self._context.wrap_socket(sock, server_hostname=self.tls_name)


def upload_archive(archive_path: Path, session_id: str, archive_sha256: str) -> tuple[int, dict]:
    connect_host = os.environ.get("SENSE_UPLOAD_HOST", "pace-sense.local").strip()
    tls_name = os.environ.get("SENSE_TLS_NAME", "pace-sense.local").strip()
    port = int(os.environ.get("SENSE_HTTPS_PORT", "443"))
    ca_file = Path(os.environ.get("SENSE_CA_FILE", "/run/secrets/sense-ca.crt"))
    token_file = Path(os.environ.get("SENSE_TOKEN_FILE", "/run/secrets/halo-upload-token"))
    timeout = int(os.environ.get("SENSE_UPLOAD_TIMEOUT_SECONDS", "600"))
    if not connect_host or not tls_name or not 1 <= port <= 65535:
        raise ValueError("SENSE upload host, TLS name, or port configuration is invalid")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"Upload token is empty: {token_file}")
    context = ssl.create_default_context(cafile=str(ca_file))
    connection = VerifiedTLSConnection(connect_host, tls_name, port, context, timeout)
    path = f"/halo-upload/v1/sessions/{session_id}"
    host_header = tls_name if port == 443 else f"{tls_name}:{port}"
    headers = {
        "Host": host_header,
        "Authorization": f"Bearer {token}",
        "X-HALO-Protocol-Version": "1",
        "X-HALO-Session-ID": session_id,
        "X-HALO-Archive-SHA256": archive_sha256,
        "Content-Type": "application/x-tar",
        "Content-Length": str(archive_path.stat().st_size),
        "Connection": "close",
    }
    try:
        with archive_path.open("rb") as body:
            connection.request("PUT", path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(64 * 1024)
        if response.status not in (200, 201):
            raise UploadError(f"SENSE returned HTTP {response.status}: {response_body[:1000].decode('utf-8', 'replace')}")
        try:
            receipt = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise UploadError("SENSE returned a non-JSON success response") from exc
        if (receipt.get("state") != "committed"
                or receipt.get("session_id") != session_id
                or receipt.get("archive_sha256", "").lower() != archive_sha256.lower()):
            raise UploadError("SENSE success receipt does not match the uploaded session and archive hash")
        return response.status, receipt
    except (OSError, ssl.SSLError, http.client.HTTPException, socket.timeout) as exc:
        raise UploadError(f"Could not reach SENSE receiver: {exc}") from exc
    finally:
        connection.close()


def commit_local_receipt(session_dir: Path, receipt: dict, archive_sha256: str, status: int = 201) -> None:
    local_receipt = {
        **receipt,
        "archive_sha256": archive_sha256,
        "http_status": status,
        "confirmed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = session_dir / ".transfer_complete.partial"
    temporary.write_text(json.dumps(local_receipt, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, session_dir / ".transfer_complete")
    (session_dir / ".transfer_pending").unlink(missing_ok=True)


def process_session(session_dir: Path, data_root: Path, include_video: bool = False) -> None:
    if (session_dir / ".transfer_complete").is_file():
        (session_dir / ".transfer_pending").unlink(missing_ok=True)
        return
    archive_path = data_root / "outbox" / f"{session_dir.name}.tar"
    archive_path, digest = prepare_archive(session_dir, archive_path.parent, include_video=include_video)
    status, receipt = upload_archive(archive_path, session_dir.name, digest)
    commit_local_receipt(session_dir, receipt, digest, status=status)
    archive_path.unlink(missing_ok=True)
    LOG.info("Transfer committed for %s (%s)", session_dir.name, digest)


def worker(data_root: Path, scan_seconds: int, include_video: bool) -> int:
    if scan_seconds < 1:
        raise ValueError("Upload scan interval must be at least one second")
    LOG.info("SENSE uploader started; host=%s tls_name=%s port=%s",
             os.environ.get("SENSE_UPLOAD_HOST", "pace-sense.local"),
             os.environ.get("SENSE_TLS_NAME", "pace-sense.local"),
             os.environ.get("SENSE_HTTPS_PORT", "443"))
    while True:
        sessions_dir = data_root / "sessions"
        for session_dir in sorted(sessions_dir.iterdir()) if sessions_dir.is_dir() else []:
            if not session_dir.is_dir() or not (session_dir / ".transfer_pending").is_file():
                continue
            if not (session_dir / ".extraction_complete").is_file():
                continue
            try:
                process_session(session_dir, data_root, include_video=include_video)
            except Exception as exc:  # keep durable pending marker and retry on the next scan
                LOG.error("Transfer attempt for %s failed; will retry: %s", session_dir.name, exc)
                time.sleep(min(scan_seconds, 30))
        time.sleep(scan_seconds)


def request_session(data_root: Path, session_id: str) -> int:
    if not SESSION_RE.fullmatch(session_id):
        raise ValueError(f"Invalid session ID: {session_id}")
    session = (data_root / "sessions" / session_id).resolve(strict=True)
    if session.parent != (data_root / "sessions").resolve():
        raise ValueError("Session must be a direct child of DATA_ROOT/sessions")
    if not (session / ".extraction_complete").is_file():
        raise ValueError("Only fully extracted sessions can be queued for transfer")
    if (session / ".transfer_complete").is_file():
        LOG.info("Session already has a transfer receipt: %s", session_id)
        return 0
    (session / ".transfer_pending").touch(exist_ok=True)
    LOG.info("Queued session for SENSE transfer: %s", session_id)
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("worker", "request"), nargs="?", default="worker")
    parser.add_argument("session_id", nargs="?")
    args = parser.parse_args()
    data_root = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
    include_video = os.environ.get("UPLOAD_INCLUDE_VIDEO", "false").lower() == "true"
    try:
        if args.command == "request":
            if not args.session_id:
                parser.error("request requires SESSION_ID")
            return request_session(data_root, args.session_id)
        return worker(data_root, int(os.environ.get("UPLOAD_SCAN_SECONDS", "10")), include_video)
    except (OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
