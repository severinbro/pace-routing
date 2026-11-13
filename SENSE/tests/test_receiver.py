#!/usr/bin/env python3
"""Integration and protocol conformance tests for the SENSE Receiver."""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import sys
RECEIVER_DIR = Path(__file__).resolve().parents[1] / "receiver"
sys.path.insert(0, str(RECEIVER_DIR))

from server import ReceiverHandler


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_test_session_files(session_dir: Path, include_imu: bool = True) -> dict:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "frames" / "left").mkdir(parents=True, exist_ok=True)
    (session_dir / "frames" / "right").mkdir(parents=True, exist_ok=True)

    files = {
        "capture.json": b'{"schema_version": 2, "session_id": "test", "fps": 30}',
        "pairs.csv": b"pair_index,left_file,right_file,left_capture_utc_ns,right_capture_utc_ns\n0,frames/left/frame_000000.jpg,frames/right/frame_000000.jpg,1000,1000\n",
        "frames.csv": b"side,frame_index,pts_seconds,capture_utc_ns,file_path\nleft,0,0.0,1000,frames/left/frame_000000.jpg\nright,0,0.0,1000,frames/right/frame_000000.jpg\n",
        "sync_report.json": b'{"unpaired_count": 0, "status": "ok"}',
        "frames/left/frame_000000.jpg": b"fake-left-jpg",
        "frames/right/frame_000000.jpg": b"fake-right-jpg",
        ".capture_complete": b"",
        ".extraction_complete": b"",
    }
    if include_imu:
        files["imu.csv"] = (
            b"utc_ns,monotonic_ns,accel_x_m_s2,accel_y_m_s2,accel_z_m_s2,gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s,quat_i,quat_j,quat_k,quat_real,read_start_utc_ns,read_end_utc_ns,read_duration_ns\n"
            b"1000,2000,0.1,9.8,-0.1,0.0,0.0,0.0,0.0,0.0,0.0,1.0,990,1010,20\n"
        )

    for rel_path, data in files.items():
        p = session_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    manifest_lines = []
    for rel_path in sorted(files.keys()):
        h = sha256_bytes(files[rel_path])
        manifest_lines.append(f"{h}  {rel_path}\n")
    manifest_data = "".join(manifest_lines).encode("utf-8")
    (session_dir / "SHA256SUMS").write_bytes(manifest_data)
    files["SHA256SUMS"] = manifest_data

    return files


def pack_tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(files.keys()):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.data_root = Path(cls.temp_dir.name) / "data"
        cls.token_file = Path(cls.temp_dir.name) / "upload.token"
        cls.token = "secret-token-12345"
        cls.token_file.write_text(cls.token, encoding="utf-8")

        os.environ["RECEIVER_ROOT"] = str(cls.data_root)
        os.environ["UPLOAD_TOKEN_FILE"] = str(cls.token_file)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ReceiverHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp_dir.cleanup()

    def setUp(self):
        # Clear data directory between tests
        if self.data_root.exists():
            shutil.rmtree(self.data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)

    def request(self, method: str, path: str, body: bytes = b"", headers: dict = None):
        h = headers or {}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            res = conn.getresponse()
            data = res.read()
            return res.status, res.headers, data
        finally:
            conn.close()

    def test_healthz(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "sense-receiver")

    def test_unauthorized_missing_token(self):
        status, _, body = self.request("PUT", "/halo-upload/v1/sessions/session-1", body=b"test")
        self.assertEqual(status, 401)

    def test_unauthorized_wrong_token(self):
        headers = {"Authorization": "Bearer wrong-token"}
        status, _, body = self.request("PUT", "/halo-upload/v1/sessions/session-1", body=b"test", headers=headers)
        self.assertEqual(status, 401)

    def test_invalid_protocol_version(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-HALO-Protocol-Version": "2",
            "X-HALO-Archive-SHA256": "0" * 64,
            "Content-Length": "4"
        }
        status, _, _ = self.request("PUT", "/halo-upload/v1/sessions/session-1", body=b"test", headers=headers)
        self.assertEqual(status, 400)

    def test_hash_mismatch_rejected(self):
        payload = b"dummy-tar-bytes"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-HALO-Protocol-Version": "1",
            "X-HALO-Session-ID": "session-1",
            "X-HALO-Archive-SHA256": "0" * 64,
            "Content-Type": "application/x-tar",
            "Content-Length": str(len(payload))
        }
        status, _, body = self.request("PUT", "/halo-upload/v1/sessions/session-1", body=payload, headers=headers)
        self.assertEqual(status, 400)
        # Verify no files in incoming or staging
        self.assertFalse((self.data_root / "incoming" / "session-1").exists())
        self.assertFalse((self.data_root / "staging" / "session-1.tar").exists())
        progress = json.loads((self.data_root / "metrics" / "session-1.json").read_text())
        self.assertEqual(progress["state"], "failed")
        self.assertEqual(progress["bytes_received"], len(payload))

    def test_path_traversal_tar_rejected(self):
        # Create a malicious TAR with ../evil.txt
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))
        payload = buf.getvalue()
        sha = sha256_bytes(payload)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-HALO-Protocol-Version": "1",
            "X-HALO-Session-ID": "session-evil",
            "X-HALO-Archive-SHA256": sha,
            "Content-Type": "application/x-tar",
            "Content-Length": str(len(payload))
        }
        status, _, body = self.request("PUT", "/halo-upload/v1/sessions/session-evil", body=payload, headers=headers)
        self.assertEqual(status, 500)
        self.assertFalse((self.data_root / "evil.txt").exists())

    def test_valid_session_upload_and_idempotency(self):
        with tempfile.TemporaryDirectory() as s_dir:
            s_path = Path(s_dir) / "session-valid-100"
            files = create_test_session_files(s_path, include_imu=True)
            tar_bytes = pack_tar(files)
            sha = sha256_bytes(tar_bytes)

        session_id = "session-valid-100"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-HALO-Protocol-Version": "1",
            "X-HALO-Session-ID": session_id,
            "X-HALO-Archive-SHA256": sha,
            "Content-Type": "application/x-tar",
            "Content-Length": str(len(tar_bytes))
        }

        # 1. First upload: Expect 201 Created
        status, _, body = self.request("PUT", f"/halo-upload/v1/sessions/{session_id}", body=tar_bytes, headers=headers)
        self.assertEqual(status, 201)
        receipt = json.loads(body)
        self.assertEqual(receipt["state"], "committed")
        self.assertEqual(receipt["session_id"], session_id)
        self.assertEqual(receipt["archive_sha256"], sha)

        # Verify files on disk in incoming/
        incoming_session = self.data_root / "incoming" / session_id
        self.assertTrue(incoming_session.is_dir())
        self.assertTrue((incoming_session / ".transfer_complete").is_file())
        self.assertTrue((incoming_session / "imu.csv").is_file())
        self.assertTrue((incoming_session / "frames" / "left" / "frame_000000.jpg").is_file())
        progress = json.loads((self.data_root / "metrics" / f"{session_id}.json").read_text())
        self.assertEqual(progress["state"], "committed")
        self.assertEqual(progress["bytes_received"], len(tar_bytes))
        self.assertGreater(progress["average_bytes_per_second"], 0)

        # 2. Re-upload identical session: Expect 200 OK (idempotent)
        status2, _, body2 = self.request("PUT", f"/halo-upload/v1/sessions/{session_id}", body=tar_bytes, headers=headers)
        self.assertEqual(status2, 200)
        receipt2 = json.loads(body2)
        self.assertEqual(receipt2["state"], "committed")
        self.assertEqual(receipt2["archive_sha256"], sha)

        # A retry must supply the archive it claims to supply.
        status_bad, _, _ = self.request(
            "PUT", f"/halo-upload/v1/sessions/{session_id}",
            body=b"x" * len(tar_bytes), headers=headers
        )
        self.assertEqual(status_bad, 400)

        # 3. Upload same session ID with different content: Expect 409 Conflict
        altered_tar = tar_bytes + b"extra-padding"
        altered_sha = sha256_bytes(altered_tar)
        headers3 = dict(headers)
        headers3["X-HALO-Archive-SHA256"] = altered_sha
        headers3["Content-Length"] = str(len(altered_tar))

        status3, _, _ = self.request("PUT", f"/halo-upload/v1/sessions/{session_id}", body=altered_tar, headers=headers3)
        self.assertEqual(status3, 409)

    def test_upload_real_halo_sample_session(self):
        halo_session = Path(__file__).resolve().parents[1] / "HALO/data/sessions/20261014T152654.760Z-398d9213611d-14"
        if not halo_session.is_dir():
            self.skipTest("HALO sample session not found")

        # Import upload_sessions from HALO
        halo_bin = Path(__file__).resolve().parents[1] / "HALO/capture/bin"
        sys.path.insert(0, str(halo_bin))
        import upload_sessions

        with tempfile.TemporaryDirectory() as client_tmp:
            client_root = Path(client_tmp)
            outbox = client_root / "outbox"
            outbox.mkdir()

            archive, sha = upload_sessions.prepare_archive(halo_session, outbox, include_video=False)
            self.assertTrue(archive.is_file())

            headers = {
                "Authorization": f"Bearer {self.token}",
                "X-HALO-Protocol-Version": "1",
                "X-HALO-Session-ID": halo_session.name,
                "X-HALO-Archive-SHA256": sha,
                "Content-Type": "application/x-tar",
                "Content-Length": str(archive.stat().st_size)
            }
            status, _, body = self.request(
                "PUT",
                f"/halo-upload/v1/sessions/{halo_session.name}",
                body=archive.read_bytes(),
                headers=headers
            )
            self.assertEqual(status, 201)
            receipt = json.loads(body)
            self.assertEqual(receipt["state"], "committed")
            self.assertEqual(receipt["session_id"], halo_session.name)

            # Check that receiver verified and committed the complete session on SENSE
            committed = self.data_root / "incoming" / halo_session.name
            self.assertTrue((committed / ".transfer_complete").is_file())
            self.assertTrue((committed / "imu.csv").is_file())
            self.assertTrue((committed / "frames.csv").is_file())
            self.assertTrue((committed / "pairs.csv").is_file())
            self.assertTrue((committed / "sync_report.json").is_file())
            self.assertEqual(len(list((committed / "frames" / "left").glob("*.jpg"))), 10)
            self.assertEqual(len(list((committed / "frames" / "right").glob("*.jpg"))), 10)



if __name__ == "__main__":
    unittest.main()
