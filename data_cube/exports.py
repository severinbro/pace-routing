"""
Export helpers for the sensor data.

Joins the 6 sensor tables on their shared "id" primary key (they are all
written together per sensor reading, see save_sensor_data.py) and uses the
GNSS latitude/longitude columns as point geometry, producing a GeoPackage
(.gpkg) that can be opened directly in QGIS/ArcGIS.
"""
import tempfile
import os
from datetime import datetime

import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

from .models import (
    GNSSMeasurement,
    AtmosphericMeasurement,
    AccelerometerMeasurement,
    AirQualityMeasurement,
    ParticulateMeasurement,
    NoiseMeasurement,
)


def build_sensor_geodataframe():
    """Builds a GeoDataFrame joining all 6 sensor tables on id.

    GNSSMeasurement is the base table (it provides the geometry), the other
    5 tables are left-joined onto it by id so every row keeps a valid point.
    """
    gnss_df = pd.DataFrame.from_records(
        GNSSMeasurement.objects.values(
            'id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites'
        )
    )

    if gnss_df.empty:
        # Still return a well-formed, empty GeoDataFrame with the right schema.
        return gpd.GeoDataFrame(
            columns=['id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites'],
            geometry=[],
            crs='EPSG:4326',
        )

    joins = [
        (AtmosphericMeasurement, ['temperature', 'humidity', 'pressure']),
        (AccelerometerMeasurement, ['accX', 'accY', 'accZ', 'angleX', 'angleY', 'angleZ']),
        (AirQualityMeasurement, ['aqi', 'tvoc', 'eco2']),
        (ParticulateMeasurement, ['pm1', 'pm25', 'pm10']),
        (NoiseMeasurement, ['noise_db']),
    ]

    merged = gnss_df
    for model, fields in joins:
        df = pd.DataFrame.from_records(model.objects.values('id', *fields))
        if df.empty:
            for field in fields:
                merged[field] = None
            continue
        # Avoid duplicate/clashing "timestamp" columns from each table.
        merged = merged.merge(df, on='id', how='left', suffixes=('', f'_{model.__name__}'))

    # Latitude/Longitude come from GNSSMeasurement as Decimal -> cast to float.
    merged['latitude'] = merged['latitude'].astype(float)
    merged['longitude'] = merged['longitude'].astype(float)

    geometry = [Point(lon, lat) for lon, lat in zip(merged['longitude'], merged['latitude'])]
    gdf = gpd.GeoDataFrame(merged, geometry=geometry, crs='EPSG:4326')
    return gdf


def write_sensor_gpkg():
    """Writes the joined sensor GeoDataFrame to a temporary .gpkg file.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    gdf = build_sensor_geodataframe()

    # Datetimes aren't well supported by the GPKG driver in all combos;
    # convert to ISO strings to keep the export robust.
    if 'timestamp' in gdf.columns:
        gdf['timestamp'] = gdf['timestamp'].astype(str)

    fd, path = tempfile.mkstemp(suffix='.gpkg')
    os.close(fd)
    # mkstemp creates the file; GDAL wants to create it itself.
    os.remove(path)

    gdf.to_file(path, driver='GPKG', layer='sensor_measurements')
    return path


def export_filename():
    return f"pace_sensor_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gpkg"
