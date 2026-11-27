#!/usr/bin/env python3
"""Build a lightweight offline raster MBTiles archive for PACE SENSE.

Uses only the Python standard library so it can run directly on the SENSE
Raspberry Pi or a laptop without extra dependencies.

Example:
    python3 scripts/build_mbtiles.py --output SENSE/data/maps/offline.mbtiles
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Default bounding box: Mainz (city center + Hochschule Mainz campus)
DEFAULT_BBOX = (49.955, 8.185, 50.030, 8.315)  # south, west, north, east
DEFAULT_TILE_URL = "https://tile.openstreetmap.de/{z}/{x}/{y}.png"
USER_AGENT = "PACE-Routing-Academic-Research/1.0 (https://pace-routing.com)"


def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Convert WGS84 lat/lon to Web Mercator slippy-map tile (x, y)."""
    lat_rad = math.radians(lat_deg)
    n = 1 << zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, xtile)), max(0, min(n - 1, ytile))


def init_mbtiles(conn: sqlite3.Connection, name: str, bbox: tuple[float, float, float, float],
                 min_zoom: int, max_zoom: int) -> None:
    """Create MBTiles 1.3 schema and populate metadata."""
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tiles ("
        "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
        "ON tiles (zoom_level, tile_column, tile_row)"
    )
    south, west, north, east = bbox
    center_lat = (south + north) / 2.0
    center_lon = (west + east) / 2.0
    default_zoom = min(max_zoom, max(min_zoom, 14))
    metadata = {
        "name": name,
        "format": "png",
        "type": "baselayer",
        "version": "1.3",
        "bounds": f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}",
        "center": f"{center_lon:.6f},{center_lat:.6f},{default_zoom}",
        "minzoom": str(min_zoom),
        "maxzoom": str(max_zoom),
        "attribution": "© OpenStreetMap contributors",
    }
    conn.execute("DELETE FROM metadata")
    conn.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", metadata.items())
    conn.commit()


def iter_tiles(bbox: tuple[float, float, float, float], min_zoom: int, max_zoom: int):
    south, west, north, east = bbox
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = deg2num(north, west, z)
        x1, y1 = deg2num(south, east, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield z, x, y


def fetch_tile(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def build_archive(output: Path, bbox: tuple[float, float, float, float],
                  min_zoom: int, max_zoom: int, tile_url: str,
                  name: str, delay: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tiles = list(iter_tiles(bbox, min_zoom, max_zoom))
    total = len(tiles)
    print(f"Building {output} ({name}): {total} tiles (z={min_zoom}..{max_zoom})")

    conn = sqlite3.connect(str(output))
    try:
        init_mbtiles(conn, name, bbox, min_zoom, max_zoom)
        existing = set(
            conn.execute("SELECT zoom_level, tile_column, tile_row FROM tiles").fetchall()
        )
        downloaded = 0
        skipped = 0
        failed = 0
        for idx, (z, x, y) in enumerate(tiles, start=1):
            tms_y = (1 << z) - 1 - y
            if (z, x, tms_y) in existing:
                skipped += 1
                continue
            url = tile_url.format(z=z, x=x, y=y)
            try:
                blob = fetch_tile(url)
                conn.execute(
                    "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                    "VALUES (?, ?, ?, ?)",
                    (z, x, tms_y, sqlite3.Binary(blob)),
                )
                downloaded += 1
                if downloaded % 25 == 0:
                    conn.commit()
                    print(f"  [{idx}/{total}] downloaded={downloaded} skipped={skipped} failed={failed}", flush=True)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failed += 1
                print(f"  WARN z={z} x={x} y={y}: {exc}", file=sys.stderr)
            if delay > 0:
                time.sleep(delay)
        conn.commit()
        distinct_count, total_stored = conn.execute(
            "SELECT COUNT(DISTINCT tile_data), COUNT(*) FROM tiles"
        ).fetchone()
    finally:
        conn.close()

    size_mb = output.stat().st_size / (1024 * 1024)
    print(
        f"Done: {output} ({size_mb:.2f} MiB) — downloaded={downloaded}, skipped={skipped}, "
        f"failed={failed}, distinct={distinct_count}/{total_stored}"
    )
    if total_stored > 5 and distinct_count <= 1:
        raise RuntimeError(
            "All downloaded tiles are identical — the tile provider likely returned a placeholder or API-key error image."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a lightweight offline MBTiles map for PACE SENSE.")
    parser.add_argument("--output", type=Path, default=Path("SENSE/data/maps/offline.mbtiles"))
    parser.add_argument("--name", default="Mainz Offline Map")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        default=list(DEFAULT_BBOX),
        help="WGS84 bounding box (default: Mainz 49.955 8.185 50.030 8.315)",
    )
    parser.add_argument("--min-zoom", type=int, default=12)
    parser.add_argument("--max-zoom", type=int, default=16)
    parser.add_argument("--tile-url", default=DEFAULT_TILE_URL)
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between tile requests in seconds")
    args = parser.parse_args()

    build_archive(
        output=args.output,
        bbox=tuple(args.bbox),
        min_zoom=args.min_zoom,
        max_zoom=args.max_zoom,
        tile_url=args.tile_url,
        name=args.name,
        delay=args.delay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
