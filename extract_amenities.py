#!/usr/bin/env python3
"""
Extract surrounding amenities for each survey stop location.

Takes as input the two files produced by the PACE export endpoints (after the
collapse/relink step):

  * the collapsed route CSV  (``pace_sensor_export_*.csv``)
  * the relinked survey JSON (``pace_survey_export_*.json``)

For every *unique* survey stop location (identified by the shared
``gnss_snapshot_id`` that all participants at a stop are relinked to) the
script queries OpenStreetMap via the Overpass API and records:

  * ``unique_amenity_types`` — number of distinct amenity types (e.g.
    ``cafe``, ``bench``, ``restaurant``, ``parking``) found within a 100 m
    radius.  Repeated types count once, so ``['cafe', 'bench', 'cafe']`` -> 2.
  * ``unique_shop_types`` — number of distinct shop types (e.g. ``cafe``,
    ``groceries``, ``bakery``) found within a 100 m radius.  Repeated types
    count once, so ``['cafe', 'groceries', 'cafe', 'bakery']`` -> 3.
  * ``closest_pt_distance_m`` — distance (metres) to the nearest public
    transport station (railway station / tram / bus / subway / ferry / etc.).
  * ``unique_pavement_types`` — number of distinct surface/pavement types on
    ways (footways, pedestrian areas, paths, etc.) within a 50 m radius.

The output is a CSV named after the input measurements CSV with the suffix
``_amenities`` appended before the extension, e.g.

    pace_sensor_export_20260813_120000.csv
    -> pace_sensor_export_20260813_120000_amenities.csv

Each row describes one survey stop location and contains the coordinates plus
the extracted parameters.

Usage
-----
    python extract_amenities.py \
        --sensors  pace_sensor_export_20260813_120000.csv \
        --surveys  pace_survey_export_20260813_120000.json

By default the public Overpass endpoint is used.  Override with
``--overpass-url`` if you run your own instance.  A retry/backoff handles the
common 429 / 504 responses from the public endpoint.
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

import pandas as pd


# --------------------------------------------------------------------------- #
# Geometry
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
def load_sensors(path: str) -> pd.DataFrame:
    """Loads the collapsed route CSV and normalises types.

    Only the columns needed for amenity extraction (``id``, ``latitude``,
    ``longitude``) are retained, so the script is unaffected by extra columns
    appended by downstream enrichers such as ``enrich_weather.py`` (e.g.
    ``temperature_2m``, ``relative_humidity_2m``, ``surface_pressure``) or by
    changes to the column ordering.
    """
    df = pd.read_csv(path)
    needed = [c for c in ("id", "latitude", "longitude") if c in df.columns]
    df = df[needed].copy() if needed else df.copy()
    if "id" in df.columns:
        df["id"] = pd.to_numeric(df["id"], errors="coerce").astype("Int64")
    for col in ("latitude", "longitude"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_surveys(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_stop_locations(sensors: pd.DataFrame, surveys: list[dict]):
    """Returns a list of (gnss_id, lat, lon) for every unique survey stop.

    Surveys are relinked so all participants at a stop share one
    ``gnss_snapshot_id``.  We collect the distinct ids, then look up their
    coordinates in the route CSV (the collapsed representative row).
    """
    seen_ids = []
    seen = set()
    for s in surveys:
        gid = s.get("gnss_snapshot_id")
        if gid is None or gid in seen:
            continue
        seen.add(gid)
        seen_ids.append(gid)

    coord_by_id = {}
    for row in sensors.itertuples(index=False):
        rid_val = getattr(row, "id", None)
        rid = int(rid_val) if pd.notna(rid_val) else None
        if rid is None:
            continue
        lat_val = getattr(row, "latitude", None)
        lon_val = getattr(row, "longitude", None)
        if lat_val is None or lon_val is None or pd.isna(lat_val) or pd.isna(lon_val):
            continue
        coord_by_id[rid] = (float(lat_val), float(lon_val))

    stops = []
    missing = []
    for gid in seen_ids:
        coord = coord_by_id.get(gid)
        if coord is None:
            missing.append(gid)
            continue
        stops.append((gid, coord[0], coord[1]))
    if missing:
        print(f"[amenities] Warning: {len(missing)} survey gnss_snapshot_id(s) "
              f"not found in CSV: {missing[:5]}{'...' if len(missing) > 5 else ''}",
              file=sys.stderr)
    return stops


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #
# Public transport station tags whose ``public_transport=station`` or
# ``railway=`` / ``highway=bus_stop`` / ``amenity=bus_station`` values identify
# a stop.  We query a generous radius and then compute the closest distance.
_PT_OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node(around:{radius},{lat},{lon})["public_transport"="station"];
  way(around:{radius},{lat},{lon})["public_transport"="station"];
  node(around:{radius},{lat},{lon})["railway"~"station|halt|tram_stop|subway_entrance|bus_stop"];
  node(around:{radius},{lat},{lon})["amenity"="bus_station"];
  node(around:{radius},{lat},{lon})["highway"="bus_stop"];
);
out center;
"""

# Amenities: anything tagged with amenity=* (cafes, benches, restaurants,
# parking, etc.).  This is the primary amenity count requested by the project.
_AMENITY_OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node(around:{radius},{lat},{lon})["amenity"];
  way(around:{radius},{lat},{lon})["amenity"];
);
out center tags;
"""

# Shops: anything tagged with shop=* (excludes amenities like cafes that use
# amenity=cafe, but OSM convention is that retail shops use shop=*).  We also
# include a few amenity-based retail types for completeness.
_SHOP_OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node(around:{radius},{lat},{lon})["shop"];
  way(around:{radius},{lat},{lon})["shop"];
);
out center tags;
"""

# Pavement/surface types: ways that pedestrians use, carrying a surface tag.
_PAVEMENT_OVERPASS_QUERY = """
[out:json][timeout:60];
(
  way(around:{radius},{lat},{lon})["highway"~"footway|pedestrian|path|cycleway|living_street|residential"]["surface"];
  way(around:{radius},{lat},{lon})["footway"]["surface"];
  way(around:{radius},{lat},{lon})["pedestrian"]["surface"];
);
out tags;
"""


def _overpass_request(query: str, url: str, max_retries: int = 5) -> dict:
    """POSTs a QL query to Overpass and returns the parsed JSON.

    Retries with exponential backoff on 429 / 504 / network errors.
    """
    data = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "PACE-amenities/1.0"}
    )
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 504):
                wait = 2 ** attempt
                print(f"[amenities]   Overpass {e.code}, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[amenities]   network error ({e}), retrying in {wait}s "
                  f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Overpass request failed after {max_retries} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# Feature extraction
# --------------------------------------------------------------------------- #
def count_unique_amenity_types(lat: float, lon: float, radius: float, url: str) -> int:
    """Number of distinct ``amenity=*`` values within ``radius`` metres."""
    query = _AMENITY_OVERPASS_QUERY.format(lat=lat, lon=lon, radius=int(radius))
    data = _overpass_request(query, url)
    types = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        amenity = tags.get("amenity")
        if amenity:
            types.add(amenity)
    return len(types)


def count_unique_shop_types(lat: float, lon: float, radius: float, url: str) -> int:
    """Number of distinct ``shop=*`` values within ``radius`` metres."""
    query = _SHOP_OVERPASS_QUERY.format(lat=lat, lon=lon, radius=int(radius))
    data = _overpass_request(query, url)
    types = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        shop = tags.get("shop")
        if shop:
            types.add(shop)
    return len(types)


def closest_pt_distance(lat: float, lon: float, search_radius: float, url: str) -> float:
    """Distance (m) to the nearest public transport station.

    Queries a generous ``search_radius`` (default 1000 m) and computes the
    haversine distance to each returned element's coordinates.  Returns
    ``NaN`` if nothing is found within the radius.
    """
    query = _PT_OVERPASS_QUERY.format(lat=lat, lon=lon, radius=int(search_radius))
    data = _overpass_request(query, url)
    best = math.nan
    for el in data.get("elements", []):
        if "lat" in el and "lon" in el:
            elat, elon = el["lat"], el["lon"]
        elif "center" in el:
            elat, elon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        d = haversine(lat, lon, elat, elon)
        if math.isnan(best) or d < best:
            best = d
    return best


def count_unique_pavement_types(lat: float, lon: float, radius: float, url: str) -> int:
    """Number of distinct ``surface=*`` values on pedestrian ways within radius."""
    query = _PAVEMENT_OVERPASS_QUERY.format(lat=lat, lon=lon, radius=int(radius))
    data = _overpass_request(query, url)
    types = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        surface = tags.get("surface")
        if surface:
            types.add(surface)
    return len(types)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def amenities_output_path(sensors_path: str) -> str:
    """Appends ``_amenities`` before the CSV extension of the sensors file."""
    root, ext = os.path.splitext(sensors_path)
    return f"{root}_amenities{ext or '.csv'}"


def write_amenities_csv(rows: list[dict], path: str) -> None:
    if not rows:
        # Still write a header-only file so downstream tools see the schema.
        fieldnames = ["gnss_snapshot_id", "latitude", "longitude",
                      "unique_amenity_types", "unique_shop_types",
                      "closest_pt_distance_m", "unique_pavement_types"]
    else:
        fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract surrounding shop / transport / pavement amenities "
                    "for each survey stop location via OpenStreetMap Overpass.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sensors", required=True,
                   help="Path to the collapsed route CSV export.")
    p.add_argument("--surveys", required=True,
                   help="Path to the relinked survey JSON export.")
    p.add_argument("--out", default=None,
                   help="Output CSV path. Defaults to <sensors>_amenities.csv.")
    p.add_argument("--amenity-radius", type=float, default=100.0,
                   help="Radius (metres) for amenity detection.")
    p.add_argument("--shop-radius", type=float, default=100.0,
                   help="Radius (metres) for shop detection.")
    p.add_argument("--pt-radius", type=float, default=1000.0,
                   help="Search radius (metres) for the closest public transport "
                        "station.")
    p.add_argument("--pavement-radius", type=float, default=50.0,
                   help="Radius (metres) for pavement/surface detection.")
    p.add_argument("--overpass-url",
                   default="https://overpass-api.de/api/interpreter",
                   help="Overpass API endpoint URL.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds to wait between stops (be nice to the public API).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    for label, path in (("sensors", args.sensors), ("surveys", args.surveys)):
        if not os.path.isfile(path):
            print(f"[amenities] Error: {label} file not found: {path}",
                  file=sys.stderr)
            return 2

    print(f"[amenities] Loading sensors from {args.sensors}")
    sensors = load_sensors(args.sensors)
    print(f"[amenities]   {len(sensors)} route rows loaded")

    print(f"[amenities] Loading surveys from {args.surveys}")
    surveys = load_surveys(args.surveys)
    print(f"[amenities]   {len(surveys)} survey entries loaded")

    stops = build_stop_locations(sensors, surveys)
    print(f"[amenities]   {len(stops)} unique survey stop location(s) detected")

    out_path = args.out or amenities_output_path(args.sensors)
    print(f"[amenities] Output -> {out_path}")

    rows = []
    for i, (gid, lat, lon) in enumerate(stops, 1):
        print(f"[amenities] Stop {i}/{len(stops)}: gnss_id={gid} "
              f"({lat:.6f}, {lon:.6f})")

        try:
            amenities = count_unique_amenity_types(lat, lon, args.amenity_radius,
                                                    args.overpass_url)
        except Exception as e:
            print(f"[amenities]   amenity query failed: {e}", file=sys.stderr)
            amenities = None

        try:
            shops = count_unique_shop_types(lat, lon, args.shop_radius,
                                            args.overpass_url)
        except Exception as e:
            print(f"[amenities]   shop query failed: {e}", file=sys.stderr)
            shops = None

        try:
            pt_dist = closest_pt_distance(lat, lon, args.pt_radius,
                                           args.overpass_url)
        except Exception as e:
            print(f"[amenities]   public transport query failed: {e}",
                  file=sys.stderr)
            pt_dist = None

        try:
            pavement = count_unique_pavement_types(lat, lon, args.pavement_radius,
                                                   args.overpass_url)
        except Exception as e:
            print(f"[amenities]   pavement query failed: {e}", file=sys.stderr)
            pavement = None

        rows.append({
            "gnss_snapshot_id": gid,
            "latitude": lat,
            "longitude": lon,
            "unique_amenity_types": amenities,
            "unique_shop_types": shops,
            "closest_pt_distance_m": pt_dist,
            "unique_pavement_types": pavement,
        })
        print(f"[amenities]   amenities={amenities}  shops={shops}  "
              f"pt_dist={pt_dist}  pavement={pavement}")

        if i < len(stops) and args.delay > 0:
            time.sleep(args.delay)

    write_amenities_csv(rows, out_path)
    print(f"[amenities] Wrote {len(rows)} row(s) to {out_path}")
    print("[amenities] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
