"""Integration tests for SENSE Protocol v2 live frame ingestion and Redis queue."""

import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
import server as receiver_server


class FakeRedis:
    def __init__(self):
        self.items = []

    def rpush(self, key, value):
        self.items.append(value)
        return len(self.items)

    def llen(self, key):
        return len(self.items)

    def lpop(self, key):
        return self.items.pop(0) if self.items else None


def build_multipart(meta_bytes: bytes | None, image_bytes: bytes | None, boundary: str = "TestBoundary123"):
    parts = []
    if meta_bytes is not None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="meta"; filename="meta.json"\r\n'
            f"Content-Type: application/json\r\n\r\n".encode("utf-8")
            + meta_bytes
            + b"\r\n"
        )
    if image_bytes is not None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="source.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + image_bytes
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class LiveReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "data"
        cls.root.mkdir()
        cls.token_file = Path(cls.tmp.name) / "upload.token"
        cls.token = "live-secret-token"
        cls.token_file.write_text(cls.token, encoding="utf-8")
        cls.prev_env = {k: os.environ.get(k) for k in ("RECEIVER_ROOT", "UPLOAD_TOKEN_FILE", "MIN_FREE_DISK_MB", "LIVE_QUEUE_MAX_LEN")}
        os.environ["RECEIVER_ROOT"] = str(cls.root)
        os.environ["UPLOAD_TOKEN_FILE"] = str(cls.token_file)
        os.environ["MIN_FREE_DISK_MB"] = "1"
        os.environ["LIVE_QUEUE_MAX_LEN"] = "3"

        cls.fake_redis = FakeRedis()
        cls.redis_patch = patch.object(receiver_server, "get_redis_client", return_value=cls.fake_redis)
        cls.redis_patch.start()

        cls.httpd = receiver_server.ThreadingHTTPServer(("127.0.0.1", 0), receiver_server.ReceiverHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.redis_patch.stop()
        for k, v in cls.prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def setUp(self):
        self.fake_redis.items.clear()
        live_dir = self.root / "live"
        if live_dir.exists():
            shutil.rmtree(live_dir)

    def _post(self, path: str, body: bytes, content_type: str, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_healthz_reports_live_protocol_version_2(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["protocol_version"], 1)
        self.assertEqual(data["live_protocol_version"], 2)

    def test_auth_and_validation_errors(self):
        meta = json.dumps({"session_id": "s1", "frame_index": 0, "capture_utc_ns": 123}).encode()
        body, ctype = build_multipart(meta, b"\xff\xd8\xff\xd9")
        status, _ = self._post("/halo-upload/v2/sessions/s1/frames", body, ctype, token=None)
        self.assertEqual(status, 401)

        bad_body, bad_ctype = build_multipart(b"{bad json", b"\xff\xd8\xff\xd9")
        status, _ = self._post("/halo-upload/v2/sessions/s1/frames", bad_body, bad_ctype, token=self.token)
        self.assertEqual(status, 400)

        status, _ = self._post("/halo-upload/v2/sessions/..evil/frames", body, ctype, token=self.token)
        self.assertEqual(status, 400)

    def test_rejects_oversized_image_with_413(self):
        meta = json.dumps({"session_id": "s-big", "frame_index": 0, "capture_utc_ns": 123}).encode()
        big_img = b"\xff\xd8" + (b"0" * (receiver_server.MAX_LIVE_IMAGE_BYTES + 10))
        body, ctype = build_multipart(meta, big_img)
        status, _ = self._post("/halo-upload/v2/sessions/s-big/frames", body, ctype, token=self.token)
        self.assertEqual(status, 413)

    def test_disk_guard_returns_507(self):
        meta = json.dumps({"session_id": "s-disk", "frame_index": 0, "capture_utc_ns": 123}).encode()
        body, ctype = build_multipart(meta, b"\xff\xd8\xff\xd9")
        fake_usage = shutil._ntuple_diskusage(10_000_000, 9_999_000, 1_000)
        with patch.object(receiver_server.shutil, "disk_usage", return_value=fake_usage):
            status, _ = self._post("/halo-upload/v2/sessions/s-disk/frames", body, ctype, token=self.token)
        self.assertEqual(status, 507)

    @unittest.skipUnless(sys.platform.startswith("linux"), "durable path uses Linux file locks")
    def test_durable_retry_after_failed_registration_and_receipts_at_capacity(self):
        from live_frame_state import FrameStore, save_json

        durable_root = self.root / "live-v2"
        if durable_root.exists():
            shutil.rmtree(durable_root)
        redis = self.fake_redis
        metadata = {"session_id": "durable", "frame_index": 0, "capture_utc_ns": 123}
        body, content_type = build_multipart(json.dumps(metadata).encode(), b"\xff\xd8\xff\xd9")
        path = "/halo-upload/v2/sessions/durable/frames"
        env = {
            "SEGMENTATION_V2_ENABLED": "true",
            "SEGMENTATION_ADMISSION_ENABLED": "true",
            "SEGMENTATION_LIVE_ROOT": str(durable_root),
        }
        with (
            patch.dict(os.environ, env),
            patch.object(redis, "get", return_value='{"adapter":"test"}', create=True),
            patch.object(redis, "eval", side_effect=[OSError("Redis down"), None], create=True),
        ):
            self.assertEqual(self._post(path, body, content_type, self.token)[0], 503)
            frame = FrameStore(durable_root).frame("durable/0")
            self.assertTrue((frame / "stage.json").is_file())
            self.assertFalse((frame / "accepted.json").exists())
            self.assertEqual(self._post(path, body, content_type, self.token)[0], 201)
            self.assertTrue((frame / "accepted.json").is_file())
            with patch.dict(os.environ, {"MIN_FREE_DISK_MB": "1000000000"}):
                self.assertEqual(self._post(path, body, content_type, self.token)[0], 200)
                conflict, ctype = build_multipart(json.dumps(metadata).encode(), b"\xff\xd8changed")
                self.assertEqual(self._post(path, conflict, ctype, self.token)[0], 409)
                save_json(frame.parent.parent / "retired.json", {"session_id": "durable"})
                self.assertEqual(self._post(path, body, content_type, self.token)[0], 410)

    def test_idempotent_write_and_redis_queue_cap_eviction(self):
        for idx in range(4):
            meta = json.dumps({"session_id": "walk-1", "frame_index": idx, "capture_utc_ns": 1000 + idx}).encode()
            body, ctype = build_multipart(meta, b"\xff\xd8\xff\xe0data\xff\xd9")
            status, payload = self._post("/halo-upload/v2/sessions/walk-1/frames", body, ctype, token=self.token)
            self.assertEqual(status, 201)
            self.assertEqual(payload["frame_index"], idx)

        # Duplicate returns 200 without re-enqueuing
        dup_meta = json.dumps({"session_id": "walk-1", "frame_index": 3, "capture_utc_ns": 1003}).encode()
        dup_body, dup_ctype = build_multipart(dup_meta, b"\xff\xd8\xff\xe0data\xff\xd9")
        status, _ = self._post("/halo-upload/v2/sessions/walk-1/frames", dup_body, dup_ctype, token=self.token)
        self.assertEqual(status, 200)

        # Queue capped at 3 -> frame 0 evicted from Redis AND deleted from disk
        self.assertEqual(len(self.fake_redis.items), 3)
        queued_indices = [json.loads(item)["frame_index"] for item in self.fake_redis.items]
        self.assertEqual(queued_indices, [1, 2, 3])
        self.assertFalse((self.root / "live" / "walk-1" / "frame_000000").exists())
        self.assertTrue((self.root / "live" / "walk-1" / "frame_000003" / "source.jpg").is_file())
        self.assertTrue((self.root / "live" / "walk-1" / "frame_000003" / "meta.json").is_file())


if __name__ == "__main__":
    unittest.main()
