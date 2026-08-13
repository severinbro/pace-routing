#!/usr/bin/env python3
"""
Enrich a PACE measurements CSV with weather data from Open-Meteo.

Takes as input the sensor CSV produced by the PACE export endpoints (the same
file consumed by ``collapse_survey_points.py`` / ``extract_amenities.py``):

    pace_sensor_export_20260813_120000.csv

For every row the script looks up the weather at the row's GNSS coordinates
(``latitude`` / ``longitude``) and timestamp using the Open-Meteo forecast API
(https://api.open-meteo.com/v1/forecast?).  The parameters of interest are:

    * ``temperature_2m``        — air temperature at 2 m above ground (°C)
    * ``relative_humidity_2m``  — relative humidity at 2 m (%)
    * ``surface_pressure``      — surface pressure (hPa)

The output is the *original* CSV with three extra columns appended:

    temperature_2m, relative_humidity_2m, surface_pressure

Output file naming follows the same convention as ``extract_amenities.py``:
the suffix ``_weather`` is inserted before the extension, e.g.

    pace_sensor_export_20260813_120000.csv
    -> pace_sensor_export_20260813_120000_weather.csv

API strategy
------------
Open-Meteo returns 15-minute values (``minutely_15``).  Rather than issuing
one request per row (which would be slow and rate-limited), the script
groups rows by a rounded location key (default 0.01° ≈ 1 km) and issues a
single request per unique location covering the full date range of the
measurements that fall there.  Each measurement row is then matched to the
nearest 15-minute weather record by timestamp.

The forecast endpoint supports historical data via ``past_days`` (up to ~92
days back).  For older measurements pass ``--archive`` to use the Open-Meteo
Archive API (https://archive-api.open-meteo.com/v1/archive) which covers
several decades but is only updated with a few days' delay.  Note that the
Archive API exposes 15-minute data only for recent years; for very old
records it may fall back to hourly resolution, in which case the nearest-hour
match is used.

Usage
-----

    python enrich_weather.py \
        --sensors pace_sensor_export_20260813_120000.csv

    # older measurements — use the archive endpoint
    python enrich_weather.py \
        --sensors pace_sensor_export_20260101_120000.csv \
        --archive

All parameters have sensible defaults and can be overridden.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pandas as pd


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

WEATHER_VARS = ("temperature_2m", "relative_humidity_2m", "surface_pressure")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_sensors(path: str) -> pd.DataFrame:
    """Loads the measurements CSV and normalises coordinate/timestamp types."""
    df = pd.read_csv(path)
    for col in ("latitude", "longitude"):
        if col not in df.columns:
            raise ValueError(
                f"CSV is missing required column '{col}'. "
                "The measurements CSV must contain 'latitude' and 'longitude'."
            )
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "timestamp" not in df.columns:
        raise ValueError(
            "CSV is missing required column 'timestamp'. "
            "The measurements CSV must contain a 'timestamp' column."
        )
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df


# --------------------------------------------------------------------------- #
# Open-Meteo requests
# --------------------------------------------------------------------------- #
def _om_request(params: dict, url: str, max_retries: int = 5) -> dict:
    """GETs the Open-Meteo API with query params and returns parsed JSON.

    Retries with exponential backoff on 429 / 5xx / network errors.
    """
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    req = urllib.request.Request(
        full, headers={"User-Agent": "PACE-weather/1.0"}
    )
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or 500 <= e.code < 600:
                wait = 2 ** attempt
                print(f"[weather]   Open-Meteo {e.code}, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(wait)
                # Rebuild request — urlopen consumed the previous one.
                req = urllib.request.Request(
                    full, headers={"User-Agent": "PACE-weather/1.0"}
                )
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[weather]   network error ({e}), retrying in {wait}s "
                  f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            req = urllib.request.Request(
                full, headers={"User-Agent": "PACE-weather/1.0"}
            )
    raise RuntimeError(
        f"Open-Meteo request failed after {max_retries} attempts: {last_err}"
    )


def fetch_weather(lat: float, lon: float,
                  start_date: str, end_date: str,
                  url: str, use_archive: bool) -> pd.DataFrame:
    """Fetches 15-minute weather for one location over a date range.

    Uses Open-Meteo's ``minutely_15`` field when available.  If the endpoint
    does not return 15-minute data (e.g. the Archive API for very old dates),
    falls back to the ``hourly`` field so enrichment still works.

    Returns a DataFrame with columns:
        time (UTC, datetime), temperature_2m, relative_humidity_2m,
        surface_pressure
    """
    base_params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "timezone": "UTC",
    }
    if use_archive:
        # Archive API uses start_date / end_date (YYYY-MM-DD).
        base_params["start_date"] = start_date
        base_params["end_date"] = end_date
    else:
        # Forecast API: span the range with past_days + forecast_days.
        # past_days max ~92; forecast_days covers the near future.
        base_params["past_days"] = 92
        base_params["forecast_days"] = 16

    # Prefer 15-minute resolution.  The forecast API exposes these variables
    # under minutely_15; the Archive API supports minutely_15 only for recent
    # dates, so we try it first and fall back to hourly on failure.
    params = dict(base_params)
    params["minutely_15"] = ",".join(WEATHER_VARS)
    try:
        data = _om_request(params, url)
    except Exception as e:
        if use_archive:
            print(f"[weather]   minutely_15 unavailable ({e}); falling back to hourly",
                  file=sys.stderr)
            data = None
        else:
            raise

    block = None
    if data and data.get("minutely_15"):
        block = data["minutely_15"]
        resolution = "15-min"
    elif data and data.get("hourly"):
        block = data["hourly"]
        resolution = "hourly"

    if block is None:
        # Retry explicitly with hourly so we get a usable payload.
        params = dict(base_params)
        params["hourly"] = ",".join(WEATHER_VARS)
        data = _om_request(params, url)
        block = data.get("hourly", {})
        resolution = "hourly"

    times = block.get("time", [])
    if not times:
        return pd.DataFrame(columns=["time"] + list(WEATHER_VARS))

    print(f"[weather]   resolution: {resolution} ({len(times)} records)")
    df = pd.DataFrame({
        "time": pd.to_datetime(times, utc=True),
        **{v: block.get(v, [None] * len(times)) for v in WEATHER_VARS},
    })
    return df


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def match_nearest(measurements: pd.DataFrame,
                  weather: pd.DataFrame) -> pd.DataFrame:
    """Matches each measurement to the nearest weather record by timestamp.

    Works with any weather resolution (15-minute or hourly).  The match
    tolerance is 30 minutes so that a 15-minute grid always binds while still
    tolerating an occasional hourly gap.

    ``measurements`` must have a UTC ``timestamp`` column.
    ``weather`` must have a UTC ``time`` column plus the WEATHER_VARS columns.
    Returns a copy of ``measurements`` with the weather columns appended.
    """
    if weather.empty:
        for v in WEATHER_VARS:
            measurements[v] = pd.NA
        return measurements

    # asof merge: requires both frames sorted by their time columns.
    weather_sorted = weather.sort_values("time").reset_index(drop=True)
    meas_sorted = measurements.sort_values("timestamp").reset_index()

    merged = pd.merge_asof(
        meas_sorted,
        weather_sorted,
        left_on="timestamp",
        right_on="time",
        direction="nearest",
        tolerance=pd.Timedelta(minutes=30),
    )

    # Drop the helper 'time' column from weather; restore original row order.
    if "time" in merged.columns:
        merged = merged.drop(columns=["time"])
    merged = merged.sort_values("index").drop(columns=["index"]).reset_index(drop=True)
    return merged


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def weather_output_path(sensors_path: str) -> str:
    """Appends ``_weather`` before the CSV extension of the sensors file."""
    root, ext = os.path.splitext(sensors_path)
    return f"{root}_weather{ext or '.csv'}"


def write_csv(df: pd.DataFrame, path: str) -> None:
    # Preserve the original timestamp formatting (ISO 8601 string) if it was
    # parsed into a datetime.  Other datetime columns are left as-is.
    out = df.copy()
    if "timestamp" in out.columns and pd.api.types.is_datetime64_any_dtype(out["timestamp"]):
        out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(
            "+0000", "+00:00"
        )
    out.to_csv(path, index=False, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Enrich a PACE measurements CSV with Open-Meteo weather "
                    "(temperature_2m, relative_humidity_2m, surface_pressure).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sensors", required=True,
                   help="Path to the measurements CSV export.")
    p.add_argument("--out", default=None,
                   help="Output CSV path. Defaults to <sensors>_weather.csv.")
    p.add_argument("--archive", action="store_true",
                   help="Use the Open-Meteo Archive API instead of the forecast "
                        "API.  Required for measurements older than ~92 days.")
    p.add_argument("--location-precision", type=int, default=2,
                   help="Decimal places used to round lat/lon when grouping rows "
                        "by location (reduces API calls). 2 ≈ 1 km.")
    p.add_argument("--delay", type=float, default=0.5,
                   help="Seconds to wait between location requests (be nice to "
                        "the public API).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not os.path.isfile(args.sensors):
        print(f"[weather] Error: sensors file not found: {args.sensors}",
              file=sys.stderr)
        return 2

    url = OPEN_METEO_ARCHIVE_URL if args.archive else OPEN_METEO_FORECAST_URL
    print(f"[weather] Using Open-Meteo endpoint: {url}")

    print(f"[weather] Loading measurements from {args.sensors}")
    df = load_sensors(args.sensors)
    print(f"[weather]   {len(df)} rows loaded")

    # Drop rows without usable coordinates or timestamp — they can't be enriched.
    valid = df.dropna(subset=["latitude", "longitude", "timestamp"]).copy()
    skipped = len(df) - len(valid)
    if skipped:
        print(f"[weather]   {skipped} row(s) skipped (missing lat/lon/timestamp)",
              file=sys.stderr)

    if valid.empty:
        print("[weather] No rows with valid coordinates/timestamps — nothing to do.",
              file=sys.stderr)
        # Still write the output with empty weather columns for schema stability.
        for v in WEATHER_VARS:
            df[v] = pd.NA
        out_path = args.out or weather_output_path(args.sensors)
        write_csv(df, out_path)
        return 0

    # Group rows by rounded location to minimise API calls.
    prec = args.location_precision
    valid["_loc_key"] = (
        valid["latitude"].round(prec).astype(str)
        + ","
        + valid["longitude"].round(prec).astype(str)
    )

    # Cache weather DataFrames per location key.
    weather_cache: dict[str, pd.DataFrame] = {}

    groups = valid.groupby("_loc_key", sort=False)
    n_groups = len(groups)
    print(f"[weather] {n_groups} unique location(s) to query")

    for i, (loc_key, grp) in enumerate(groups, 1):
        lat = grp["latitude"].iloc[0]
        lon = grp["longitude"].iloc[0]
        grp_min = grp["timestamp"].min().floor("h")
        grp_max = grp["timestamp"].max().ceil("h")

        # Pad the range by one day on each side so the nearest-record match
        # has neighbours to bind to around the edges.
        start_date = (grp_min - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (grp_max + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"[weather] Location {i}/{n_groups}: ({lat:.4f}, {lon:.4f})  "
              f"range {start_date}..{end_date}  ({len(grp)} rows)")

        try:
            wdf = fetch_weather(lat, lon, start_date, end_date, url, args.archive)
        except Exception as e:
            print(f"[weather]   request failed: {e}", file=sys.stderr)
            wdf = pd.DataFrame(columns=["time"] + list(WEATHER_VARS))

        print(f"[weather]   received {len(wdf)} weather record(s)")
        weather_cache[loc_key] = wdf

        if i < n_groups and args.delay > 0:
            time.sleep(args.delay)

    # Match each row to the nearest hourly weather record within its location.
    print("[weather] Matching measurements to nearest weather record...")
    enriched_parts = []
    for loc_key, grp in groups:
        wdf = weather_cache.get(loc_key)
        if wdf is None or wdf.empty:
            for v in WEATHER_VARS:
                grp = grp.copy()
                grp[v] = pd.NA
            enriched_parts.append(grp)
            continue
        matched = match_nearest(grp, wdf)
        enriched_parts.append(matched)

    enriched_valid = pd.concat(enriched_parts, ignore_index=True)

    # Re-merge enriched rows back into the original frame (preserving original
    # row order and any rows that were skipped due to missing coords/timestamp).
    keep_cols = list(WEATHER_VARS)
    enriched_valid = enriched_valid[keep_cols]
    # Align by position with the `valid` subset of `df`.
    valid_idx = valid.index
    for v in keep_cols:
        df[v] = pd.NA
        df.loc[valid_idx, v] = enriched_valid[v].values

    df = df.drop(columns=[c for c in ["_loc_key"] if c in df.columns])

    out_path = args.out or weather_output_path(args.sensors)
    print(f"[weather] Output -> {out_path}")
    write_csv(df, out_path)

    # Report coverage.
    have = int(df[WEATHER_VARS[0]].notna().sum())
    print(f"[weather] {have}/{len(df)} row(s) enriched with weather data")
    print("[weather] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
