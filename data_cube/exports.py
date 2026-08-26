"""
Export helpers for the sensor and survey data.

Joins the 7 sensor tables on their shared "id" primary key (they are all
written together per sensor reading, see save_sensor_data.py) and exports
the joined result as a CSV file.

Also provides a JSON export of all EnvironmentSurvey responses.

Both exports are "collapsed": at a survey stop several participants submit
surveys within a few minutes, each linking to a slightly different GNSS
snapshot.  The CSV export collapses all those overlapping stationary points
into a single representative row (and downsamples the rest of the route to
roughly one point every ``ROUTE_MIN_SPACING`` metres), while the JSON export
relinks every participant at a stop to the same ``gnss_snapshot_id`` so the
two files stay referentially consistent.
"""
import tempfile
import os
import json
import math
from datetime import datetime, time, timedelta
from statistics import mean, median

import numpy as np
import pandas as pd

from .models import (
    GNSSPhoneMeasurement,
    AtmosphericMeasurement,
    AccelerometerMeasurement,
    AirQualityMeasurement,
    ParticulateMeasurement,
    NoiseMeasurement,
    EnvironmentSurvey,
)


# --------------------------------------------------------------------------- #
# Survey-stop collapse configuration
# --------------------------------------------------------------------------- #
# At a real survey stop participants stand within a few metres of each other
# and submit surveys within a few minutes.  These thresholds define when two
# surveys belong to the same physical stop.  Clustering is sequential (sorted
# by timestamp), so a route that revisits the same coordinates much later
# correctly produces a separate stop rather than merging the two visits.
STOP_SPATIAL_THRESHOLD_M = 20.0   # max distance between two surveys in a stop
STOP_TIME_WINDOW_S = 300.0       # max gap (s) between consecutive surveys in a stop
STOP_TIME_BUFFER_S = 120.0       # slack (s) added before/after a stop when
                                  # matching route points to the stop
ROUTE_MIN_SPACING_M = 5.0       # min metres between non-stop route points

_EARTH_RADIUS_M = 6_371_000.0


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance between two WGS84 points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


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


# Columns whose values are continuous sensor measurements and therefore
# candidates for statistical outlier removal. GNSS coordinates, satellite
# counts and integer-coded survey responses are intentionally excluded
# (they are either bounded by validators or vary legitimately along a route).
_OUTLIER_COLUMNS = [
    'temperature', 'humidity', 'pressure',
    'accX', 'accY', 'accZ', 'angleX', 'angleY', 'angleZ',
    'aqi', 'tvoc', 'eco2',
    'pm1', 'pm25', 'pm10',
    'noise_db',
]


def filter_outliers(df, columns=None, factor=1.5):
    """Replaces statistically significant outliers using the IQR method.

    For each numeric column, values falling outside
    ``[Q1 - factor*IQR, Q3 + factor*IQR]`` are treated as outliers and
    replaced with the previous valid value (forward-fill). Any leading
    outliers (before the first valid value) are back-filled. This preserves
    every row — including its GNSS coordinates and other valid sensor
    readings — rather than discarding the whole snapshot.

    Parameters
    ----------
    df : pandas.DataFrame
        The joined sensor dataframe to filter (modified copy returned).
    columns : list[str] or None
        Columns to inspect. Defaults to :data:`_OUTLIER_COLUMNS`.
    factor : float
        IQR multiplier. 1.5 is the standard "mild outlier" threshold; 3.0
        would restrict filtering to extreme outliers only.

    Returns
    -------
    pandas.DataFrame
        A new DataFrame with outlier values replaced by forward-fill.
    """
    if df.empty:
        return df

    columns = columns if columns is not None else _OUTLIER_COLUMNS
    present = [c for c in columns if c in df.columns]

    if not present:
        return df.copy()

    result = df.copy()
    total_replaced = 0
    for col in present:
        series = pd.to_numeric(result[col], errors='coerce')
        # Skip columns that are entirely NaN (e.g. a sensor table had no data).
        if series.notna().sum() < 4:
            continue
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        # Guard against zero-variance columns (IQR == 0).
        if iqr == 0:
            continue
        lower = q1 - factor * iqr
        upper = q3 + factor * iqr

        outlier_mask = ~series.between(lower, upper) & series.notna()
        replaced = outlier_mask.sum()
        if replaced:
            # Blank out the outliers, then carry the last valid value forward.
            series = series.mask(outlier_mask)
            series = series.ffill().bfill()
            result[col] = series
            total_replaced += replaced
            print(f"[exports] Outlier filter: replaced {replaced} values in '{col}' "
                  f"(bounds [{lower:.2f}, {upper:.2f}]).")

    if total_replaced:
        print(f"[exports] Outlier filter replaced {total_replaced} values total "
              f"across {len(result)} rows.")
    return result


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

    # Only non-GNSS sensor tables are joined onto the phone GNSS base table.
    # The GNSS sensor (SAM-M10Q) measurements are excluded from the CSV export.
    # When a date range is applied, restrict the joined tables to the same
    # window so stale rows from outside the range don't leak in via the merge.
    range_filters = {}
    if start_dt:
        range_filters['timestamp__gte'] = start_dt
    if end_dt:
        range_filters['timestamp__lte'] = end_dt

    joins = [
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

    # Replace statistically significant outliers in the continuous sensor
    # measurements with the previous valid value (forward-fill) before the
    # dataframe is handed to the CSV exporter (or any other consumer). GNSS
    # coordinates and bounded integer fields are left untouched (see
    # filter_outliers docstring).
    merged = filter_outliers(merged)

    return merged


# --------------------------------------------------------------------------- #
# Survey-stop clustering & route collapsing
# --------------------------------------------------------------------------- #
class _SurveyCluster:
    """A group of surveys submitted at one physical survey stop."""

    def __init__(self, first):
        self.surveys = [first]
        self.gnss_ids = [first['gnss_snapshot_id']]
        self._lats = []
        self._lons = []
        self._timestamps = []
        self._add_coords(first)

    def _add_coords(self, survey):
        pt = survey.get('_gnss_point')
        if pt is not None:
            self._lats.append(pt['latitude'])
            self._lons.append(pt['longitude'])
        ts = survey.get('_ts')
        if ts is not None:
            self._timestamps.append(ts)

    def add(self, survey):
        self.surveys.append(survey)
        if survey.get('gnss_snapshot_id') is not None:
            self.gnss_ids.append(survey['gnss_snapshot_id'])
        self._add_coords(survey)

    @property
    def centroid_lat(self):
        return mean(self._lats) if self._lats else float('nan')

    @property
    def centroid_lon(self):
        return mean(self._lons) if self._lons else float('nan')

    @property
    def start_ts(self):
        return min(self._timestamps) if self._timestamps else None

    @property
    def end_ts(self):
        return max(self._timestamps) if self._timestamps else None

    @property
    def primary_gnss_id(self):
        """The gnss_snapshot_id whose timestamp is closest to the cluster median.

        This becomes the single id shared by every participant at the stop and
        the id of the collapsed representative row in the route CSV.
        """
        if not self._timestamps or not self.gnss_ids:
            return self.gnss_ids[0] if self.gnss_ids else None
        target = median(self._timestamps)
        best_id, best_delta = None, None
        for s in self.surveys:
            gid = s.get('gnss_snapshot_id')
            ts = s.get('_ts')
            if gid is None or ts is None:
                continue
            delta = abs((ts - target).total_seconds())
            if best_delta is None or delta < best_delta:
                best_id, best_delta = gid, delta
        return best_id if best_id is not None else (self.gnss_ids[0] if self.gnss_ids else None)


def _build_gnss_lookup(sensors_df):
    """Maps ``id -> {id, latitude, longitude, timestamp}`` for every route row."""
    lookup = {}
    for row in sensors_df.itertuples(index=False):
        rid = int(row.id) if pd.notna(getattr(row, 'id', None)) else None
        if rid is None:
            continue
        lookup[rid] = {
            'id': rid,
            'latitude': float(row.latitude),
            'longitude': float(row.longitude),
            'timestamp': row.timestamp,
        }
    return lookup


def _cluster_surveys(surveys, gnss_lookup,
                     spatial_threshold=STOP_SPATIAL_THRESHOLD_M,
                     time_window=STOP_TIME_WINDOW_S):
    """Groups surveys into physical stops using spatial + temporal thresholds.

    Surveys are sorted by timestamp.  A survey joins the current cluster when
    its GNSS coordinates are within ``spatial_threshold`` metres of the cluster
    centroid *and* its timestamp is within ``time_window`` seconds of the
    cluster's most recent survey.  Otherwise it starts a new cluster, so a
    route that revisits the same coordinates much later produces a separate
    stop.
    """
    annotated = []
    for s in surveys:
        gid = s.get('gnss_snapshot_id')
        pt = gnss_lookup.get(gid) if gid is not None else None
        ts_raw = s.get('timestamp')
        ts = None
        if ts_raw:
            try:
                ts = pd.to_datetime(ts_raw).to_pydatetime()
            except Exception:
                ts = None
        s2 = dict(s)
        s2['_gnss_point'] = pt
        s2['_ts'] = ts
        annotated.append(s2)

    annotated.sort(key=lambda s: (s['_ts'] is None, s['_ts']))

    clusters = []
    for s in annotated:
        pt = s['_gnss_point']
        ts = s['_ts']
        placed = False
        if pt is not None and ts is not None and clusters:
            cur = clusters[-1]
            if cur.end_ts is not None:
                dt = abs((ts - cur.end_ts).total_seconds())
                if dt <= time_window:
                    d = haversine(pt['latitude'], pt['longitude'],
                                  cur.centroid_lat, cur.centroid_lon)
                    if d <= spatial_threshold:
                        cur.add(s)
                        placed = True
        if not placed:
            clusters.append(_SurveyCluster(s))
    return clusters


def _collapse_rows(rows, primary_id):
    """Reduces a group of route rows to one representative row.

    Numeric columns are averaged; the id and timestamp come from the primary
    row; non-numeric columns take the primary row's value.
    """
    primary = rows.loc[rows['id'] == primary_id]
    if primary.empty:
        primary = rows.iloc[[0]]
    primary = primary.iloc[0]

    out = primary.copy()
    for col in rows.columns:
        if col in ('id', 'timestamp'):
            continue
        if pd.api.types.is_numeric_dtype(rows[col]):
            vals = pd.to_numeric(rows[col], errors='coerce').dropna()
            out[col] = float(vals.mean()) if not vals.empty else primary[col]
        else:
            out[col] = primary[col]
    return out


def _collapse_route(sensors_df, clusters,
                    spatial_threshold=STOP_SPATIAL_THRESHOLD_M,
                    time_buffer=STOP_TIME_BUFFER_S,
                    min_spacing=ROUTE_MIN_SPACING_M):
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
    if sensors_df.empty:
        return sensors_df.copy()

    df = sensors_df.sort_values('timestamp').reset_index(drop=True).copy()
    df['_is_stop'] = False
    df['_cluster_id'] = -1

    # 1. Mark stop points.
    for idx, cluster in enumerate(clusters):
        pid = cluster.primary_gnss_id
        if pid is None or math.isnan(cluster.centroid_lat):
            continue
        clat, clon = cluster.centroid_lat, cluster.centroid_lon
        t0 = cluster.start_ts - timedelta(seconds=time_buffer) if cluster.start_ts else None
        t1 = cluster.end_ts + timedelta(seconds=time_buffer) if cluster.end_ts else None

        for i, row in df.iterrows():
            if df.at[i, '_is_stop']:
                continue
            ts = row['timestamp']
            if t0 is not None and t1 is not None:
                if pd.isna(ts) or ts < t0 or ts > t1:
                    continue
            d = haversine(row['latitude'], row['longitude'], clat, clon)
            if d <= spatial_threshold:
                df.at[i, '_is_stop'] = True
                df.at[i, '_cluster_id'] = idx

    # 2. Collapse stop points.
    collapsed_rows = []
    for idx, cluster in enumerate(clusters):
        pid = cluster.primary_gnss_id
        if pid is None:
            continue
        stop_rows = df[df['_cluster_id'] == idx]
        if stop_rows.empty:
            # No route points matched (e.g. survey id not in CSV); fabricate a
            # row from the primary GNSS point so the CSV/JSON stay linked.
            pt = cluster.surveys[0].get('_gnss_point')
            if pt is None:
                continue
            row = {c: None for c in df.columns}
            row['id'] = pid
            row['latitude'] = pt['latitude']
            row['longitude'] = pt['longitude']
            row['timestamp'] = pt['timestamp']
            row['_is_stop'] = True
            row['_cluster_id'] = idx
            collapsed_rows.append(pd.Series(row))
        else:
            collapsed_rows.append(_collapse_rows(stop_rows, pid))

    # 3. Downsample non-stop points to min_spacing.
    non_stop = df[~df['_is_stop']].copy()
    kept = []
    last_lat = last_lon = None
    for _, row in non_stop.iterrows():
        if last_lat is None:
            kept.append(row)
            last_lat, last_lon = row['latitude'], row['longitude']
            continue
        d = haversine(last_lat, last_lon, row['latitude'], row['longitude'])
        if d >= min_spacing:
            kept.append(row)
            last_lat, last_lon = row['latitude'], row['longitude']
    non_stop_kept = pd.DataFrame(kept, columns=df.columns) if kept else pd.DataFrame(columns=df.columns)

    # 4. Combine & sort.
    parts = []
    if collapsed_rows:
        parts.append(pd.DataFrame(collapsed_rows, columns=df.columns))
    if not non_stop_kept.empty:
        parts.append(non_stop_kept)

    if not parts:
        return df.drop(columns=['_is_stop', '_cluster_id'])

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values('timestamp').reset_index(drop=True)
    return combined.drop(columns=['_is_stop', '_cluster_id'])


def _relink_surveys(surveys, clusters):
    """Returns a copy of the survey entries with unified ``gnss_snapshot_id``.

    Every participant at a given stop receives the cluster's
    ``primary_gnss_id``.  Internal annotation keys (``_gnss_point``, ``_ts``)
    are stripped so the output matches the original export schema.
    """
    id_to_primary = {}
    for cluster in clusters:
        pid = cluster.primary_gnss_id
        for gid in cluster.gnss_ids:
            if gid is not None:
                id_to_primary[gid] = pid

    out = []
    for s in surveys:
        s2 = {k: v for k, v in s.items() if not k.startswith('_')}
        gid = s2.get('gnss_snapshot_id')
        if gid is not None and gid in id_to_primary:
            s2['gnss_snapshot_id'] = id_to_primary[gid]
        out.append(s2)
    return out


def write_sensor_csv(start=None, end=None):
    """Writes the collapsed route DataFrame to a temporary .csv file.

    Survey stops are collapsed into a single representative point and the
    remaining route is downsampled to roughly one point every
    :data:`ROUTE_MIN_SPACING_M` metres, so the CSV contains continuous points
    (one per ~5 m) with matching sensor measurements instead of dense
    stationary blobs at each stop.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to the given (inclusive) date range.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    df = build_sensor_dataframe(start=start, end=end)
    surveys = build_survey_json(start=start, end=end)

    gnss_lookup = _build_gnss_lookup(df)
    clusters = _cluster_surveys(surveys, gnss_lookup)
    route = _collapse_route(df, clusters)

    # Convert datetimes to ISO strings for a stable, readable CSV.
    if 'timestamp' in route.columns:
        route['timestamp'] = route['timestamp'].apply(
            lambda x: x.isoformat() if pd.notna(x) and not isinstance(x, str) else x
        )

    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='') as f:
        route.to_csv(f, index=False)
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
            'campaign_stop': s.campaign_stop,
            'responses': {
                'noise': s.q1, 'air_quality': s.q2, 'air_temperature': s.q3,
                'aesthetics': s.q4, 'diversity': s.q5, 'urban_design': s.q6,
                'accessibility': s.q7, 'safety': s.q8, 'enjoyment': s.q9,
                'stress': s.q10, 'comfort': s.q11,
            },
            'gnss_snapshot_id': s.gnss_snapshot_id,
        })
    return entries


def write_survey_json(start=None, end=None):
    """Writes the relinked survey data to a temporary .json file.

    Every participant at a given survey stop is linked to the same
    ``gnss_snapshot_id`` (the id of the collapsed representative point in the
    CSV export), so the two files stay referentially consistent.  The survey
    answers themselves are unchanged.

    Optional ``start``/``end`` (ISO ``YYYY-MM-DD`` strings) restrict the export
    to the given (inclusive) date range.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    entries = build_survey_json(start=start, end=end)
    df = build_sensor_dataframe(start=start, end=end)

    gnss_lookup = _build_gnss_lookup(df)
    clusters = _cluster_surveys(entries, gnss_lookup)
    relinked = _relink_surveys(entries, clusters)

    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(relinked, f, indent=2)
    return path


def survey_export_filename():
    return f"pace_survey_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


# --------------------------------------------------------------------------- #
# Weather enrichment export (Open-Meteo)
# --------------------------------------------------------------------------- #
def write_weather_csv(start=None, end=None):
    """Writes a weather-enriched measurements CSV using Open-Meteo data.

    Builds the same collapsed route DataFrame as :func:`write_sensor_csv`,
    then enriches every row with Open-Meteo weather variables
    (``temperature_2m``, ``relative_humidity_2m``, ``surface_pressure``)
    matched to the row's GNSS coordinates and timestamp.

    Requires an internet connection to reach the Open-Meteo API.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    import enrich_weather as _ew

    # Produce the measurements CSV first, then enrich it in place.
    sensor_path = write_sensor_csv(start=start, end=end)
    try:
        df = _ew.load_sensors(sensor_path)
    except Exception:
        if os.path.exists(sensor_path):
            os.remove(sensor_path)
        raise

    valid = df.dropna(subset=['latitude', 'longitude', 'timestamp']).copy()
    if valid.empty:
        for v in _ew.WEATHER_VARS:
            df[v] = pd.NA
    else:
        prec = 2
        valid['_loc_key'] = (
            valid['latitude'].round(prec).astype(str)
            + ',' + valid['longitude'].round(prec).astype(str)
        )
        weather_cache = {}
        for loc_key, grp in valid.groupby('_loc_key', sort=False):
            lat = grp['latitude'].iloc[0]
            lon = grp['longitude'].iloc[0]
            grp_min = grp['timestamp'].min().floor('h')
            grp_max = grp['timestamp'].max().ceil('h')
            start_date = (grp_min - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (grp_max + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            try:
                wdf = _ew.fetch_weather(
                    lat, lon, start_date, end_date,
                    _ew.OPEN_METEO_FORECAST_URL, use_archive=False,
                )
            except Exception:
                wdf = pd.DataFrame(columns=['time'] + list(_ew.WEATHER_VARS))
            weather_cache[loc_key] = wdf

        enriched_parts = []
        for loc_key, grp in valid.groupby('_loc_key', sort=False):
            wdf = weather_cache.get(loc_key)
            if wdf is None or wdf.empty:
                grp = grp.copy()
                for v in _ew.WEATHER_VARS:
                    grp[v] = pd.NA
                enriched_parts.append(grp)
            else:
                enriched_parts.append(_ew.match_nearest(grp, wdf))

        enriched_valid = pd.concat(enriched_parts, ignore_index=True)
        keep_cols = list(_ew.WEATHER_VARS)
        enriched_valid = enriched_valid[keep_cols]
        valid_idx = valid.index
        for v in keep_cols:
            df[v] = pd.NA
            df.loc[valid_idx, v] = enriched_valid[v].values

    df = df.drop(columns=[c for c in ['_loc_key'] if c in df.columns])

    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='') as f:
        out = df.copy()
        if 'timestamp' in out.columns and pd.api.types.is_datetime64_any_dtype(out['timestamp']):
            out['timestamp'] = out['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%S%z').str.replace('+0000', '+00:00')
        out.to_csv(f, index=False, encoding='utf-8')

    # Clean up the intermediate sensor CSV.
    if os.path.exists(sensor_path):
        os.remove(sensor_path)
    return path


def weather_export_filename():
    return f"pace_weather_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


# --------------------------------------------------------------------------- #
# Amenities export (OpenStreetMap Overpass)
# --------------------------------------------------------------------------- #
def write_amenities_csv(start=None, end=None):
    """Writes a CSV of amenity counts per survey stop via OpenStreetMap.

    For every unique survey stop location (identified by the shared
    ``gnss_snapshot_id``), queries OpenStreetMap via the Overpass API and
    records the number of distinct ``amenity=*`` types found within a 100 m
    radius.

    Requires an internet connection to reach the Overpass API.

    Returns the absolute path to the written file; caller is responsible
    for cleaning it up after use.
    """
    import extract_amenities as _ea

    # Build the survey stop locations from the collapsed route + survey JSON.
    surveys = build_survey_json(start=start, end=end)
    df = build_sensor_dataframe(start=start, end=end)
    gnss_lookup = _build_gnss_lookup(df)
    clusters = _cluster_surveys(surveys, gnss_lookup)

    # Collect unique stop coordinates from cluster primary GNSS points.
    stops = []
    for cluster in clusters:
        pid = cluster.primary_gnss_id
        if pid is None:
            continue
        pt = gnss_lookup.get(pid)
        if pt is None:
            continue
        stops.append((pid, pt['latitude'], pt['longitude']))

    overpass_url = 'https://overpass-api.de/api/interpreter'
    radius = 100.0

    rows = []
    for gid, lat, lon in stops:
        try:
            amenity_count = _count_unique_amenity_types(lat, lon, radius, overpass_url)
        except Exception:
            amenity_count = None
        rows.append({
            'gnss_snapshot_id': gid,
            'latitude': lat,
            'longitude': lon,
            'unique_amenity_types': amenity_count,
        })

    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='') as f:
        import csv as _csv
        fieldnames = ['gnss_snapshot_id', 'latitude', 'longitude', 'unique_amenity_types']
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return path


def _count_unique_amenity_types(lat, lon, radius, url):
    """Number of distinct ``amenity=*`` values within ``radius`` metres."""
    import urllib.request, urllib.parse, json as _json, time as _time

    query = """
[out:json][timeout:60];
(
  node(around:{radius},{lat},{lon})["amenity"];
  way(around:{radius},{lat},{lon})["amenity"];
);
out center tags;
""".format(lat=lat, lon=lon, radius=int(radius))

    data_str = ('data=' + urllib.parse.quote(query)).encode('utf-8')
    req = urllib.request.Request(url, data=data_str, headers={'User-Agent': 'PACE-amenities/1.0'})

    max_retries = 5
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = _json.loads(resp.read().decode('utf-8'))
            types = set()
            for el in data.get('elements', []):
                tags = el.get('tags', {})
                amenity = tags.get('amenity')
                if amenity:
                    types.add(amenity)
            return len(types)
        except Exception as e:
            last_err = e
            _time.sleep(2 ** attempt)
    raise RuntimeError(f'Overpass request failed after {max_retries} attempts: {last_err}')


def amenities_export_filename():
    return f"pace_amenities_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
