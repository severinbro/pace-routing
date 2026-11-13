"""Thermal rotation, timing, gaps, and manifest compatibility."""

import csv
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("thermal_sync", ROOT / "capture/bin/thermal_sync.py")
thermal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(thermal)
UPLOAD_SPEC = importlib.util.spec_from_file_location("upload_sessions", ROOT / "capture/bin/upload_sessions.py")
uploader = importlib.util.module_from_spec(UPLOAD_SPEC)
UPLOAD_SPEC.loader.exec_module(uploader)


class ThermalTests(unittest.TestCase):
    def test_rotates_preview_and_uint16_pixels(self):
        upper = bytes([index % 256 for index in range(thermal.WIDTH * thermal.HEIGHT) for _ in (0, 1)])
        lower = b"".join(struct.pack("<H", 18000 + index % 100)
                         for index in range(thermal.WIDTH * thermal.HEIGHT))
        raw, preview = thermal.split_frame(upper + lower)
        self.assertEqual(struct.unpack_from("<H", raw)[0],
                         18000 + (thermal.WIDTH * thermal.HEIGHT - 1) % 100)
        self.assertEqual(struct.unpack_from("<H", raw, len(raw) - 2)[0], 18000)
        self.assertEqual(preview[0], (thermal.WIDTH * thermal.HEIGHT - 1) % 256)
        png = thermal.grayscale_png(preview)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", png[16:24]), (256, 192))
        self.assertEqual(zlib.decompress(png[41:-16])[1], preview[0])

    def test_rejects_malformed_and_calibration_frames(self):
        with self.assertRaises(ValueError):
            thermal.split_frame(b"too short")
        with self.assertRaises(ValueError):
            thermal.split_frame(bytes(thermal.WIDTH * thermal.HEIGHT * 2)
                                + b"\x00\x80" * (thermal.WIDTH * thermal.HEIGHT))

    def test_absolute_colors_use_temperature_not_image_contrast(self):
        # 0, 30, 60 and 80 C map to fixed endpoints/midpoint, regardless of scene.
        counts = [round((temp + 273.15) * 64) for temp in (0, 30, 60, 80)]
        raw = b"".join(struct.pack("<H", counts[index % len(counts)])
                       for index in range(thermal.WIDTH * thermal.HEIGHT))
        png = thermal.absolute_color_png(raw, 0, 60)
        self.assertEqual(struct.unpack(">II", png[16:24]), (256, 192))
        self.assertEqual(png[25], 2)  # PNG RGB color type.
        first_row = zlib.decompress(png[41:-16])[:1 + thermal.WIDTH * 3]
        self.assertEqual(first_row[0], 0)
        self.assertEqual(first_row[1:4], thermal.COLOR_PALETTE[:3])
        self.assertEqual(first_row[7:10], thermal.COLOR_PALETTE[-3:])
        self.assertEqual(first_row[10:13], thermal.COLOR_PALETTE[-3:])
        with self.assertRaises(ValueError):
            thermal.absolute_color_png(b"short", 0, 60)
        with patch.dict(os.environ, {"THERMAL_COLOR_MIN_C": "60", "THERMAL_COLOR_MAX_C": "0"}):
            with self.assertRaises(ValueError):
                thermal.color_limits()

    def test_nearest_and_gap(self):
        self.assertEqual(thermal.nearest_frame([100, 140, 180], 153), (1, 13))
        self.assertEqual(thermal.nearest_frame([], 153), None)
        self.assertEqual(thermal.nearest_frame([100], 300_000_100), None)

    def test_missing_source_preserves_stereo_and_reports_gaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "logs").mkdir()
            (session / "capture.json").write_text(json.dumps({
                "thermal_enabled": True, "thermal_capture_status": "unavailable",
                "started_at_utc_ns": 1_700_000_000_000_000_000,
                "finished_at_utc_ns": 1_700_000_002_000_000_000,
            }))
            with (session / ".pairs.partial").open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=["pair_index", "pair_capture_utc_ns"])
                writer.writeheader()
                writer.writerow({"pair_index": 0, "pair_capture_utc_ns": 1_700_000_000_000_000_000})
            (session / ".sync_report.partial").write_text('{"pairs": 1}')
            thermal.process(session, session / ".frames.partial")
            with (session / ".thermal_manifest.partial").open(newline="") as source:
                self.assertEqual(list(csv.DictReader(source))[0]["thermal_status"], "missing")
            with (session / ".pairs.partial").open(newline="") as source:
                self.assertEqual(list(csv.DictReader(source))[0]["thermal_status"], "missing")
            report = json.loads((session / ".sync_report.partial").read_text())
            self.assertEqual(report["pairs_without_thermal"], 1)

    def test_large_skew_retains_sample_and_flags_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "logs").mkdir()
            (session / ".thermal.partial").mkdir()
            (session / ".thermal.partial/thermal.nut").write_bytes(b"diagnostic source")
            (session / "capture.json").write_text(json.dumps({
                "thermal_enabled": True, "thermal_capture_status": "recorded",
                "started_at_utc_ns": 1_700_000_000_000_000_000,
                "finished_at_utc_ns": 1_700_000_002_000_000_000,
            }))
            target = 1_700_000_001_000_000_000
            with (session / ".pairs.partial").open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=["pair_index", "pair_capture_utc_ns"])
                writer.writeheader()
                writer.writerow({"pair_index": 0, "pair_capture_utc_ns": target})
            (session / ".sync_report.partial").write_text('{"pairs": 1}')
            upper = b"\x20\x80" * (thermal.WIDTH * thermal.HEIGHT)
            lower = b"\x00\x4a" * (thermal.WIDTH * thermal.HEIGHT)
            with patch.object(thermal, "probe_frames", return_value=[target + 100_000_000]), \
                 patch.object(thermal, "decode_selected", return_value=iter([upper + lower])):
                thermal.process(session, session / ".frames.partial")
            with (session / ".thermal_manifest.partial").open(newline="") as source:
                row = next(csv.DictReader(source))
            self.assertEqual(row["thermal_status"], "skew")
            self.assertEqual(row["thermal_skew_ns"], "100000000")
            self.assertTrue((session / ".frames.partial/thermal/frame_000000.raw").is_file())
            self.assertEqual(row["color_file"], "frames/thermal/frame_000000_color.png")
            self.assertTrue((session / ".frames.partial/thermal/frame_000000_color.png").is_file())
            metadata = json.loads((session / ".thermal_metadata.partial").read_text())
            self.assertEqual((metadata["color_min_c"], metadata["color_max_c"]), (0, 60))

    def test_upload_includes_color_and_accepts_older_grayscale_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session1"
            (session / "frames/left").mkdir(parents=True)
            (session / "frames/right").mkdir(parents=True)
            (session / "frames/thermal").mkdir(parents=True)
            (session / "capture.json").write_text('{"thermal_enabled": true}')
            for name in ("frames.csv", "sync_report.json", ".capture_complete",
                         ".extraction_complete", "thermal.json"):
                (session / name).write_text("ok")
            (session / "pairs.csv").write_text("pair_index\n0\n")
            for side in ("left", "right"):
                (session / f"frames/{side}/frame_000000.jpg").write_bytes(b"jpg")
            thermal_dir = session / "frames/thermal"
            (thermal_dir / "frame_000000.raw").write_bytes(b"\0" * (256 * 192 * 2))
            (thermal_dir / "frame_000000.png").write_bytes(b"gray")
            (thermal_dir / "frame_000000_color.png").write_bytes(b"color")
            fields = ["thermal_status", "raw_file", "preview_file", "color_file"]
            row = {"thermal_status": "ok", "raw_file": "frames/thermal/frame_000000.raw",
                   "preview_file": "frames/thermal/frame_000000.png",
                   "color_file": "frames/thermal/frame_000000_color.png"}
            with (session / "thermal.csv").open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            files = uploader.transfer_files(session)
            self.assertIn(thermal_dir / "frame_000000_color.png", files)
            (thermal_dir / "frame_000000_color.png").unlink()
            with self.assertRaises(ValueError):
                uploader.transfer_files(session)
            with (session / "thermal.csv").open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields[:-1])
                writer.writeheader()
                writer.writerow({key: row[key] for key in fields[:-1]})
            self.assertNotIn(thermal_dir / "frame_000000_color.png", uploader.transfer_files(session))


if __name__ == "__main__":
    unittest.main()
