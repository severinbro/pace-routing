"""BH1750 timestamp, matching, failure, and archive compatibility tests."""

import csv
import importlib.util
import json
import math
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


logger = module("light_logger", "capture/bin/light_logger.py")
sync = module("light_sync", "capture/bin/light_sync.py")
uploader = module("upload_sessions_light", "capture/bin/upload_sessions.py")


class FakeSensor:
    lux = 42.5


class LightTests(unittest.TestCase):
    def test_host_read_window_and_invalid_lux(self):
        wall = iter((100, 110))
        mono = iter((200, 208))
        self.assertEqual(logger.read_row(FakeSensor(), lambda: next(wall), lambda: next(mono)),
                         (105, 204, 42.5, 100, 110, 8))
        sensor = FakeSensor()
        sensor.lux = math.nan
        with self.assertRaises(ValueError):
            logger.read_row(sensor)

    def session(self, root, status="recorded"):
        session = root / "session1"
        session.mkdir()
        (session / "capture.json").write_text(json.dumps({
            "light_enabled": True, "light_capture_status": status,
        }))
        with (session / ".pairs.partial").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=["pair_index", "pair_capture_utc_ns"])
            writer.writeheader()
            writer.writerow({"pair_index": 0, "pair_capture_utc_ns": 1_000_000_000})
            writer.writerow({"pair_index": 1, "pair_capture_utc_ns": 2_000_000_000})
        (session / ".sync_report.partial").write_text('{"pairs": 2}')
        return session

    def write_samples(self, session, times):
        with (session / "light.csv").open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(logger.FIELDS)
            for index, timestamp in enumerate(times):
                writer.writerow((timestamp, timestamp, 20 + index,
                                 timestamp - 1_000, timestamp + 1_000, 2_000))

    def test_nearest_late_and_missing_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.session(Path(temporary))
            self.write_samples(session, [900_000_000, 1_600_000_000])
            sync.process(session)
            with (session / ".pairs.partial").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual((rows[0]["light_sample_index"], rows[0]["light_lux"],
                              rows[0]["light_gap_ns"], rows[0]["light_status"]),
                             ("0", "20", "100000000", "ok"))
            self.assertEqual(rows[1]["light_status"], "missing")
            self.assertEqual(rows[1]["light_gap_ns"], "400000000")
            self.assertEqual(rows[1]["light_sample_index"], "")
            report = json.loads((session / ".sync_report.partial").read_text())
            self.assertEqual((report["pairs_with_light"], report["pairs_without_light"]), (1, 1))

    def test_absent_sensor_and_mid_recording_disconnect_keep_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.session(Path(temporary), "unavailable")
            sync.process(session)
            report = json.loads((session / ".sync_report.partial").read_text())
            self.assertEqual(report["pairs_without_light"], 2)
            self.assertEqual(report["light_source_error"], "light.csv missing")
        with tempfile.TemporaryDirectory() as temporary:
            session = self.session(Path(temporary), "interrupted")
            self.write_samples(session, [1_010_000_000])
            sync.process(session)
            with (session / ".pairs.partial").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["light_status"] for row in rows], ["ok", "missing"])
            self.assertTrue((session / "light.csv").is_file())

    def test_invalid_timestamp_log_is_flagged_not_fabricated(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.session(Path(temporary))
            self.write_samples(session, [1_100_000_000, 1_000_000_000])
            sync.process(session)
            report = json.loads((session / ".sync_report.partial").read_text())
            self.assertIn("not strictly increasing", report["light_source_error"])
            self.assertEqual(report["pairs_with_light"], 0)

    def test_uploader_includes_light_and_old_sessions_still_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session1"
            for side in ("left", "right"):
                (session / f"frames/{side}").mkdir(parents=True)
                (session / f"frames/{side}/frame_000000.jpg").write_bytes(b"jpeg")
            for name in uploader.REQUIRED_FILES:
                (session / name).write_text("x")
            (session / "capture.json").write_text('{"light_enabled": true}')
            (session / "light.csv").write_text("utc_ns,lux\n1,10\n")
            files = uploader.transfer_files(session)
            self.assertIn(session / "light.csv", files)
            archive, _ = uploader.prepare_archive(session, Path(temporary) / "outbox")
            with tarfile.open(archive) as bundle:
                self.assertIn("light.csv", bundle.getnames())
                self.assertIn(b"light.csv", bundle.extractfile("SHA256SUMS").read())
            (session / "light.csv").unlink()
            self.assertNotIn(session / "light.csv", uploader.transfer_files(session))
            (session / "capture.json").write_text('{}')
            self.assertNotIn(session / "light.csv", uploader.transfer_files(session))


if __name__ == "__main__":
    unittest.main()
