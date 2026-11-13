#!/usr/bin/env python3
"""Log BH1750 illuminance with best-effort host read-window timestamps."""

import argparse
import csv
import math
from pathlib import Path
import signal
import sys
import time


FIELDS = (
    "utc_ns", "monotonic_ns", "lux", "read_start_utc_ns",
    "read_end_utc_ns", "read_duration_ns",
)
running = True


def stop(_signum, _frame):
    global running
    running = False


def read_row(sensor, wall_clock=time.time_ns, monotonic_clock=time.monotonic_ns):
    start_utc = wall_clock()
    start_mono = monotonic_clock()
    lux = float(sensor.lux)
    end_mono = monotonic_clock()
    end_utc = wall_clock()
    if not math.isfinite(lux) or lux < 0:
        raise ValueError(f"Invalid BH1750 lux reading: {lux}")
    return ((start_utc + end_utc) // 2, (start_mono + end_mono) // 2,
            lux, start_utc, end_utc, end_mono - start_mono)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x23)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--duration", type=float, help="Standalone test duration in seconds")
    args = parser.parse_args()
    if args.bus < 0 or args.address not in (0x23, 0x5C):
        parser.error("bus must be nonnegative and address must be 0x23 or 0x5c")
    if not math.isfinite(args.interval) or args.interval < 0.2:
        parser.error("interval must be at least 0.2 seconds for high-resolution mode")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("duration must be positive")

    from adafruit_bh1750 import BH1750, Mode, Resolution
    from adafruit_extended_bus import ExtendedI2C

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sensor = BH1750(ExtendedI2C(args.bus), address=args.address)
    sensor.mode = Mode.CONTINUOUS
    sensor.resolution = Resolution.HIGH
    deadline = time.monotonic() + args.duration if args.duration else None
    with args.output.open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(FIELDS)
        output.flush()
        failures = 0
        last_utc = -1
        ready = False
        while running and (deadline is None or time.monotonic() < deadline):
            start = time.monotonic()
            try:
                row = read_row(sensor)
                if row[0] <= last_utc:
                    raise ValueError("Non-increasing host UTC timestamps")
                writer.writerow(row)
                output.flush()
                last_utc = row[0]
                failures = 0
                if args.ready and not ready:
                    args.ready.touch()
                    ready = True
            except (OSError, RuntimeError, ValueError) as error:
                failures += 1
                print(f"BH1750 read failed ({failures}): {error}", file=sys.stderr, flush=True)
                if failures >= 10:
                    return 1
            time.sleep(max(0.0, args.interval - (time.monotonic() - start)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
