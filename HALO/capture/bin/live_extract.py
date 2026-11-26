#!/usr/bin/env python3
"""Decoupled read-only live frame extractor for growing HALO left.mkv clips.

Periodically pulls the latest flushed keyframe from left.mkv without touching
the active rpicam-vid process, applies configured rotation, and atomically
writes frame_%06d.jpg + frame_%06d.meta.json into DATA_ROOT/live_outbox/<id>/.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import time


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
    frame_index = 0
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
        tmp_meta = outbox_dir / f".{stem}.meta.json.tmp"
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
            tmp_meta.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            os.replace(tmp_jpg, final_jpg)
            os.replace(tmp_meta, final_meta)
            frame_index += 1
        except OSError as exc:
            LOG.warning("Failed committing live frame %s: %s", stem, exc)
            tmp_jpg.unlink(missing_ok=True)
            tmp_meta.unlink(missing_ok=True)
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
