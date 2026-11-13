#!/usr/bin/env python3
"""Record BNO085 readings with best-effort host read-window timestamps."""

import argparse
import csv
import signal
import sys
import time
from pathlib import Path

from adafruit_bno08x import (
    BNO_REPORT_ACCELEROMETER,
    BNO_REPORT_GYROSCOPE,
    BNO_REPORT_ROTATION_VECTOR,
)
from adafruit_bno08x.i2c import BNO08X_I2C
from adafruit_extended_bus import ExtendedI2C


running = True


def stop(_signum, _frame):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x4A)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sensor = BNO08X_I2C(ExtendedI2C(args.bus), address=args.address)
    report_interval_us = int(args.interval * 1_000_000)
    for feature in (
        BNO_REPORT_ACCELEROMETER,
        BNO_REPORT_GYROSCOPE,
        BNO_REPORT_ROTATION_VECTOR,
    ):
        sensor.enable_feature(feature, report_interval_us)

    fields = (
        "utc_ns", "monotonic_ns", "accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2",
        "gyro_x_rad_s", "gyro_y_rad_s", "gyro_z_rad_s",
        "quat_i", "quat_j", "quat_k", "quat_real",
        "read_start_utc_ns", "read_end_utc_ns", "read_duration_ns",
    )
    with args.output.open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(fields)
        output.flush()
        args.ready.touch()
        failures = 0
        while running:
            started = time.monotonic()
            read_start_utc_ns = time.time_ns()
            read_start_monotonic_ns = time.monotonic_ns()
            try:
                accel = sensor.acceleration
                gyro = sensor.gyro
                quat = sensor.quaternion
                read_end_monotonic_ns = time.monotonic_ns()
                read_end_utc_ns = time.time_ns()
                if sum(component * component for component in quat) < 0.25:
                    time.sleep(args.interval)
                    continue
                writer.writerow((
                    (read_start_utc_ns + read_end_utc_ns) // 2,
                    (read_start_monotonic_ns + read_end_monotonic_ns) // 2,
                    *accel, *gyro, *quat,
                    read_start_utc_ns, read_end_utc_ns,
                    read_end_monotonic_ns - read_start_monotonic_ns,
                ))
                output.flush()
                failures = 0
            except (OSError, RuntimeError, ValueError) as error:
                failures += 1
                print(f"IMU read failed ({failures}): {error}", file=sys.stderr, flush=True)
                if failures >= 10:
                    return 1
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
