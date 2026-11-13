#!/usr/bin/env python3
"""Match lossless USB thermal frames to stereo pairs without hiding timing gaps."""

from bisect import bisect_left
import csv
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib


WIDTH = 256
HEIGHT = 192
FRAME_BYTES = WIDTH * HEIGHT * 4  # two 256x192 planes, two bytes per pixel
SKEW_LIMIT_NS = 50_000_000
MAX_SELECTION_GAP_NS = 250_000_000
THERMAL_FIELDS = (
    "pair_index", "raw_file", "preview_file", "color_file", "thermal_capture_utc_ns",
    "thermal_skew_ns", "thermal_synced", "thermal_status",
)
COLOR_STOPS = (
    (0, (0, 0, 0)),
    (51, (40, 0, 85)),
    (102, (126, 15, 111)),
    (153, (205, 44, 54)),
    (204, (251, 140, 28)),
    (255, (255, 252, 190)),
)


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))


def grayscale_png(pixels: bytes) -> bytes:
    if len(pixels) != WIDTH * HEIGHT:
        raise ValueError("Invalid preview pixel count")
    scanlines = b"".join(b"\0" + pixels[y * WIDTH:(y + 1) * WIDTH]
                         for y in range(HEIGHT))
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 0, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(scanlines)) + png_chunk(b"IEND", b""))


def color_limits() -> tuple[float, float]:
    low = float(os.environ.get("THERMAL_COLOR_MIN_C", "0"))
    high = float(os.environ.get("THERMAL_COLOR_MAX_C", "60"))
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise ValueError("THERMAL_COLOR_MIN_C and THERMAL_COLOR_MAX_C must be finite, increasing values")
    return low, high


def color_palette() -> bytes:
    palette = bytearray()
    for level in range(256):
        for (start, first), (end, second) in zip(COLOR_STOPS, COLOR_STOPS[1:]):
            if start <= level <= end:
                palette.extend(round(a + (b - a) * (level - start) / (end - start))
                               for a, b in zip(first, second))
                break
    return bytes(palette)


COLOR_PALETTE = color_palette()


def absolute_color_png(raw: bytes, low_c: float, high_c: float) -> bytes:
    """Colorize Kelvin-times-64 counts on one fixed Celsius scale."""
    if len(raw) != WIDTH * HEIGHT * 2:
        raise ValueError("Invalid thermal temperature-grid size")
    span = high_c - low_c
    pixels = bytearray()
    for offset in range(0, len(raw), 2):
        count = raw[offset] | raw[offset + 1] << 8
        celsius = count / 64.0 - 273.15
        level = max(0, min(255, round((celsius - low_c) * 255 / span)))
        pixels.extend(COLOR_PALETTE[level * 3:level * 3 + 3])
    row_bytes = WIDTH * 3
    scanlines = b"".join(b"\0" + pixels[y * row_bytes:(y + 1) * row_bytes]
                         for y in range(HEIGHT))
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(scanlines)) + png_chunk(b"IEND", b""))


def split_frame(frame: bytes) -> tuple[bytes, bytes]:
    """Return rotated uint16-LE counts and rotated 8-bit grayscale pixels."""
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"Thermal frame has {len(frame)} bytes; expected {FRAME_BYTES}")
    image_bytes = frame[:WIDTH * HEIGHT * 2]
    raw = frame[WIDTH * HEIGHT * 2:]
    if raw == b"\0" * len(raw) or raw == b"\x00\x80" * (WIDTH * HEIGHT):
        raise ValueError("Thermal measurement plane is blank or in calibration")
    # Reverse whole pixels, preserving the two bytes within each little-endian sample.
    rotated_raw = b"".join(raw[i:i + 2] for i in range(len(raw) - 2, -1, -2))
    rotated_preview = image_bytes[::2][::-1]
    return rotated_raw, rotated_preview


def nearest_frame(times: list[int], target: int) -> tuple[int, int] | None:
    if not times:
        return None
    position = bisect_left(times, target)
    candidates = [index for index in (position - 1, position) if 0 <= index < len(times)]
    index = min(candidates, key=lambda item: (abs(times[item] - target), item))
    skew = abs(times[index] - target)
    return (index, skew) if skew <= MAX_SELECTION_GAP_NS else None


def probe_frames(source: Path, started_ns: int, finished_ns: int) -> list[int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=time_base,width,height:frame=best_effort_timestamp", "-of", "json", str(source)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if len(streams) != 1 or (streams[0].get("width"), streams[0].get("height")) != (WIDTH, HEIGHT * 2):
        raise ValueError("Thermal source is not a 256x384 video stream")
    numerator, denominator = map(int, streams[0]["time_base"].split("/"))
    if numerator <= 0 or denominator <= 0:
        raise ValueError("Invalid thermal source time base")
    frames = data.get("frames", [])
    if not frames:
        raise ValueError("Thermal source has no frames")
    times = [int(frame["best_effort_timestamp"]) * numerator * 1_000_000_000 // denominator
             for frame in frames]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Thermal frame times are unordered or duplicated")
    if times[0] < started_ns - 10_000_000_000 or times[-1] > finished_ns + 10_000_000_000:
        raise ValueError("Thermal source timestamps are not plausible host UTC")
    return times


def decode_selected(source: Path, indices: list[int], log_path: Path):
    expression = "select='" + "+".join(f"eq(n,{index})" for index in indices) + "'"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
               "-vf", expression, "-fps_mode", "passthrough", "-pix_fmt", "yuyv422",
               "-f", "rawvideo", "pipe:1"]
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log)
        try:
            for _ in indices:
                pieces = bytearray()
                while len(pieces) < FRAME_BYTES:
                    chunk = process.stdout.read(FRAME_BYTES - len(pieces))
                    if not chunk:
                        break
                    pieces.extend(chunk)
                yield bytes(pieces)
            extra = process.stdout.read(1)
            if process.wait() or extra:
                raise ValueError("Thermal decode count or FFmpeg status did not match selection")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def process(session: Path, frames_tmp: Path) -> None:
    capture = json.loads((session / "capture.json").read_text())
    if not capture.get("thermal_enabled"):
        return
    low_c, high_c = color_limits()
    with (session / ".pairs.partial").open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        pairs = list(reader)
    if not fields or any("pair_capture_utc_ns" not in row for row in pairs):
        raise ValueError("Stereo pair manifest is missing capture timestamps")
    thermal_dir = frames_tmp / "thermal"
    thermal_dir.mkdir(parents=True, exist_ok=True)
    source = session / ".thermal.partial/thermal.nut"
    times = []
    reason = ""
    if source.is_file() and source.stat().st_size:
        try:
            times = probe_frames(source, int(capture["started_at_utc_ns"]),
                                 int(capture["finished_at_utc_ns"]))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            reason = f"Thermal source unusable: {exc}"
    else:
        reason = "Thermal source missing"

    selected = {}
    for row in pairs:
        match = nearest_frame(times, int(row["pair_capture_utc_ns"]))
        if match:
            selected[int(row["pair_index"])] = match
    unique_indices = sorted({index for index, _ in selected.values()})
    valid_indices = set()
    if unique_indices:
        try:
            frames = decode_selected(source, unique_indices, session / "logs/thermal_extract.log")
            for index in unique_indices:
                frame = next(frames)
                try:
                    raw, preview = split_frame(frame)
                except ValueError:
                    continue
                png = grayscale_png(preview)
                color_png = absolute_color_png(raw, low_c, high_c)
                for pair_index, (chosen_index, _) in selected.items():
                    if chosen_index == index:
                        stem = f"frame_{pair_index:06d}"
                        (thermal_dir / f"{stem}.raw").write_bytes(raw)
                        (thermal_dir / f"{stem}.png").write_bytes(png)
                        (thermal_dir / f"{stem}_color.png").write_bytes(color_png)
                valid_indices.add(index)
            next(frames, None)  # finish the generator and check FFmpeg's status/count
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = f"Thermal decode failed: {exc}"

    thermal_rows = []
    for row in pairs:
        pair_index = int(row["pair_index"])
        item = {name: "" for name in THERMAL_FIELDS}
        item["pair_index"] = str(pair_index)
        item["thermal_status"] = "missing"
        if pair_index in selected:
            frame_index, skew_ns = selected[pair_index]
            item["thermal_capture_utc_ns"] = str(times[frame_index])
            item["thermal_skew_ns"] = str(skew_ns)
            item["thermal_synced"] = str(skew_ns <= SKEW_LIMIT_NS)
            if frame_index in valid_indices:
                stem = f"frame_{pair_index:06d}"
                item["raw_file"] = f"frames/thermal/{stem}.raw"
                item["preview_file"] = f"frames/thermal/{stem}.png"
                item["color_file"] = f"frames/thermal/{stem}_color.png"
                item["thermal_status"] = "ok" if skew_ns <= SKEW_LIMIT_NS else "skew"
            else:
                item["thermal_status"] = "invalid"
                item["thermal_synced"] = "False"
        thermal_rows.append(item)
        row.update({key: value for key, value in item.items() if key != "pair_index"})

    with (session / ".pairs_thermal.partial").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields + list(THERMAL_FIELDS[1:]))
        writer.writeheader()
        writer.writerows(pairs)
    (session / ".pairs_thermal.partial").replace(session / ".pairs.partial")
    with (session / ".thermal_manifest.partial").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=THERMAL_FIELDS)
        writer.writeheader()
        writer.writerows(thermal_rows)
    metadata = {
        "schema_version": 1, "model_usb_id": "0bda:5830", "width": WIDTH, "height": HEIGHT,
        "raw_dtype": "uint16_le", "raw_unit": "Kelvin_times_64",
        "celsius_formula": "raw / 64.0 - 273.15",
        "temperature_note": "Apparent temperature; emissivity and absolute accuracy are not calibrated here",
        "preview_format": "grayscale_png_8bit", "rotation_degrees": 180,
        "color_format": "rgb_png_8bit", "color_file_suffix": "_color.png",
        "color_palette": "fixed_black_purple_red_orange_yellow",
        "color_min_c": low_c, "color_max_c": high_c,
        "color_out_of_range": "clamped_to_end_colors",
        "color_stops": [{"level": level, "rgb": list(rgb)} for level, rgb in COLOR_STOPS],
        "timestamp_method": "V4L2_monotonic_to_host_UTC_via_FFmpeg_not_hardware_sync",
        "capture_status": capture.get("thermal_capture_status"), "source_error": reason,
        "max_pair_skew_ns": SKEW_LIMIT_NS,
        "max_selection_gap_ns": MAX_SELECTION_GAP_NS,
    }
    (session / ".thermal_metadata.partial").write_text(json.dumps(metadata, indent=2) + "\n")
    report_path = session / ".sync_report.partial"
    report = json.loads(report_path.read_text())
    report.update({
        "thermal_samples": sum(item["thermal_status"] in ("ok", "skew") for item in thermal_rows),
        "pairs_without_thermal": sum(item["thermal_status"] in ("missing", "invalid") for item in thermal_rows),
        "pairs_exceeding_thermal_skew_limit": sum(item["thermal_status"] == "skew" for item in thermal_rows),
        "thermal_skew_limit_ns": SKEW_LIMIT_NS,
    })
    report_path.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    try:
        process(Path(sys.argv[1]), Path(sys.argv[2]))
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"THERMAL ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
