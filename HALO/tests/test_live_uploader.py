"""Unit tests for HALO live Protocol v2 outbox, IMU binding, and multipart upload."""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "capture/bin"))
import upload_sessions as uploader


class LiveUploaderTests(unittest.TestCase):
    def test_global_fifo_eviction_at_max_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "live_outbox"
            s1 = outbox / "20261119T100000.000Z-halo-1"
            s2 = outbox / "20261119T100100.000Z-halo-2"
            s1.mkdir(parents=True)
            s2.mkdir(parents=True)
            for idx, (folder, ts) in enumerate([(s1, 100), (s1, 200), (s2, 300), (s2, 400)]):
                jpg = folder / f"frame_{idx:06d}.jpg"
                meta = folder / f"frame_{idx:06d}.meta.json"
                jpg.write_bytes(b"\xff\xd8\xff\xd9")
                meta.write_text(json.dumps({"session_id": folder.name, "frame_index": idx, "capture_utc_ns": ts}))
                os.utime(meta, ns=(ts * 10**9, ts * 10**9))

            evicted = uploader.enforce_live_outbox_cap(outbox, max_frames=2)
            self.assertEqual(evicted, 2)
            remaining = uploader.list_live_outbox_frames(outbox)
            self.assertEqual(len(remaining), 2)
            self.assertEqual([p[1].name for p in remaining], ["frame_000002.meta.json", "frame_000003.meta.json"])

    def test_imu_lookup_partial_and_finalized_with_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "20261119T120000.000Z-halo-1"
            partial = root / "sessions" / f".{session_id}.partial"
            partial.mkdir(parents=True)
            imu_csv = partial / "imu.csv"
            imu_csv.write_text(
                "utc_ns,quat_i,quat_j,quat_k,quat_real\n"
                "1000000000000,0.0,0.0,0.0,1.0\n"
                "1000500000000,0.0,0.0,0.0,1.0\n",
                encoding="utf-8",
            )
            calib = root / "calibration_v5.yaml"
            calib.write_text(
                "imu_bno085:\n  R_left_from_imu: [[1, 0, 0], [0, 1, 0], [0, 0, 1]]\n",
                encoding="utf-8",
            )
            r_mat = uploader.load_r_left_from_imu(calib)
            self.assertEqual(r_mat, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])

            # Within +/- 250 ms of 1000000000000
            imu_ns, pitch, roll, yaw, calibrated = uploader.lookup_imu_orientation(
                root, session_id, 1000100000000, r_mat, image_rotation=180
            )
            self.assertEqual(imu_ns, 1000000000000)
            self.assertTrue(calibrated)
            self.assertAlmostEqual(pitch, 0.0, places=2)
            self.assertAlmostEqual(roll, 0.0, places=2)
            self.assertTrue(math.isclose(abs(yaw), 180.0, abs_tol=1e-2))

            # Outside +/- 250 ms -> None
            imu_ns2, pitch2, roll2, yaw2, calibrated2 = uploader.lookup_imu_orientation(
                root, session_id, 1001000000000, None, image_rotation=0
            )
            self.assertIsNone(imu_ns2)
            self.assertIsNone(pitch2)
            self.assertFalse(calibrated2)

            # Finalized session folder also works
            final_dir = root / "sessions" / session_id
            partial.rename(final_dir)
            imu_ns3, _, _, _, calibrated3 = uploader.lookup_imu_orientation(
                root, session_id, 1000500000000, None, image_rotation=0
            )
            self.assertEqual(imu_ns3, 1000500000000)
            self.assertFalse(calibrated3)

    def test_process_live_outbox_once_uploads_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "20261119T120000.000Z-halo-42"
            sdir = root / "live_outbox" / session_id
            sdir.mkdir(parents=True)
            (sdir / "frame_000000.jpg").write_bytes(b"\xff\xd8\xff\xe0test\xff\xd9")
            (sdir / "frame_000000.meta.json").write_text(
                json.dumps({"session_id": session_id, "frame_index": 0, "capture_utc_ns": 1795089600000000000}),
                encoding="utf-8",
            )
            captured = {}

            def fake_upload(jpeg, meta):
                captured["jpg"] = jpeg
                captured["meta"] = meta
                return 201

            with patch.object(uploader, "upload_live_frame", side_effect=fake_upload):
                did_work = uploader.process_live_outbox_once(root, max_frames=120, r_left_from_imu=None, image_rotation=180)
            self.assertTrue(did_work)
            self.assertEqual(captured["meta"]["session_id"], session_id)
            self.assertEqual(captured["meta"]["frame_index"], 0)
            self.assertFalse(captured["meta"]["imu_calibrated"])
            self.assertIsNone(captured["meta"]["qualified_capture_utc_ns"])
            self.assertFalse((sdir / "frame_000000.jpg").exists())
            self.assertFalse((sdir / "frame_000000.meta.json").exists())

    def test_retry_reuses_frozen_frame_and_rejection_unblocks_next_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "live_outbox" / "walk"
            outbox.mkdir(parents=True)
            for index in (0, 1):
                stem = f"frame_{index:06d}"
                (outbox / f"{stem}.jpg").write_bytes(b"\xff\xd8" + bytes([index]))
                metadata = {"session_id": "walk", "frame_index": index, "capture_utc_ns": 123}
                (outbox / f"{stem}.meta.json").write_text(json.dumps(metadata))
            response = [uploader.UploadError("HTTP 503"), uploader.RejectedLiveFrame(409, "frame_conflict"), 201]
            with patch.object(uploader, "upload_live_frame", side_effect=response) as upload:
                with self.assertRaises(uploader.UploadError):
                    uploader.process_live_outbox_once(root, 120, None, 180)
                self.assertTrue((outbox / "frame_000000.upload.json").is_file())
                self.assertTrue(uploader.process_live_outbox_once(root, 120, None, 0))
                self.assertTrue(uploader.process_live_outbox_once(root, 120, None, 0))
                self.assertEqual(upload.call_args_list[0], upload.call_args_list[1])
                self.assertEqual(upload.call_args_list[2].args[1]["frame_index"], 1)
            evidence, = (root / "live_quarantine").glob("*/reason.json")
            self.assertEqual(json.loads(evidence.read_bytes())["http_status"], 409)

    def test_configuration_pause_keeps_frame_until_explicit_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "live_outbox" / "walk"
            outbox.mkdir(parents=True)
            (outbox / "frame_000000.jpg").write_bytes(b"\xff\xd8jpeg")
            (outbox / "frame_000000.meta.json").write_text(json.dumps({
                "session_id": "walk", "frame_index": 0, "capture_utc_ns": 123,
            }))
            pause = root / "manual-pause"
            pause.touch()
            with patch.dict(os.environ, {"LIVE_UPLOAD_PAUSE_FILE": str(pause)}), patch.object(
                uploader, "upload_live_frame", side_effect=[uploader.LiveConfigurationError("HTTP 401"), 201],
            ) as upload:
                self.assertFalse(uploader.process_live_outbox_once(root, 120, None, 180))
                upload.assert_not_called()
                pause.unlink()
                self.assertFalse(uploader.process_live_outbox_once(root, 120, None, 180))
                self.assertFalse(uploader.process_live_outbox_once(root, 120, None, 180))
                self.assertEqual(upload.call_count, 1)
                (root / "live-upload.configuration-error.json").unlink()
                self.assertTrue(uploader.process_live_outbox_once(root, 120, None, 180))
                self.assertEqual(upload.call_args_list[0], upload.call_args_list[1])

    def test_receipt_status_and_identity(self):
        meta = {"session_id": "walk", "frame_index": 3}
        self.assertEqual(uploader.check_live_receipt(201, json.dumps({**meta, "state": "accepted", "reason": None}).encode(), meta), 201)
        with self.assertRaises(uploader.UploadError):
            uploader.check_live_receipt(201, json.dumps({**meta, "frame_index": 4}).encode(), meta)
        for status in (429, 503, 507):
            with self.assertRaises(uploader.UploadError) as raised:
                uploader.check_live_receipt(status, b"{}", meta)
            self.assertIs(type(raised.exception), uploader.UploadError)
        for status in (400, 409, 410, 413):
            with self.assertRaises(uploader.RejectedLiveFrame):
                uploader.check_live_receipt(status, b"{}", meta)
        for status in (302, 401, 403, 404, 405):
            with self.assertRaises(uploader.LiveConfigurationError):
                uploader.check_live_receipt(status, b"{}", meta)
        with patch.dict(os.environ, {"LIVE_UPLOAD_REQUIRE_DURABLE_RECEIPT": "true"}):
            with self.assertRaises(uploader.LiveConfigurationError):
                uploader.check_live_receipt(201, json.dumps({**meta, "state": "queued"}).encode(), meta)


if __name__ == "__main__":
    unittest.main()
