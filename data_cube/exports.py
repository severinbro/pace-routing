"""
Export helpers for the sensor and survey data.

Joins the 7 sensor tables on their shared "id" primary key (they are all
written together per sensor reading, see save_sensor_data.py) and uses the
GNSS latitude/longitude columns as point geometry, producing a GeoPackage
(.gpkg) that can be opened directly in QGIS/ArcGIS.

Also provides a JSON export of all EnvironmentSurvey responses.
"""
import tempfile
import os
import json
from datetime import datetime

import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

from .models import (
    GNSSPhoneMeasurement,
    GNSSSensorMeasurement,
    AtmosphericMeasurement,
    AccelerometerMeasurement,
    AirQualityMeasurement,
    ParticulateMeasurement,
    NoiseMeasurement,
    EnvironmentSurvey,
)


def build_sensor_geodataframe():
    """Builds a GeoDataFrame joining all sensor tables on id.

    GNSSPhoneMeasurement is the base table (it provides the geometry), the other
    tables are left-joined onto it by id so every row keeps a valid point.
    """
    gnss_df = pd.DataFrame.from_records(
        GNSSPhoneMeasurement.objects.values(
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
        (GNSSSensorMeasurement, ['sensor_lat', 'sensor_lon', 'sensor_alt', 'sensor_sats']),
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

    # Latitude/Longitude come from GNSSPhoneMeasurement as Decimal -> cast to float.
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


def build_survey_json():
    """Builds a list of dicts, one per EnvironmentSurvey response.

    Each entry contains the survey id, timestamp, username, all 11 responses,
    and the id of the linked GNSS phone snapshot.
    """
    surveys = EnvironmentSurvey.objects.select_related('user').order_by('id')
    entries = []
    for s in surveys:
        entries.append({
            'survey_id': s.id,
            'timestamp': s.timestamp.isoformat() if s.timestamp else None,
            'username': s.user.username if s.user else None,
            'responses': {
                'q1': s.q1, 'q2': s.q2, 'q3': s.q3, 'q4': s.q4,
                'q5': s.q5, 'q6': s.q6, 'q7': s.q7,
                'q8': s.q8, 'q9': s.q9, 'q10': s.q10, 'q11': s.q11,
            },
            'gnss_snapshot_id': s.gnss_snapshot_id,
        })
    return entries


def write_survey_json():
    """Writes the survey data to a temporary .json file.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    entries = build_survey_json()
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(entries, f, indent=2)
    return path


def survey_export_filename():
    return f"pace_survey_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
