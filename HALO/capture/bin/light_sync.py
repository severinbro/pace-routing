#!/usr/bin/env python3
"""Associate host-timestamped BH1750 readings with stereo pair times."""

from bisect import bisect_left
import csv
import json
import math
from pathlib import Path
import sys


MAX_GAP_NS = 250_000_000
FIELDS = ("light_sample_index", "light_lux", "light_gap_ns", "light_status")


def read_samples(path: Path) -> tuple[list[dict], str]:
    if not path.is_file():
        return [], "light.csv missing"
    try:
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"utc_ns", "monotonic_ns", "lux", "read_start_utc_ns",
                        "read_end_utc_ns", "read_duration_ns"}
            if not required.issubset(reader.fieldnames or []):
                return [], "light.csv header incomplete"
            samples = []
            for row in reader:
                middle = int(row["utc_ns"])
                start = int(row["read_start_utc_ns"])
                end = int(row["read_end_utc_ns"])
                duration = int(row["read_duration_ns"])
                lux = float(row["lux"])
                if not start <= middle <= end or duration < 0 or not math.isfinite(lux) or lux < 0:
                    raise ValueError("Invalid light read window or lux")
                if samples and middle <= samples[-1]["utc_ns"]:
                    raise ValueError("Light timestamps are not strictly increasing")
                samples.append({"utc_ns": middle, "lux": row["lux"]})
        return samples, "" if samples else "light.csv has no readings"
    except (OSError, ValueError, TypeError, KeyError) as error:
        return [], f"light.csv invalid: {error}"


def nearest(samples: list[dict], target_ns: int) -> tuple[int, int] | None:
    if not samples:
        return None
    times = [sample["utc_ns"] for sample in samples]
    position = bisect_left(times, target_ns)
    candidates = [index for index in (position - 1, position) if 0 <= index < len(times)]
    index = min(candidates, key=lambda item: (abs(times[item] - target_ns), item))
    return index, abs(times[index] - target_ns)


def process(session: Path) -> None:
    capture = json.loads((session / "capture.json").read_text())
    if not capture.get("light_enabled"):
        return
    samples, source_error = read_samples(session / "light.csv")
    with (session / ".pairs.partial").open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "pair_capture_utc_ns" not in fields or any(field in fields for field in FIELDS):
        raise ValueError("Stereo pair manifest missing time or already has light fields")
    matched = 0
    for row in rows:
        row.update(dict.fromkeys(FIELDS, ""))
        row["light_status"] = "missing"
        match = nearest(samples, int(row["pair_capture_utc_ns"]))
        if match is None:
            continue
        index, gap = match
        row["light_gap_ns"] = str(gap)
        if gap <= MAX_GAP_NS:
            row["light_sample_index"] = str(index)
            row["light_lux"] = samples[index]["lux"]
            row["light_status"] = "ok"
            matched += 1
    with (session / ".pairs_light.partial").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields + list(FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    (session / ".pairs_light.partial").replace(session / ".pairs.partial")
    report_path = session / ".sync_report.partial"
    report = json.loads(report_path.read_text())
    report.update({"light_samples": len(samples), "pairs_with_light": matched,
                   "pairs_without_light": len(rows) - matched,
                   "light_max_pair_gap_ns": MAX_GAP_NS,
                   "light_source_error": source_error})
    report_path.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    try:
        process(Path(sys.argv[1]))
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"LIGHT ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
