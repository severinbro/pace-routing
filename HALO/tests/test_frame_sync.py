"""Timing-manifest tests independent of the camera and Docker runtime."""

import csv
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "capture/bin/frame_sync.py"
SPEC = importlib.util.spec_from_file_location("frame_sync", MODULE_PATH)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class TimingTests(unittest.TestCase):
    def test_source_pts_conversion(self):
        self.assertEqual(sync.capture_time_ns(1_700_000_000_000_000_000, 2033, 1, 1000),
                         1_700_000_002_033_000_000)

    def test_invalid_first_frame_clock(self):
        started = 1_700_000_000_000_000_000
        with self.assertRaises(sync.TimingError):
            sync.first_frame_utc_ns("no encoder timestamp", started, started + 10_000_000_000)
        with self.assertRaises(sync.TimingError):
            sync.first_frame_utc_ns("Video start timestamp : 123456 us", started,
                                    started + 10_000_000_000)
        self.assertEqual(sync.first_frame_utc_ns(
            "Video start timestamp : 1700000001000000 us", started,
            started + 10_000_000_000), started + 1_000_000_000)

    def test_imu_bracket_missing(self):
        self.assertEqual(sync.bracket_imu([100, 200], 50, 150), ("", "", "", False))
        self.assertEqual(sync.bracket_imu([100, 200], 150, 50), ("0", "1", "100", False))

    def test_selection_prefers_nearby_synchronized_frames(self):
        left = [{"index": index, "source_pts_ns": value, "capture_utc_ns": value}
                for index, value in enumerate([0, 33_000_000, 1_000_000_000, 1_033_000_000])]
        right = [{"index": index, "source_pts_ns": value, "capture_utc_ns": value}
                 for index, value in enumerate([20_000, 66_000_000, 1_000_020_000, 1_066_000_000])]
        chosen = sync.choose_frames(left, right, 1_000_000_000, 16_666_666)
        self.assertEqual([frame["index"] for frame in chosen["left"]], [0, 2])
        self.assertEqual([frame["index"] for frame in chosen["right"]], [0, 2])

    def test_one_sided_drop_and_large_skew(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            frames = session / ".frames.partial"
            for side in ("left", "right"):
                (frames / side).mkdir(parents=True)
            (session / "logs").mkdir()
            base = 1_700_000_000_000_000_000
            (session / "capture.json").write_text(json.dumps({
                "schema_version": 2, "synchronization": "rpicam_software_sync",
                "imu_enabled": True, "fps": 30,
                "left_first_frame_utc_ns": base,
                "right_first_frame_utc_ns": base + 100_000_000,
            }))
            with (session / "imu.csv").open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["utc_ns", "read_start_utc_ns", "read_end_utc_ns", "read_duration_ns"])
                for offset in range(0, 3_000_000_001, 50_000_000):
                    writer.writerow([base + offset, base + offset - 1_000_000,
                                     base + offset + 1_000_000, 2_000_000])
            for side, pts in (("left", [0, 1000, 2000]), ("right", [0, 2000])):
                lines = ["[Parsed_showinfo_1 @ 0x1] config in time_base: 1/1000, config out time_base: 0/0"]
                for number, value in enumerate(pts):
                    lines.append(f"[Parsed_showinfo_1 @ 0x1] n: {number} pts: {value} pts_time:{value / 1000}")
                    (frames / side / f"frame_{number:06d}.jpg").write_bytes(b"jpeg")
                (session / "logs" / f"extract_{side}.log").write_text("\n".join(lines))
            (session / "logs/frame_selection.json").write_text(json.dumps({
                side: [{"index": index, "source_pts_ns": pts * 1_000_000,
                        "capture_utc_ns": base + (100_000_000 if side == "right" else 0)
                        + pts * 1_000_000, "time_bucket": pts // 1000}
                       for index, pts in enumerate(values)]
                for side, values in (("left", [0, 1000, 2000]), ("right", [0, 2000]))
            }))
            report = sync.make_manifests(session, frames, Decimal("1"), Decimal("0.05"))
            self.assertEqual(report["pairs"], 2)
            self.assertEqual(report["unpaired_left_images"], 1)
            self.assertEqual(report["unpaired_right_images"], 0)
            self.assertEqual(report["pairs_exceeding_stereo_skew_limit"], 2)
            with (session / ".pairs.partial").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[1]["right_file"], "frames/right/frame_000001.jpg")
            self.assertEqual(rows[1]["left_file"], "frames/left/frame_000002.jpg")
            self.assertEqual(rows[0]["stereo_skew_ns"], "100000000")
            self.assertEqual((rows[0]["imu_before_index"], rows[0]["imu_after_index"]), ("1", "1"))


if __name__ == "__main__":
    unittest.main()
