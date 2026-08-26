#!/usr/bin/env python3
"""
Collapse survey-stop GNSS points and reassign survey GNSS links.

Takes as input the two files produced by the PACE export endpoints:

  * the sensor CSV  (``/export-csv/``        -> ``write_sensor_csv``)
  * the survey JSON (``/export-survey-json/`` -> ``write_survey_json``)

and produces two new files:

  1. ``--out-csv``  — the complete route GNSS/sensor data, downsampled to
     roughly one point every ``--min-spacing`` metres, with every survey
     stop collapsed into a *single* representative point (instead of the
     many overlapping points recorded while participants stood still and
     submitted their surveys).

  2. ``--out-json`` — the survey JSON with the same answers as the input,
     but every participant at a given survey stop is linked to the same
     ``gnss_snapshot_id`` (the id of the collapsed representative point),
     so the CSV and JSON stay referentially consistent.

Survey-stop detection
---------------------
Each survey entry carries a ``gnss_snapshot_id`` that points at a row in the
sensor CSV.  At a real survey stop several participants submit surveys within
a few minutes of each other, each linking to a slightly different GNSS
snapshot (different timestamp, slightly different coordinates because people
stand a few metres apart and the platform drifts).  These snapshots are
clustered together using a combined spatial + temporal threshold:

  * two surveys join the same cluster when their GNSS coordinates are within
    ``--spatial-threshold`` metres *and* their timestamps are within
    ``--time-window`` seconds of each other;

  * clustering is sequential (surveys sorted by timestamp), so a route that
    loops past the same spot an hour later correctly produces a *second*
    cluster rather than merging the two stops.

Once the clusters are known, *all* route points that fall inside a cluster's
spatial radius and temporal window are collapsed into one representative row
(median coordinates, averaged sensor values, primary id/timestamp).  This
removes the dense "blob" of stationary points recorded during the stop.

Usage
-----
    python collapse_survey_points.py \
        --surveys  pace_survey_export_20260813_120000.json \
        --sensors  pace_sensor_export_20260813_120000.csv \
        --out-csv  route_collapsed.csv \
        --out-json surveys_relinked.json

All distance/time parameters have sensible defaults and can be overridden.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from statistics import mean, median

import pandas as pd


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
_EARTH_RADIUS_M = 6_371_000.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_surveys(path: str) -> list[dict]:
    """Loads the survey export JSON (list of entries)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sensors(path: str) -> pd.DataFrame:
    """Loads the sensor export CSV and normalises types.

    The export writes ``timestamp`` as an ISO string; we parse it back to
    datetime so temporal filtering works.  ``latitude``/``longitude`` are
    already float in the export but are coerced defensively.
    """
    df = pd.read_csv(path)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for col in ("latitude", "longitude"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "id" in df.columns:
        df["id"] = pd.to_numeric(df["id"], errors="coerce").astype("Int64")
    return df


# --------------------------------------------------------------------------- #
# GNSS lookup
# --------------------------------------------------------------------------- #
def build_gnss_lookup(sensors: pd.DataFrame) -> dict[int, dict]:
    """Maps ``id -> {lat, lon, timestamp}`` for every row in the sensor CSV."""
    lookup: dict[int, dict] = {}
    for row in sensors.itertuples(index=False):
        rid = int(row.id) if pd.notna(row.id) else None
        if rid is None:
            continue
        lookup[rid] = {
            "id": rid,
            "latitude": float(row.latitude),
            "longitude": float(row.longitude),
            "timestamp": row.timestamp,
        }
    return lookup


# --------------------------------------------------------------------------- #
# Survey clustering
# --------------------------------------------------------------------------- #
class SurveyCluster:
    """A group of surveys submitted at one physical survey stop."""

    def __init__(self, first: dict):
        self.surveys: list[dict] = [first]
        self.gnss_ids: list[int] = [first["gnss_snapshot_id"]]
        self._lats: list[float] = []
        self._lons: list[float] = []
        self._timestamps: list[datetime] = []
        self._add_coords(first)

    # -- coordinate accumulation ------------------------------------------- #
    def _add_coords(self, survey: dict) -> None:
        pt = survey["_gnss_point"]
        if pt is not None:
            self._lats.append(pt["latitude"])
            self._lons.append(pt["longitude"])
        ts = survey.get("_ts")
        if ts is not None:
            self._timestamps.append(ts)

    def add(self, survey: dict) -> None:
        self.surveys.append(survey)
        if survey.get("gnss_snapshot_id") is not None:
            self.gnss_ids.append(survey["gnss_snapshot_id"])
        self._add_coords(survey)

    # -- cluster properties ------------------------------------------------ #
    @property
    def centroid_lat(self) -> float:
        return mean(self._lats) if self._lats else float("nan")

    @property
    def centroid_lon(self) -> float:
        return mean(self._lons) if self._lons else float("nan")

    @property
    def start_ts(self) -> datetime | None:
        return min(self._timestamps) if self._timestamps else None

    @property
    def end_ts(self) -> datetime | None:
        return max(self._timestamps) if self._timestamps else None

    @property
    def primary_gnss_id(self) -> int | None:
        """The gnss_snapshot_id whose timestamp is closest to the cluster median.

        This becomes the single id shared by every participant at the stop and
        the id of the collapsed representative row in the route CSV.
        """
        if not self._timestamps or not self.gnss_ids:
            return self.gnss_ids[0] if self.gnss_ids else None
        target = median(self._timestamps)
        # Pair each survey's gnss id with its timestamp distance from the median.
        best_id, best_delta = None, None
        for s in self.surveys:
            gid = s.get("gnss_snapshot_id")
            ts = s.get("_ts")
            if gid is None or ts is None:
                continue
            delta = abs((ts - target).total_seconds())
            if best_delta is None or delta < best_delta:
                best_id, best_delta = gid, delta
        return best_id if best_id is not None else (self.gnss_ids[0] if self.gnss_ids else None)


def cluster_surveys(
    surveys: list[dict],
    gnss_lookup: dict[int, dict],
    spatial_threshold: float,
    time_window: float,
) -> list[SurveyCluster]:
    """Groups surveys into physical stops using spatial + temporal thresholds.

    Surveys are sorted by timestamp.  A survey joins the current cluster when
    its GNSS coordinates are within ``spatial_threshold`` metres of the
    cluster centroid *and* its timestamp is within ``time_window`` seconds of
    the cluster's most recent survey.  Otherwise it starts a new cluster.

    This sequential approach means a route that revisits the same coordinates
    much later produces a separate cluster (the time gap exceeds the window).
    """
    # Annotate each survey with its GNSS point and parsed timestamp.
    annotated = []
    for s in surveys:
        gid = s.get("gnss_snapshot_id")
        pt = gnss_lookup.get(gid) if gid is not None else None
        ts_raw = s.get("timestamp")
        ts = None
        if ts_raw:
            try:
                ts = pd.to_datetime(ts_raw).to_pydatetime()
            except Exception:
                ts = None
        s2 = dict(s)
        s2["_gnss_point"] = pt
        s2["_ts"] = ts
        annotated.append(s2)

    # Sort by timestamp (None timestamps sort last).
    annotated.sort(key=lambda s: (s["_ts"] is None, s["_ts"]))

    clusters: list[SurveyCluster] = []
    for s in annotated:
        pt = s["_gnss_point"]
        ts = s["_ts"]
        placed = False
        if pt is not None and ts is not None and clusters:
            cur = clusters[-1]
            if cur.end_ts is not None:
                dt = abs((ts - cur.end_ts).total_seconds())
                if dt <= time_window:
                    d = haversine(pt["latitude"], pt["longitude"],
                                  cur.centroid_lat, cur.centroid_lon)
                    if d <= spatial_threshold:
                        cur.add(s)
                        placed = True
        if not placed:
            clusters.append(SurveyCluster(s))
    return clusters


# --------------------------------------------------------------------------- #
# Route collapsing
# --------------------------------------------------------------------------- #
def _collapse_rows(rows: pd.DataFrame, primary_id: int) -> pd.Series:
    """Reduces a group of route rows to one representative row.

    Numeric columns are averaged; the id and timestamp come from the primary
    row; non-numeric columns take the primary row's value.
    """
    primary = rows.loc[rows["id"] == primary_id]
    if primary.empty:
        primary = rows.iloc[[0]]
    primary = primary.iloc[0]

    out = primary.copy()
    for col in rows.columns:
        if col in ("id", "timestamp"):
            continue
        if pd.api.types.is_numeric_dtype(rows[col]):
            vals = pd.to_numeric(rows[col], errors="coerce").dropna()
            out[col] = float(vals.mean()) if not vals.empty else primary[col]
        else:
            out[col] = primary[col]
    return out


def collapse_route(
    sensors: pd.DataFrame,
    clusters: list[SurveyCluster],
    spatial_threshold: float,
    time_buffer: float,
    min_spacing: float,
) -> pd.DataFrame:
    """Builds the collapsed + downsampled route DataFrame.

    Steps:
      1. Mark every route point that belongs to a survey stop (within the
         stop's spatial radius and temporal window).
      2. Collapse each stop's points into one representative row keyed by the
         cluster's ``primary_gnss_id``.
      3. Downsample the remaining (non-stop) points to one every
         ``min_spacing`` metres along the route.
      4. Concatenate, sort by timestamp, return.
    """
    if sensors.empty:
        return sensors.copy()

    df = sensors.sort_values("timestamp").reset_index(drop=True).copy()
    df["_is_stop"] = False
    df["_cluster_id"] = -1

    # --- 1. Mark stop points ------------------------------------------------ #
    for idx, cluster in enumerate(clusters):
        pid = cluster.primary_gnss_id
        if pid is None or math.isnan(cluster.centroid_lat):
            continue
        clat, clon = cluster.centroid_lat, cluster.centroid_lon
        t0 = cluster.start_ts - timedelta(seconds=time_buffer) if cluster.start_ts else None
        t1 = cluster.end_ts + timedelta(seconds=time_buffer) if cluster.end_ts else None

        for i, row in df.iterrows():
            if df.at[i, "_is_stop"]:
                continue
            ts = row["timestamp"]
            if t0 is not None and t1 is not None:
                if pd.isna(ts) or ts < t0 or ts > t1:
                    continue
            d = haversine(row["latitude"], row["longitude"], clat, clon)
            if d <= spatial_threshold:
                df.at[i, "_is_stop"] = True
                df.at[i, "_cluster_id"] = idx

    # --- 2. Collapse stop points ------------------------------------------- #
    collapsed_rows = []
    for idx, cluster in enumerate(clusters):
        pid = cluster.primary_gnss_id
        if pid is None:
            continue
        stop_rows = df[df["_cluster_id"] == idx]
        if stop_rows.empty:
            # No route points matched (e.g. survey id not in CSV); fabricate a
            # row from the primary GNSS point so the CSV/JSON stay linked.
            pt = cluster.surveys[0].get("_gnss_point")
            if pt is None:
                continue
            row = {c: None for c in df.columns}
            row["id"] = pid
            row["latitude"] = pt["latitude"]
            row["longitude"] = pt["longitude"]
            row["timestamp"] = pt["timestamp"]
            row["_is_stop"] = True
            row["_cluster_id"] = idx
            collapsed_rows.append(pd.Series(row))
        else:
            collapsed_rows.append(_collapse_rows(stop_rows, pid))

    # --- 3. Downsample non-stop points to min_spacing ---------------------- #
    non_stop = df[~df["_is_stop"]].copy()
    kept = []
    last_lat = last_lon = None
    for _, row in non_stop.iterrows():
        if last_lat is None:
            kept.append(row)
            last_lat, last_lon = row["latitude"], row["longitude"]
            continue
        d = haversine(last_lat, last_lon, row["latitude"], row["longitude"])
        if d >= min_spacing:
            kept.append(row)
            last_lat, last_lon = row["latitude"], row["longitude"]
    non_stop_kept = pd.DataFrame(kept, columns=df.columns) if kept else pd.DataFrame(columns=df.columns)

    # --- 4. Combine & sort ------------------------------------------------- #
    parts = []
    if collapsed_rows:
        parts.append(pd.DataFrame(collapsed_rows, columns=df.columns))
    if not non_stop_kept.empty:
        parts.append(non_stop_kept)

    if not parts:
        return df.drop(columns=["_is_stop", "_cluster_id"])

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    return combined.drop(columns=["_is_stop", "_cluster_id"])


# --------------------------------------------------------------------------- #
# Survey JSON relinking
# --------------------------------------------------------------------------- #
def relink_surveys(
    surveys: list[dict],
    clusters: list[SurveyCluster],
) -> list[dict]:
    """Returns a copy of the survey entries with unified ``gnss_snapshot_id``.

    Every participant at a given stop receives the cluster's
    ``primary_gnss_id``.  Internal annotation keys (``_gnss_point``, ``_ts``)
    are stripped so the output matches the original export schema.
    """
    id_to_primary: dict[int, int | None] = {}
    for cluster in clusters:
        pid = cluster.primary_gnss_id
        for gid in cluster.gnss_ids:
            if gid is not None:
                id_to_primary[gid] = pid

    out = []
    for s in surveys:
        s2 = {k: v for k, v in s.items() if not k.startswith("_")}
        gid = s2.get("gnss_snapshot_id")
        if gid is not None and gid in id_to_primary:
            s2["gnss_snapshot_id"] = id_to_primary[gid]
        out.append(s2)
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_route_csv(df: pd.DataFrame, path: str) -> None:
    """Writes the collapsed route DataFrame to CSV (timestamp as ISO string)."""
    out = df.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = out["timestamp"].apply(
            lambda x: x.isoformat() if pd.notna(x) and not isinstance(x, str) else x
        )
    out.to_csv(path, index=False)


def write_survey_json(surveys: list[dict], path: str) -> None:
    """Writes the relinked survey list to JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(surveys, f, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Collapse survey-stop GNSS points into single representative "
            "points and relink survey entries to the collapsed id."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--surveys", required=True,
                   help="Path to the exported survey JSON file.")
    p.add_argument("--sensors", required=True,
                   help="Path to the exported sensor CSV file.")
    p.add_argument("--out-csv", required=True,
                   help="Path for the collapsed route CSV output.")
    p.add_argument("--out-json", required=True,
                   help="Path for the relinked survey JSON output.")
    p.add_argument("--spatial-threshold", type=float, default=20.0,
                   help="Max distance (metres) between two surveys to be "
                        "considered the same stop.")
    p.add_argument("--time-window", type=float, default=300.0,
                   help="Max time gap (seconds) between consecutive surveys "
                        "in the same stop.")
    p.add_argument("--time-buffer", type=float, default=120.0,
                   help="Seconds of slack added before/after a stop's survey "
                        "timestamps when matching route points to the stop.")
    p.add_argument("--min-spacing", type=float, default=5.0,
                   help="Minimum metres between consecutive non-stop route "
                        "points in the output CSV.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    for label, path in (("surveys", args.surveys), ("sensors", args.sensors)):
        if not os.path.isfile(path):
            print(f"[collapse] Error: {label} file not found: {path}",
                  file=sys.stderr)
            return 2

    print(f"[collapse] Loading surveys from {args.surveys}")
    surveys = load_surveys(args.surveys)
    print(f"[collapse]   {len(surveys)} survey entries loaded")

    print(f"[collapse] Loading sensors from {args.sensors}")
    sensors = load_sensors(args.sensors)
    print(f"[collapse]   {len(sensors)} sensor rows loaded")

    gnss_lookup = build_gnss_lookup(sensors)
    print(f"[collapse]   {len(gnss_lookup)} GNSS rows indexed by id")

    # Warn about surveys whose gnss_snapshot_id is missing from the CSV.
    missing = [s.get("gnss_snapshot_id") for s in surveys
               if s.get("gnss_snapshot_id") is not None
               and s["gnss_snapshot_id"] not in gnss_lookup]
    if missing:
        print(f"[collapse] Warning: {len(missing)} survey gnss_snapshot_id(s) "
              f"not found in sensor CSV: {missing[:5]}{'...' if len(missing) > 5 else ''}",
              file=sys.stderr)

    print("[collapse] Clustering survey stops ...")
    clusters = cluster_surveys(
        surveys, gnss_lookup,
        spatial_threshold=args.spatial_threshold,
        time_window=args.time_window,
    )
    print(f"[collapse]   {len(clusters)} survey stop(s) detected")
    for i, c in enumerate(clusters):
        n = len(c.surveys)
        pid = c.primary_gnss_id
        lat = c.centroid_lat
        lon = c.centroid_lon
        print(f"[collapse]   stop #{i}: {n} survey(s), "
              f"primary gnss_id={pid}, "
              f"centroid=({lat:.6f}, {lon:.6f})")

    print("[collapse] Collapsing route points ...")
    route = collapse_route(
        sensors, clusters,
        spatial_threshold=args.spatial_threshold,
        time_buffer=args.time_buffer,
        min_spacing=args.min_spacing,
    )
    print(f"[collapse]   {len(route)} rows in collapsed route "
          f"(was {len(sensors)})")

    print(f"[collapse] Writing route CSV -> {args.out_csv}")
    write_route_csv(route, args.out_csv)

    print("[collapse] Relinking survey gnss_snapshot_id values ...")
    relinked = relink_surveys(surveys, clusters)
    print(f"[collapse] Writing survey JSON -> {args.out_json}")
    write_survey_json(relinked, args.out_json)

    print("[collapse] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
