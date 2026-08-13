"""
Export helpers for the sensor and survey data.

Joins the 7 sensor tables on their shared "id" primary key (they are all
written together per sensor reading, see save_sensor_data.py) and exports
the joined result as a CSV file.

Also provides a JSON export of all EnvironmentSurvey responses.
"""
import tempfile
import os
import json
from datetime import datetime, time

import pandas as pd

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


def _parse_date_range(start=None, end=None):
    """Parses optional ISO date strings (YYYY-MM-DD) into datetime bounds.

    Returns a (start_dt, end_dt) tuple where either element may be None.
    The end bound is moved to the end of that day (23:59:59) so the range is
    inclusive of the whole selected day.
    """
    start_dt = None
    end_dt = None
    if start:
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d')
        except ValueError:
            start_dt = None
    if end:
        try:
            end_dt = datetime.combine(
                datetime.strptime(end, '%Y-%m-%d').date(),
                time(23, 59, 59),
            )
        except ValueError:
            end_dt = None
    return start_dt, end_dt


def build_sensor_dataframe(start=None, end=None):
    """Builds a DataFrame joining all sensor tables on id.

    GNSSPhoneMeasurement is the base table, the other tables are left-joined
    onto it by id so every row is kept.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to records whose timestamp falls within the (inclusive) date range.
    """
    start_dt, end_dt = _parse_date_range(start, end)

    gnss_qs = GNSSPhoneMeasurement.objects.all()
    if start_dt:
        gnss_qs = gnss_qs.filter(timestamp__gte=start_dt)
    if end_dt:
        gnss_qs = gnss_qs.filter(timestamp__lte=end_dt)

    gnss_df = pd.DataFrame.from_records(
        gnss_qs.values(
            'id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites'
        )
    )

    if gnss_df.empty:
        # Still return a well-formed, empty DataFrame with the right schema.
        return pd.DataFrame(
            columns=['id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites']
        )

    # GNSSSensorMeasurement shares column names (latitude, longitude, altitude,
    # satellites, timestamp) with GNSSPhoneMeasurement, so select them with their
    # real field names and rename in pandas with a "sensor_" prefix to avoid
    # collisions after the merge.
    # When a date range is applied, restrict the joined tables to the same
    # window so stale rows from outside the range don't leak in via the merge.
    range_filters = {}
    if start_dt:
        range_filters['timestamp__gte'] = start_dt
    if end_dt:
        range_filters['timestamp__lte'] = end_dt

    joins = [
        (GNSSSensorMeasurement,
         ['latitude', 'longitude', 'altitude', 'satellites'],
         ['sensor_lat', 'sensor_lon', 'sensor_alt', 'sensor_sats']),
        (AtmosphericMeasurement, ['temperature', 'humidity', 'pressure'], None),
        (AccelerometerMeasurement, ['accX', 'accY', 'accZ', 'angleX', 'angleY', 'angleZ'], None),
        (AirQualityMeasurement, ['aqi', 'tvoc', 'eco2'], None),
        (ParticulateMeasurement, ['pm1', 'pm25', 'pm10'], None),
        (NoiseMeasurement, ['noise_db'], None),
    ]

    merged = gnss_df
    for model, fields, rename_map in joins:
        qs = model.objects.all()
        if range_filters:
            qs = qs.filter(**range_filters)
        df = pd.DataFrame.from_records(qs.values('id', *fields))
        if df.empty:
            for field in (rename_map or fields):
                merged[field] = None
            continue
        # Rename columns (e.g. sensor GNSS fields) before merge to avoid clashes.
        if rename_map:
            rename = dict(zip(['id'] + fields, ['id'] + rename_map))
            df = df.rename(columns=rename)
            fields = rename_map
        # Avoid duplicate/clashing "timestamp" columns from each table.
        merged = merged.merge(df, on='id', how='left', suffixes=('', f'_{model.__name__}'))

    # Latitude/Longitude come from GNSSPhoneMeasurement as Decimal -> cast to float.
    merged['latitude'] = merged['latitude'].astype(float)
    merged['longitude'] = merged['longitude'].astype(float)

    return merged


def write_sensor_csv(start=None, end=None):
    """Writes the joined sensor DataFrame to a temporary .csv file.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to the given (inclusive) date range.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    df = build_sensor_dataframe(start=start, end=end)

    # Convert datetimes to ISO strings for a stable, readable CSV.
    if 'timestamp' in df.columns:
        df['timestamp'] = df['timestamp'].astype(str)

    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='') as f:
        df.to_csv(f, index=False)
    return path


def export_filename():
    return f"pace_sensor_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


def build_survey_json(start=None, end=None):
    """Builds a list of dicts, one per EnvironmentSurvey response.

    Each entry contains the survey id, timestamp, username, all 11 responses,
    and the id of the linked GNSS phone snapshot.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to the given (inclusive) date range.
    """
    start_dt, end_dt = _parse_date_range(start, end)
    surveys = EnvironmentSurvey.objects.select_related('user').order_by('id')
    if start_dt:
        surveys = surveys.filter(timestamp__gte=start_dt)
    if end_dt:
        surveys = surveys.filter(timestamp__lte=end_dt)
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


def write_survey_json(start=None, end=None):
    """Writes the survey data to a temporary .json file.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to the given (inclusive) date range.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    entries = build_survey_json(start=start, end=end)
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(entries, f, indent=2)
    return path


def survey_export_filename():
    return f"pace_survey_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
