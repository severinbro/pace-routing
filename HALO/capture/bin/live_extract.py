#!/usr/bin/env python3
"""Decoupled read-only live frame extractor for growing HALO left.mkv clips.

Periodically pulls the latest flushed keyframe from left.mkv without touching
the active rpicam-vid process, applies configured rotation, and atomically
writes frame_%06d.jpg + frame_%06d.meta.json into DATA_ROOT/live_outbox/<id>/.
"""

import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import time

from live_outbox import enforce_cap, locked, sync_dir, write_json


LOG = logging.getLogger("halo_live_extract")
STOP_REQUESTED = False


def _handle_signal(_signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def rotation_filter(rotation: int) -> str | None:
    if rotation == 180:
        return "hflip,vflip"
    if rotation == 90:
        return "transpose=1"
    if rotation == 270:
        return "transpose=2"
    return None


def extract_latest_frame(video_path: Path, tmp_jpg: Path, rotation: int, quality: int) -> bool:
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return False
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-sseof", "-1",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", str(quality),
    ]
    vf = rotation_filter(rotation)
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(["-f", "image2", str(tmp_jpg)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("Live frame ffmpeg invocation failed: %s", exc)
        tmp_jpg.unlink(missing_ok=True)
        return False
    if proc.returncode != 0 or not tmp_jpg.is_file() or tmp_jpg.stat().st_size == 0:
        tmp_jpg.unlink(missing_ok=True)
        return False
    return True


def run_loop(
    video_path: Path,
    outbox_dir: Path,
    session_id: str,
    start_utc_ns: int,
    interval_seconds: float,
    rotation: int,
    quality: int,
) -> int:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    start_mono_ns = time.monotonic_ns()
    max_frames = int(os.getenv("LIVE_OUTBOX_MAX_FRAMES", "120"))
    max_bytes = int(os.getenv("LIVE_OUTBOX_MAX_BYTES", str(256 << 20)))
    if max_frames < 1 or max_bytes < 1:
        raise ValueError("live outbox limits must be positive")
    with locked(outbox_dir.parent):
        existing_indices = [int(path.stem.removeprefix("frame_")) for path in outbox_dir.glob("frame_??????.jpg")
                            if path.stem.removeprefix("frame_").isdecimal()]
        frame_index = max(existing_indices, default=-1) + 1
    next_tick = time.monotonic() + interval_seconds

    while not STOP_REQUESTED:
        now = time.monotonic()
        if now < next_tick:
            time.sleep(min(0.25, next_tick - now))
            continue
        next_tick = now + interval_seconds

        stem = f"frame_{frame_index:06d}"
        tmp_jpg = outbox_dir / f".{stem}.jpg.tmp"
        final_jpg = outbox_dir / f"{stem}.jpg"
        final_meta = outbox_dir / f"{stem}.meta.json"

        capture_utc_ns = start_utc_ns + max(0, time.monotonic_ns() - start_mono_ns)
        if not extract_latest_frame(video_path, tmp_jpg, rotation=rotation, quality=quality):
            continue

        meta = {
            "session_id": session_id,
            "frame_index": frame_index,
            "capture_utc_ns": capture_utc_ns,
        }
        try:
            with locked(outbox_dir.parent):
                with tmp_jpg.open("rb") as image:
                    os.fsync(image.fileno())
                os.replace(tmp_jpg, final_jpg)
                sync_dir(outbox_dir)
                try:
                    write_json(final_meta, meta)
                except OSError:
                    if not final_meta.exists():
                        final_jpg.unlink(missing_ok=True)
                    raise
                enforce_cap(outbox_dir.parent, max_frames, max_bytes)
            frame_index += 1
        except OSError as exc:
            LOG.warning("Failed committing live frame %s: %s", stem, exc)
            tmp_jpg.unlink(missing_ok=True)
            frame_index += 1
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--start-utc-ns", type=int, required=True)
    parser.add_argument("--interval", type=float, default=3.5)
    parser.add_argument("--rotation", type=int, default=180)
    parser.add_argument("--quality", type=int, default=4)
    args = parser.parse_args()

    interval = max(0.5, args.interval)
    return run_loop(
        video_path=args.video,
        outbox_dir=args.outbox,
        session_id=args.session_id,
        start_utc_ns=args.start_utc_ns,
        interval_seconds=interval,
        rotation=args.rotation,
        quality=args.quality,
    )


if __name__ == "__main__":
    raise SystemExit(main())
