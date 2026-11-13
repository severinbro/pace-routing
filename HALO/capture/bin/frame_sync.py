#!/usr/bin/env python3
"""Validate camera clocks and build source-PTS/IMU timing manifests."""

import argparse
from bisect import bisect_left
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys


VIDEO_START = re.compile(r"Video start timestamp\s*:\s*(\d+)\s*us")
TIME_BASE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
SHOWINFO = re.compile(r"\[Parsed_showinfo_\d+[^]]*\].*?\bn:\s*(\d+)\s+pts:\s*(-?\d+)")
PAIR_FIELDS = (
    "pair_index", "nominal_time_seconds", "left_file", "right_file",
    "left_capture_utc_ns", "right_capture_utc_ns", "stereo_skew_ns",
    "pair_capture_utc_ns", "imu_before_index", "imu_after_index",
    "imu_gap_ns", "stereo_synced", "imu_bracketed",
)
FRAME_FIELDS = ("side", "file", "source_pts_ns", "capture_utc_ns", "time_bucket", "paired")


class TimingError(ValueError):
    """A deterministic timing failure; leave the recording for diagnosis."""


def first_frame_utc_ns(log_text: str, started_ns: int, finished_ns: int) -> int:
    values = VIDEO_START.findall(log_text)
    if len(values) != 1:
        raise TimingError(f"Expected one encoder first-frame timestamp, found {len(values)}")
    timestamp_ns = int(values[0]) * 1000
    # Allows brief clock/log scheduling differences, but rejects CLOCK_BOOTTIME.
    tolerance_ns = 2_000_000_000
    if not started_ns - tolerance_ns <= timestamp_ns <= finished_ns + tolerance_ns:
        raise TimingError(f"Encoder first-frame clock is not plausible UTC: {timestamp_ns}")
    return timestamp_ns


def capture_time_ns(first_frame_ns: int, pts: int, numerator: int, denominator: int) -> int:
    if pts < 0 or numerator <= 0 or denominator <= 0:
        raise TimingError("Invalid source presentation timestamp or time base")
    return first_frame_ns + pts * numerator * 1_000_000_000 // denominator


def parse_showinfo(log_text: str) -> list[int]:
    bases = TIME_BASE.findall(log_text)
    if not bases:
        raise TimingError("FFmpeg did not report the selected-frame time base")
    numerator, denominator = map(int, bases[0])
    matches = SHOWINFO.findall(log_text)
    if not matches:
        raise TimingError("FFmpeg did not report selected-frame timestamps")
    if [int(number) for number, _ in matches] != list(range(len(matches))):
        raise TimingError("FFmpeg showinfo frame indices are incomplete")
    pts_ns = [capture_time_ns(0, int(pts), numerator, denominator) for _, pts in matches]
    if any(b <= a for a, b in zip(pts_ns, pts_ns[1:])):
        raise TimingError("Selected-frame PTS are not strictly increasing")
    return pts_ns


def probe_video(path: Path, first_ns: int) -> list[dict]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=time_base:frame=best_effort_timestamp",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise TimingError(f"Cannot probe source video {path}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if len(streams) != 1:
        raise TimingError(f"Expected one video stream in {path}")
    numerator, denominator = map(int, streams[0]["time_base"].split("/"))
    frames = data.get("frames", [])
    if not frames:
        raise TimingError(f"No decoded source frames in {path}")
    result_frames = []
    for index, frame in enumerate(frames):
        pts = int(frame["best_effort_timestamp"])
        pts_ns = capture_time_ns(0, pts, numerator, denominator)
        result_frames.append({"index": index, "source_pts_ns": pts_ns,
                              "capture_utc_ns": first_ns + pts_ns})
    if any(b["source_pts_ns"] <= a["source_pts_ns"] for a, b in zip(result_frames, result_frames[1:])):
        raise TimingError(f"Source PTS are not strictly increasing in {path}")
    return result_frames


def choose_frames(left: list[dict], right: list[dict], interval_ns: int,
                  skew_limit_ns: int) -> dict[str, list[dict]]:
    """Choose one frame per side/bucket, favoring synchronized exposures."""
    origin_ns = min(left[0]["capture_utc_ns"], right[0]["capture_utc_ns"])
    groups: dict[int, dict[str, list[dict]]] = {}
    for side, frames in (("left", left), ("right", right)):
        for frame in frames:
            bucket = (frame["capture_utc_ns"] - origin_ns) // interval_ns
            groups.setdefault(bucket, {"left": [], "right": []})[side].append(frame)
    chosen = {"left": [], "right": []}
    for bucket, sides in sorted(groups.items()):
        target_ns = origin_ns + bucket * interval_ns
        left_candidates, right_candidates = sides["left"], sides["right"]
        if left_candidates and right_candidates:
            def rank(pair):
                left_frame, right_frame = pair
                skew = abs(left_frame["capture_utc_ns"] - right_frame["capture_utc_ns"])
                middle = (left_frame["capture_utc_ns"] + right_frame["capture_utc_ns"]) // 2
                return (skew > skew_limit_ns, abs(middle - target_ns), skew)
            left_frame, right_frame = min(
                ((a, b) for a in left_candidates for b in right_candidates), key=rank
            )
            chosen["left"].append({**left_frame, "time_bucket": bucket})
            chosen["right"].append({**right_frame, "time_bucket": bucket})
        else:
            for side, candidates in sides.items():
                if candidates:
                    frame = min(candidates, key=lambda item: abs(item["capture_utc_ns"] - target_ns))
                    chosen[side].append({**frame, "time_bucket": bucket})
    return chosen


def plan_extraction(session: Path, interval: Decimal) -> None:
    metadata = json.loads((session / "capture.json").read_text())
    if metadata.get("schema_version") != 2:
        raise TimingError("Missing validated v2 capture clock")
    interval_ns = int(interval * 1_000_000_000)
    if interval_ns <= 0:
        raise TimingError("Frame interval must be positive")
    skew_limit_ns = int(Decimal(1_000_000_000) / (Decimal(2) * Decimal(metadata["fps"])))
    left = probe_video(session / "video/left.mkv", int(metadata["left_first_frame_utc_ns"]))
    right = probe_video(session / "video/right.mkv", int(metadata["right_first_frame_utc_ns"]))
    chosen = choose_frames(left, right, interval_ns, skew_limit_ns)
    for side in ("left", "right"):
        if not chosen[side]:
            raise TimingError(f"No {side} frame selected")
        indices = [frame["index"] for frame in chosen[side]]
        expression = "select='" + "+".join(f"eq(n,{index})" for index in indices) + "',showinfo"
        (session / f"logs/select_{side}.txt").write_text(expression)
    (session / "logs/frame_selection.json").write_text(json.dumps(chosen, indent=2) + "\n")


def bracket_imu(samples: list[int], target_ns: int, max_gap_ns: int) -> tuple[str, str, str, bool]:
    position = bisect_left(samples, target_ns)
    if position < len(samples) and samples[position] == target_ns:
        return str(position), str(position), "0", True
    if position == 0 or position == len(samples):
        return "", "", "", False
    before, after = position - 1, position
    gap = samples[after] - samples[before]
    return str(before), str(after), str(gap), gap <= max_gap_ns


def iso_utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_capture(args: argparse.Namespace) -> None:
    session = args.session
    left_log = (session / "logs/left.log").read_text()
    right_log = (session / "logs/right.log").read_text()
    for side, role, log in (("left", "server", left_log), ("right", "client", right_log)):
        if f"Sync mode set to {role}" not in log or "Sync achieved!" not in log:
            raise TimingError(f"{side} camera did not confirm software sync as {role}")
    left_first_ns = first_frame_utc_ns(left_log, args.started_ns, args.finished_ns)
    right_first_ns = first_frame_utc_ns(right_log, args.started_ns, args.finished_ns)
    metadata = {
        "schema_version": 2,
        "session_id": args.session_id,
        "started_at_utc": iso_utc(args.started_ns),
        "finished_at_utc": iso_utc(args.finished_ns),
        "started_at_utc_ns": args.started_ns,
        "finished_at_utc_ns": args.finished_ns,
        "synchronization": "rpicam_software_sync",
        "left_sync_role": "server",
        "right_sync_role": "client",
        "left_sync_achieved": True,
        "right_sync_achieved": True,
        "first_frame_clock": "FrameWallClock_expected_validated_UTC",
        "left_first_frame_utc_ns": left_first_ns,
        "right_first_frame_utc_ns": right_first_ns,
        "imu_timestamp_method": "host_read_window_midpoint_not_sensor_sample_time",
        "imu_enabled": args.imu_enabled,
        "left_camera_index": args.left_camera,
        "right_camera_index": args.right_camera,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "bitrate": args.bitrate,
        "clip_duration_seconds": args.duration,
        "thermal_enabled": args.thermal_enabled == "true",
        "thermal_capture_status": args.thermal_status,
        "thermal_device": args.thermal_device,
        "thermal_timestamp_method": "v4l2_monotonic_converted_to_host_UTC_not_hardware_sync",
        "light_enabled": args.light_enabled == "true",
        "light_capture_status": args.light_status,
        "light_i2c_bus": args.light_bus,
        "light_address": args.light_address,
        "light_interval_seconds": args.light_interval,
        "light_mode": "continuous_high_resolution_mode2_0.5_lux",
        "light_timestamp_method": "host_read_window_midpoint_not_sensor_integration_time",
    }
    (session / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")


def read_imu(session: Path) -> list[int]:
    path = session / "imu.csv"
    if not path.is_file():
        raise TimingError("Missing IMU log")
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        needed = {"utc_ns", "read_start_utc_ns", "read_end_utc_ns", "read_duration_ns"}
        if not needed.issubset(reader.fieldnames or []):
            raise TimingError("IMU log lacks host read-window timestamps")
        samples = []
        for row in reader:
            middle = int(row["utc_ns"])
            start = int(row["read_start_utc_ns"])
            end = int(row["read_end_utc_ns"])
            if not start <= middle <= end or int(row["read_duration_ns"]) < 0:
                raise TimingError("Invalid IMU read window")
            samples.append(middle)
    if not samples or any(b <= a for a, b in zip(samples, samples[1:])):
        raise TimingError("IMU sample timestamps are missing or unordered")
    return samples


def frame_rows(session: Path, frames_dir: Path, side: str, planned: list[dict]) -> list[dict]:
    pts_ns = parse_showinfo((session / f"logs/extract_{side}.log").read_text())
    images = sorted((frames_dir / side).glob("frame_*.jpg"))
    expected = [f"frame_{index:06d}.jpg" for index in range(len(pts_ns))]
    if [path.name for path in images] != expected or any(path.stat().st_size == 0 for path in images):
        raise TimingError(f"{side}: JPEG count/names do not match selected source PTS")
    if pts_ns != [frame["source_pts_ns"] for frame in planned]:
        raise TimingError(f"{side}: extracted JPEG PTS do not match planned source frames")
    rows = []
    seen_buckets = set()
    for image, frame in zip(images, planned):
        pts = frame["source_pts_ns"]
        bucket = frame["time_bucket"]
        if bucket in seen_buckets:
            raise TimingError(f"{side}: multiple selected frames in time bucket {bucket}")
        seen_buckets.add(bucket)
        rows.append({
            "side": side, "file": f"frames/{side}/{image.name}",
            "source_pts_ns": pts, "capture_utc_ns": frame["capture_utc_ns"],
            "time_bucket": bucket, "paired": False,
        })
    return rows


def make_manifests(session: Path, frames_dir: Path, interval: Decimal, imu_interval: Decimal) -> dict:
    metadata = json.loads((session / "capture.json").read_text())
    if metadata.get("schema_version") != 2 or metadata.get("synchronization") != "rpicam_software_sync":
        raise TimingError("Session does not have validated v2 software-sync metadata")
    if not metadata.get("imu_enabled"):
        raise TimingError("IMU was not enabled for this synchronized session")
    interval_ns = int(interval * 1_000_000_000)
    if interval_ns <= 0:
        raise TimingError("Frame interval must be positive")
    planned = json.loads((session / "logs/frame_selection.json").read_text())
    left = frame_rows(session, frames_dir, "left", planned["left"])
    right = frame_rows(session, frames_dir, "right", planned["right"])
    imu = read_imu(session)
    right_by_bucket = {row["time_bucket"]: row for row in right}
    pair_rows = []
    skew_limit_ns = int(Decimal(1_000_000_000) / (Decimal(2) * Decimal(metadata["fps"])))
    max_imu_gap_ns = int(imu_interval * 2 * 1_000_000_000) + 10_000_000
    for left_row in left:
        right_row = right_by_bucket.get(left_row["time_bucket"])
        if right_row is None:
            continue
        left_row["paired"] = right_row["paired"] = True
        left_ns = left_row["capture_utc_ns"]
        right_ns = right_row["capture_utc_ns"]
        middle_ns = (left_ns + right_ns) // 2
        before, after, gap, bracketed = bracket_imu(imu, middle_ns, max_imu_gap_ns)
        pair_rows.append({
            "pair_index": len(pair_rows),
            "nominal_time_seconds": str(Decimal(left_row["time_bucket"]) * interval),
            "left_file": left_row["file"], "right_file": right_row["file"],
            "left_capture_utc_ns": left_ns, "right_capture_utc_ns": right_ns,
            "stereo_skew_ns": abs(left_ns - right_ns),
            "pair_capture_utc_ns": middle_ns,
            "imu_before_index": before, "imu_after_index": after,
            "imu_gap_ns": gap,
            "stereo_synced": abs(left_ns - right_ns) <= skew_limit_ns,
            "imu_bracketed": bracketed,
        })
    with (session / ".pairs.partial").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        writer.writerows(pair_rows)
    with (session / ".frames_manifest.partial").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FRAME_FIELDS)
        writer.writeheader()
        writer.writerows(left + right)
    return {
        "schema_version": 1,
        "left_images": len(left), "right_images": len(right), "pairs": len(pair_rows),
        "unpaired_left_images": sum(not row["paired"] for row in left),
        "unpaired_right_images": sum(not row["paired"] for row in right),
        "pairs_exceeding_stereo_skew_limit": sum(not row["stereo_synced"] for row in pair_rows),
        "pairs_without_close_imu_bracket": sum(not row["imu_bracketed"] for row in pair_rows),
        "stereo_skew_limit_ns": skew_limit_ns,
        "max_imu_gap_ns": max_imu_gap_ns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("session", type=Path)
    capture.add_argument("session_id")
    capture.add_argument("started_ns", type=int)
    capture.add_argument("finished_ns", type=int)
    capture.add_argument("left_camera", type=int)
    capture.add_argument("right_camera", type=int)
    capture.add_argument("width", type=int)
    capture.add_argument("height", type=int)
    capture.add_argument("fps", type=int)
    capture.add_argument("bitrate", type=int)
    capture.add_argument("duration", type=float)
    capture.add_argument("imu_enabled", choices=("true", "false"))
    capture.add_argument("thermal_enabled", choices=("true", "false"))
    capture.add_argument("thermal_status")
    capture.add_argument("thermal_device")
    capture.add_argument("light_enabled", choices=("true", "false"))
    capture.add_argument("light_status")
    capture.add_argument("light_bus", type=int)
    capture.add_argument("light_address")
    capture.add_argument("light_interval", type=float)
    extract = sub.add_parser("extract")
    extract.add_argument("session", type=Path)
    extract.add_argument("frames_dir", type=Path)
    extract.add_argument("interval", type=Decimal)
    extract.add_argument("imu_interval", type=Decimal)
    plan = sub.add_parser("plan")
    plan.add_argument("session", type=Path)
    plan.add_argument("interval", type=Decimal)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            args.imu_enabled = args.imu_enabled == "true"
            write_capture(args)
        elif args.command == "plan":
            plan_extraction(args.session, args.interval)
        else:
            report = make_manifests(args.session, args.frames_dir, args.interval, args.imu_interval)
            (args.session / ".sync_report.partial").write_text(json.dumps(report, indent=2) + "\n")
    except (TimingError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"TIMING ERROR: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
