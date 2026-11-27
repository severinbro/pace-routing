from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import LoginView
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.conf import settings
import redis, json, subprocess, logging, os, sqlite3, shutil, time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Connect to Redis
r = redis.Redis(host=settings.PACE_REDIS_HOST, port=6379, db=0,
                socket_connect_timeout=1, socket_timeout=1)


# --- CAMPAIGN STATE HELPERS (stored in Redis) ---
# Campaign state is kept in Redis so both the web container and the
# db_writer/sensor worker can read it without hitting the database.
# Keys:
#   campaign:active      -> '1' when a campaign is running
#   campaign:total_stops -> total number of survey stops (int)
#   campaign:current_stop -> index of the currently unlocked stop (1-based)
#   campaign:collect_mode -> '1' when sensor data should be persisted
#   campaign:submitted_stops -> comma-separated list of submitted stop indices
def campaign_state():
    """Returns a dict describing the current campaign state."""
    active = r.get('campaign:active') == b'1'
    total = int(r.get('campaign:total_stops') or 0)
    current = int(r.get('campaign:current_stop') or 0)
    # Track which stops have been submitted so unlock can be guarded.
    submitted_raw = r.get('campaign:submitted_stops') or b''
    submitted_stops = set()
    try:
        submitted_stops = set(int(x) for x in submitted_raw.decode().split(',') if x.strip())
    except (ValueError, AttributeError):
        submitted_stops = set()
    return {
        'active': active,
        'total_stops': total,
        'current_stop': current,
        'collect_mode': r.get('collect_mode') == b'1',
        'submitted_stops': sorted(submitted_stops),
    }


# --- CUSTOM LOGIN (role-based redirect) ---
class RoleLoginView(LoginView):
    """Rate-limits sign-in attempts and redirects successful logins to the info page."""
    template_name = 'data_cube/login.html'

    def _attempt_key(self):
        address = self.request.META.get('HTTP_X_REAL_IP') or self.request.META.get('REMOTE_ADDR', 'unknown')
        return 'login:failures:' + address

    def dispatch(self, request, *args, **kwargs):
        self.request = request
        if request.method == 'POST':
            try:
                if int(r.get(self._attempt_key()) or 0) >= 5:
                    return HttpResponse('Too many sign-in attempts. Try again in 10 minutes.', status=429)
            except redis.RedisError:
                logger.exception('Login rate-limit store unavailable')
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        try:
            key = self._attempt_key()
            attempts = r.incr(key)
            if attempts == 1:
                r.expire(key, 600)
        except redis.RedisError:
            logger.exception('Login rate-limit store unavailable')
        return super().form_invalid(form)

    def form_valid(self, form):
        try:
            r.delete(self._attempt_key())
        except redis.RedisError:
            logger.exception('Login rate-limit store unavailable')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('about')


def _read_mbtiles_metadata(mbtiles_path: Path) -> dict:
    """Reads MBTiles metadata table if the SQLite archive exists."""
    if not mbtiles_path.is_file():
        return {}
    try:
        with closing(sqlite3.connect(f'{mbtiles_path.absolute().as_uri()}?mode=ro&immutable=1', uri=True)) as conn:
            rows = conn.execute('SELECT name, value FROM metadata').fetchall()
        return {str(k): str(v) for k, v in rows}
    except sqlite3.Error as exc:
        logger.warning('Failed to read MBTiles metadata from %s: %s', mbtiles_path, exc)
        return {}


def _load_redis_json(key: str, default):
    try:
        raw = r.get(key)
        if raw:
            return json.loads(raw.decode('utf-8'))
    except (redis.RedisError, ValueError, UnicodeDecodeError):
        pass
    return default


def _get_live_gnss_fix() -> dict:
    """Return latest available GNSS fix (phone preferred, fallback sensor)."""
    phone = _load_redis_json('gnss_phone', None)
    if isinstance(phone, dict) and (phone.get('lat') or phone.get('lon')):
        return {
            'lat': float(phone.get('lat', 0.0)),
            'lon': float(phone.get('lon', 0.0)),
            'accuracy': float(phone.get('accuracy', 0.0) or 0.0),
        }
    snap = _load_redis_json('sensor_measurements', None)
    if isinstance(snap, dict):
        lat = float(snap.get('lat') or snap.get('sensor_lat') or 0.0)
        lon = float(snap.get('lon') or snap.get('sensor_lon') or 0.0)
        if lat or lon:
            return {
                'lat': lat,
                'lon': lon,
                'accuracy': float(snap.get('accuracy', 0.0) or 0.0),
            }
    return {'lat': 0.0, 'lon': 0.0, 'accuracy': 0.0}


# --- MAP (authenticated participants & admins) ---
@login_required(login_url='admin_login')
def map_tab(request):
    """Renders the offline MBTiles Leaflet map with campaign stops."""
    mbtiles_path = Path(settings.OFFLINE_MBTILES_PATH)
    meta = _read_mbtiles_metadata(mbtiles_path)
    center_lat, center_lon, default_zoom = 49.9929, 8.2473, 13
    if 'center' in meta:
        try:
            parts = [float(p.strip()) for p in meta['center'].split(',')]
            if len(parts) >= 2:
                center_lon, center_lat = parts[0], parts[1]
            if len(parts) >= 3:
                default_zoom = int(round(parts[2]))
        except ValueError:
            pass
    try:
        min_zoom = int(meta.get('minzoom', 11))
    except ValueError:
        min_zoom = 11
    try:
        max_zoom = int(meta.get('maxzoom', 18))
    except ValueError:
        max_zoom = 18
    return render(request, 'data_cube/map.html', {
        'mbtiles_available': mbtiles_path.is_file(),
        'mbtiles_name': meta.get('name', 'Offline Map'),
        'center_lat': center_lat,
        'center_lon': center_lon,
        'default_zoom': default_zoom,
        'min_zoom': min_zoom,
        'max_zoom': max_zoom,
    })


@login_required(login_url='admin_login')
def offline_tile(request, z: int, x: int, y: int):
    """Serves a single raster map tile directly from a local MBTiles SQLite file."""
    if z < 0 or z > 22 or x < 0 or y < 0:
        return HttpResponse(status=400)
    mbtiles_path = Path(settings.OFFLINE_MBTILES_PATH)
    if not mbtiles_path.is_file():
        return HttpResponse(status=204)
    tms_y = (1 << z) - 1 - y
    try:
        with closing(sqlite3.connect(f'{mbtiles_path.absolute().as_uri()}?mode=ro&immutable=1', uri=True)) as conn:
            row = conn.execute(
                'SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?',
                (z, x, tms_y),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning('MBTiles query failed for %d/%d/%d: %s', z, x, y, exc)
        return HttpResponse(status=500)
    if not row or not row[0]:
        return HttpResponse(status=204)
    blob = bytes(row[0])
    if blob.startswith(b'\xff\xd8'):
        content_type = 'image/jpeg'
    elif blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        content_type = 'image/webp'
    else:
        content_type = 'image/png'
    response = HttpResponse(blob, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=86400'
    return response


@login_required(login_url='admin_login')
def api_map_data(request):
    """Returns campaign stop pins and live group GNSS position for participants & admins."""
    from data_cube.models import EnvironmentSurvey

    try:
        state = campaign_state()
    except redis.RedisError:
        state = {'active': False, 'total_stops': 0, 'current_stop': 0, 'collect_mode': False, 'submitted_stops': []}

    planned = _load_redis_json('campaign:planned_stops', [])
    recorded = _load_redis_json('campaign:recorded_stops', {})
    stops_by_num = {}

    if isinstance(planned, list):
        for item in planned:
            try:
                num = int(item['stop'])
                lat = float(item['lat'])
                lon = float(item['lon'])
            except (KeyError, TypeError, ValueError):
                continue
            stops_by_num[num] = {
                'stop': num,
                'lat': lat,
                'lon': lon,
                'label': str(item.get('label') or f'Stop {num}'),
                'planned': True,
            }

    if isinstance(recorded, dict):
        for key, item in recorded.items():
            try:
                num = int(key)
                lat = float(item['lat'])
                lon = float(item['lon'])
            except (KeyError, TypeError, ValueError):
                continue
            if num not in stops_by_num and (lat or lon):
                stops_by_num[num] = {
                    'stop': num,
                    'lat': lat,
                    'lon': lon,
                    'label': f'Stop {num}',
                    'planned': False,
                }

    # Fallback: include recent survey stops that have a GNSS snapshot if not already present.
    if state['active'] and state['submitted_stops']:
        surveys = (
            EnvironmentSurvey.objects
            .filter(campaign_stop__in=state['submitted_stops'], gnss_snapshot__isnull=False)
            .select_related('gnss_snapshot')
            .order_by('-id')
        )
        for srv in surveys:
            num = srv.campaign_stop
            if num and num not in stops_by_num:
                lat = float(srv.gnss_snapshot.latitude)
                lon = float(srv.gnss_snapshot.longitude)
                if lat or lon:
                    stops_by_num[num] = {
                        'stop': num,
                        'lat': lat,
                        'lon': lon,
                        'label': f'Stop {num}',
                        'planned': False,
                    }

    submitted_set = set(state.get('submitted_stops', []))
    current_stop = state.get('current_stop', 0)
    stops = []
    for num in sorted(stops_by_num):
        entry = dict(stops_by_num[num])
        if state['active']:
            if num in submitted_set or (current_stop > 0 and num < current_stop):
                entry['status'] = 'completed'
            elif num == current_stop:
                entry['status'] = 'current'
            else:
                entry['status'] = 'upcoming'
        else:
            entry['status'] = 'upcoming'
        stops.append(entry)

    return JsonResponse({
        'campaign': {
            'active': state['active'],
            'total_stops': state['total_stops'],
            'current_stop': current_stop,
            'submitted_stops': sorted(submitted_set),
        },
        'live_fix': _get_live_gnss_fix(),
        'stops': stops,
    })


@staff_member_required(login_url='admin_login')
def campaign_stops(request):
    """Admin-only endpoint to read or replace planned campaign route stop coordinates."""
    if request.method == 'GET':
        return JsonResponse({'stops': _load_redis_json('campaign:planned_stops', [])})
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        raw_stops = payload.get('stops', [])
        if not isinstance(raw_stops, list) or len(raw_stops) > 50:
            return JsonResponse({'error': 'stops must be a list of up to 50 items'}, status=400)
        cleaned = []
        for idx, item in enumerate(raw_stops, start=1):
            lat = float(item['lat'])
            lon = float(item['lon'])
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return JsonResponse({'error': 'Invalid coordinates'}, status=400)
            label = str(item.get('label') or f'Stop {idx}').strip()[:80]
            cleaned.append({'stop': idx, 'lat': round(lat, 6), 'lon': round(lon, 6), 'label': label})
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    r.set('campaign:planned_stops', json.dumps(cleaned))
    return JsonResponse({'stops': cleaned})

# --- ADMIN-ONLY: MEASUREMENT DASHBOARD ---
@staff_member_required(login_url='admin_login')
def dashboard_tab(request):
    """Renders the live sensor telemetry grid."""
    return render(request, 'data_cube/dashboard.html', {
        'hardware_available': settings.SENSE_HARDWARE_AVAILABLE,
    })

@staff_member_required(login_url='admin_login')
def toggle_collect_mode(request):
    """Toggles sensor-data collection on/off (stored in Redis).

    This controls ONLY sensor measurement storage — survey submissions are
    unaffected. The db_writer container checks the "collect_mode" key before
    persisting each reading; when off, messages are consumed and discarded.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    active = request.POST.get('active') == '1'
    if active:
        r.set('collect_mode', '1')
    else:
        r.delete('collect_mode')
    return JsonResponse({'collect_mode': active})

# --- ADMIN-ONLY: DATA BROWSER ---
@staff_member_required(login_url='admin_login')
def data_browser(request):
    """Renders the SENSE data export page."""
    return render(request, 'data_cube/data_browser.html')


def _sense_reading_tables():
    from data_cube.models import (
        GNSSPhoneMeasurement, GNSSSensorMeasurement,
        AtmosphericMeasurement, AccelerometerMeasurement,
        AirQualityMeasurement, ParticulateMeasurement, NoiseMeasurement,
        EnvironmentSurvey,
    )

    return [
        {
            'key': 'gnss_phone',
            'label': 'GNSS Phone',
            'columns': ['ID', 'Timestamp', 'Latitude', 'Longitude', 'Altitude', 'Satellites', 'Accuracy'],
            'rows': list(GNSSPhoneMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites', 'accuracy'
            )),
        },
        {
            'key': 'gnss_sensor',
            'label': 'GNSS Sensor',
            'columns': ['ID', 'Timestamp', 'Latitude', 'Longitude', 'Altitude', 'Satellites'],
            'rows': list(GNSSSensorMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites'
            )),
        },
        {
            'key': 'atmosphere',
            'label': 'Atmosphere',
            'columns': ['ID', 'Timestamp', 'Temperature (°C)', 'Humidity (%)', 'Pressure (hPa)'],
            'rows': list(AtmosphericMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'temperature', 'humidity', 'pressure'
            )),
        },
        {
            'key': 'accelerometer',
            'label': 'Accelerometer',
            'columns': ['ID', 'Timestamp', 'Acc X', 'Acc Y', 'Acc Z', 'Angle X', 'Angle Y', 'Angle Z'],
            'rows': list(AccelerometerMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'accX', 'accY', 'accZ', 'angleX', 'angleY', 'angleZ'
            )),
        },
        {
            'key': 'air_quality',
            'label': 'Air Quality',
            'columns': ['ID', 'Timestamp', 'AQI', 'TVOC (ppb)', 'eCO2 (ppm)'],
            'rows': list(AirQualityMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'aqi', 'tvoc', 'eco2'
            )),
        },
        {
            'key': 'particulates',
            'label': 'Particulates',
            'columns': ['ID', 'Timestamp', 'PM1.0', 'PM2.5', 'PM10'],
            'rows': list(ParticulateMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'pm1', 'pm25', 'pm10'
            )),
        },
        {
            'key': 'noise',
            'label': 'Noise',
            'columns': ['ID', 'Timestamp', 'Noise (dB)'],
            'rows': list(NoiseMeasurement.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'noise_db'
            )),
        },
        {
            'key': 'surveys',
            'label': 'Surveys',
            'columns': ['ID', 'Timestamp', 'User', 'Stop', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6', 'Q7', 'Q8', 'Q9', 'Q10', 'Q11', 'GNSS ID'],
            'rows': list(EnvironmentSurvey.objects.order_by('-id')[:200].values_list(
                'id', 'timestamp', 'user__username', 'campaign_stop',
                'q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7',
                'q8', 'q9', 'q10', 'q11', 'gnss_snapshot_id'
            )),
        },
    ]

# --- ADMIN-ONLY: CSV EXPORT ---
# Import at module level so the (heavy) pandas import happens once at worker
# startup, not on the first export request. A cold pandas import on the RPi5
# plus 7 DB queries previously pushed the first export past Gunicorn's worker
# timeout, producing a 502 that disappeared on the second (warm) request.
from data_cube.exports import (
    write_sensor_csv, export_filename,
    write_survey_json, survey_export_filename,
    write_weather_csv, weather_export_filename,
    write_amenities_csv, amenities_export_filename,
)


def _stream_temp_file(path, filename, content_type):
    def close_and_remove():
        try:
            if file is not None:
                file.close()
        finally:
            Path(path).unlink(missing_ok=True)

    file = None
    try:
        file = open(path, 'rb')
        response = FileResponse(file, as_attachment=True, filename=filename(),
                                content_type=content_type)
        response._resource_closers.append(close_and_remove)
        return response
    except Exception:
        close_and_remove()
        raise


@staff_member_required(login_url='admin_login')
def export_csv(request):
    """Joins the 7 sensor tables on id and streams the result as a .csv file.

    Optional ``start`` and ``end`` query parameters (ISO ``YYYY-MM-DD``) restrict
    the export to the given (inclusive) date range.
    """
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    path = write_sensor_csv(start=start, end=end)
    return _stream_temp_file(path, export_filename, 'text/csv')

# --- ADMIN-ONLY: SURVEY JSON EXPORT ---
@staff_member_required(login_url='admin_login')
def export_survey_json(request):
    """Streams all survey responses as a .json file (one entry per survey).

    Optional ``start`` and ``end`` query parameters (ISO ``YYYY-MM-DD``) restrict
    the export to the given (inclusive) date range.
    """
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    path = write_survey_json(start=start, end=end)
    return _stream_temp_file(path, survey_export_filename, 'application/json')

# --- ADMIN-ONLY: WEATHER CSV EXPORT (Open-Meteo, needs internet) ---
@staff_member_required(login_url='admin_login')
def export_weather_csv(request):
    """Streams a weather-enriched measurements CSV using Open-Meteo data.

    Requires an internet connection to reach the Open-Meteo API.  The weather
    data covers the same timespan and locations as the measured data.

    Optional ``start`` and ``end`` query parameters (ISO ``YYYY-MM-DD``) restrict
    the export to the given (inclusive) date range.
    """
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    path = write_weather_csv(start=start, end=end)
    return _stream_temp_file(path, weather_export_filename, 'text/csv')

# --- ADMIN-ONLY: AMENITIES CSV EXPORT (Overpass, needs internet) ---
@staff_member_required(login_url='admin_login')
def export_amenities_csv(request):
    """Streams a CSV of amenity counts per survey stop via OpenStreetMap.

    Requires an internet connection to reach the Overpass API.  For every
    unique survey stop location, records the number of distinct ``amenity=*``
    types found within a 100 m radius.

    Optional ``start`` and ``end`` query parameters (ISO ``YYYY-MM-DD``) restrict
    the export to the given (inclusive) date range.
    """
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    path = write_amenities_csv(start=start, end=end)
    return _stream_temp_file(path, amenities_export_filename, 'text/csv')

# --- ADMIN-ONLY: LEGACY LANDING REDIRECT ---
@staff_member_required(login_url='admin_login')
def admin_home(request):
    """Redirect legacy /admin-home/ URL to the project info page."""
    return redirect('about')


def _read_proc_cpu_times():
    try:
        line = Path('/proc/stat').read_text().splitlines()[0]
        parts = [int(v) for v in line.split()[1:9]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except (OSError, IndexError, ValueError):
        return None


_cpu_sample_lock = Lock()
_previous_cpu_times = None


def _cpu_percent():
    global _previous_cpu_times
    with _cpu_sample_lock:
        current = _read_proc_cpu_times()
        previous = _previous_cpu_times
        if current is None:
            return None
        _previous_cpu_times = current
    if previous is None or current[0] <= previous[0]:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    return round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 1)


def _sense_system_metrics() -> dict:
    """Collect lightweight host CPU, RAM, disk, and temperature metrics via procfs/stdlib."""
    cpu_pct = _cpu_percent()

    mem_total = mem_avail = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                mem_total = int(line.split()[1]) * 1024
            elif line.startswith('MemAvailable:'):
                mem_avail = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    mem_used = (mem_total - mem_avail) if (mem_total and mem_avail is not None) else None
    mem_pct = round(100.0 * mem_used / mem_total, 1) if (mem_total and mem_used is not None) else None

    data_path = Path(settings.SENSE_DATA_DIR)
    try:
        du = shutil.disk_usage(data_path if data_path.exists() else Path('/'))
        disk_total, disk_used, disk_free = du.total, du.used, du.free
        disk_pct = round(100.0 * disk_used / disk_total, 1) if disk_total else None
    except OSError:
        disk_total = disk_used = disk_free = disk_pct = None

    temp_c = None
    try:
        temp_c = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass

    try:
        load_1m = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load_1m = None

    return {
        'cpu_percent': cpu_pct,
        'cpu_count': os.cpu_count(),
        'load_1m': load_1m,
        'cpu_temp_c': temp_c,
        'mem_total_bytes': mem_total,
        'mem_used_bytes': mem_used,
        'mem_percent': mem_pct,
        'disk_total_bytes': disk_total,
        'disk_used_bytes': disk_used,
        'disk_free_bytes': disk_free,
        'disk_percent': disk_pct,
    }


@staff_member_required(login_url='admin_login')
def sense_home(request):
    """Renders the SENSE Controller with recording controls and saved readings."""
    return render(request, 'data_cube/sense_home.html', {
        'hardware_available': settings.SENSE_HARDWARE_AVAILABLE,
        'tables': _sense_reading_tables(),
    })


@staff_member_required(login_url='admin_login')
def sense_system_status(request):
    """Returns SENSE collection status and live CPU/RAM/Storage metrics."""
    try:
        state = campaign_state()
    except redis.RedisError:
        state = {'active': False, 'total_stops': 0, 'current_stop': 0, 'collect_mode': False, 'submitted_stops': []}
    return JsonResponse({
        'collect_mode': state['collect_mode'],
        'campaign': state,
        'system': _sense_system_metrics(),
        'mbtiles_available': Path(settings.OFFLINE_MBTILES_PATH).is_file(),
    })

# --- ADMIN-ONLY: CAMPAIGN HUB ---
@staff_member_required(login_url='admin_login')
def campaign_hub(request):
    """Renders the campaign management dashboard for admins."""
    return render(request, 'data_cube/campaign_hub.html', {'campaign': campaign_state()})

@staff_member_required(login_url='admin_login')
def campaign_start(request):
    """Starts a new measurement campaign with a given number of stops."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        total_stops = int(request.POST.get('total_stops', 0))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid number'}, status=400)
    if total_stops < 1:
        return JsonResponse({'error': 'At least 1 stop required'}, status=400)

    r.set('campaign:active', '1')
    r.set('campaign:total_stops', total_stops)
    r.set('campaign:current_stop', 0)  # no stop unlocked yet
    r.delete('campaign:submitted_stops', 'campaign:recorded_stops')  # clear prior tracking
    r.set('collect_mode', '1')  # start collecting sensor data
    r.set('campaign:collect_mode', '1')  # mirror for campaign hub display
    return JsonResponse(campaign_state())

@staff_member_required(login_url='admin_login')
def campaign_abort(request):
    """Aborts the active campaign and stops data collection."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    r.delete('campaign:active', 'campaign:total_stops', 'campaign:current_stop',
             'campaign:collect_mode', 'campaign:submitted_stops', 'campaign:recorded_stops', 'collect_mode')
    return JsonResponse({'active': False})

@staff_member_required(login_url='admin_login')
def campaign_unlock_stop(request):
    """Unlocks the next survey stop for participants.

    Guards against the double-press bug: the current stop must have been
    submitted before the next one can be unlocked.  This prevents an
    accidental second click from skipping a stop and storing it without data.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    state = campaign_state()
    if not state['active']:
        return JsonResponse({'error': 'No active campaign'}, status=400)
    current = state['current_stop']
    # If a stop is currently unlocked but has not been submitted yet, refuse
    # to advance — the participant must still submit the current stop.
    if current > 0 and current not in state['submitted_stops']:
        return JsonResponse({
            'error': 'Current stop has not been submitted yet',
            'current_stop': current,
        }, status=409)
    next_stop = current + 1
    if next_stop > state['total_stops']:
        return JsonResponse({'error': 'All stops already unlocked'}, status=400)
    r.set('campaign:current_stop', next_stop)
    fix = _get_live_gnss_fix()
    if fix['lat'] or fix['lon']:
        recorded = _load_redis_json('campaign:recorded_stops', {})
        if not isinstance(recorded, dict):
            recorded = {}
        recorded[str(next_stop)] = {'lat': fix['lat'], 'lon': fix['lon']}
        r.set('campaign:recorded_stops', json.dumps(recorded))
    return JsonResponse(campaign_state())

@staff_member_required(login_url='admin_login')
def campaign_toggle_collect(request):
    """Toggles sensor data collection on/off during a campaign.

    Sets both the standalone "collect_mode" key (checked by the db_writer)
    and the "campaign:collect_mode" mirror (for the campaign hub display).
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    active = request.POST.get('active') == '1'
    if active:
        r.set('collect_mode', '1')
        r.set('campaign:collect_mode', '1')
    else:
        r.delete('collect_mode')
        r.delete('campaign:collect_mode')
    return JsonResponse({'collect_mode': active})

@staff_member_required(login_url='admin_login')
def campaign_status(request):
    """Returns the current campaign state as JSON (for AJAX polling)."""
    return JsonResponse(campaign_state())

def _dbus_poweroff_now():
    """Invoke systemd-logind PowerOff over the mounted host system D-Bus."""
    import subprocess
    subprocess.run(
        [
            'dbus-send', '--system', '--print-reply',
            '--dest=org.freedesktop.login1',
            '/org/freedesktop/login1',
            'org.freedesktop.login1.Manager.PowerOff',
            'boolean:false',
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _schedule_host_poweroff():
    """Schedule a graceful SENSE host shutdown via host D-Bus, falling back to sysrq."""
    import os
    import shutil
    import subprocess
    import threading

    dbus_socket = '/run/dbus/system_bus_socket'
    if os.path.exists(dbus_socket) and shutil.which('dbus-send'):
        probe = subprocess.run(
            [
                'dbus-send', '--system', '--print-reply',
                '--dest=org.freedesktop.login1',
                '/org/freedesktop/login1',
                'org.freedesktop.login1.Manager.CanPowerOff',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if probe.returncode != 0 or '"yes"' not in probe.stdout:
            detail = (probe.stderr or probe.stdout).strip()[:160]
            raise OSError(detail or 'Host D-Bus rejected PowerOff check')
        timer = threading.Timer(2.0, _dbus_poweroff_now)
        timer.daemon = True
        timer.start()
        return {'shutting_down': True, 'method': 'systemd-logind', 'delay_seconds': 2}

    with open('/proc/sysrq-trigger', 'w') as trigger:
        trigger.write('o')
    return {'shutting_down': True, 'method': 'sysrq'}


@staff_member_required(login_url='admin_login')
def campaign_poweroff(request):
    """Powers off the SENSE host machine (Raspberry Pi 5).

    Uses graceful systemd-logind PowerOff over the mounted host D-Bus socket
    when available (SENSE Compose stack), and falls back to /proc/sysrq-trigger
    for the legacy privileged standalone Compose stack.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    logger.warning('Poweroff requested by user %s — shutting down SENSE host.',
                   request.user.username)
    try:
        payload = _schedule_host_poweroff()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error('Poweroff failed: %s', exc)
        return JsonResponse({'error': 'Poweroff failed: ' + str(exc)}, status=500)
    return JsonResponse(payload)

# --- ADMIN-ONLY: CREATE NON-ADMIN USER ---
@staff_member_required(login_url='admin_login')
def create_user(request):
    """Allows an admin to create a new non-admin (participant) account."""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = False  # Non-admin: no dashboard/map/data access
            user.save()
            from urllib.parse import urlencode
            return redirect(f"{reverse('create_user')}?{urlencode({'created': user.username})}")
    else:
        form = UserCreationForm()
    return render(request, 'data_cube/create_user.html', {
        'form': form,
        'created_username': request.GET.get('created', ''),
    })

# --- PUBLIC: SELF-SERVICE SIGN UP ---
def sign_up(request):
    """Allows participants to create their own anonymous account.

    Only a username and password are required so that participants stay
    anonymous. Created accounts are non-admin (is_staff=False).
    """
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = False
            user.save()
            login(request, user)
            return redirect('about')
    else:
        form = UserCreationForm()
    return render(request, 'data_cube/sign_up.html', {'form': form})

# --- PUBLIC: ABOUT / PROJECT INFO ---
def about(request):
    """Renders the project info landing page (visible to everyone)."""
    return render(request, 'data_cube/about.html')

# --- SURVEY (the single survey page, served at the root URL) ---
@login_required(login_url='admin_login')
def surveys_tab(request):
    """Renders the environmental survey, gated by the campaign state.

    - No active campaign or no stop unlocked: shows a 'waiting' screen.
    - Stop unlocked: shows the survey form.
    - All stops completed: shows a 'campaign complete' screen.
    """
    from data_cube.models import EnvironmentSurvey

    # Phase 1: Environmental features (q1-q7)
    phase1_features = [
        {"id": 1, "label": "Noise",            "low": "Very Quiet",    "high": "Very Loud"},
        {"id": 2, "label": "Air Quality",      "low": "Very Poor",     "high": "Excellent"},
        {"id": 3, "label": "Air Temperature",  "low": "Very Cold",     "high": "Very Hot"},
        {"id": 4, "label": "Aesthetics",       "low": "Unappealing",   "high": "Beautiful"},
        {"id": 5, "label": "Diversity",        "low": "Monotonous",    "high": "Diverse"},
        {"id": 6, "label": "Urban Design",     "low": "Poorly Designed","high": "Well Designed"},
        {"id": 7, "label": "Accessibility",    "low": "Inaccessible",  "high": "Fully Accessible"},
    ]

    # Phase 2: Personal perception (q8-q10)
    phase2_questions = [
        {"id": 8,  "label": "How safe do you feel in this environment?",
         "low": "Very Unsafe", "high": "Very Safe"},
        {"id": 9,  "label": "How likely is it for you to enjoy staying here?",
         "low": "Very Unlikely", "high": "Very Likely"},
        {"id": 10, "label": "How stressed are you by the current environment?",
         "low": "Not Stressed", "high": "Very Stressed"},
    ]

    state = campaign_state()

    if request.method == 'POST':
        # Only accept survey submissions when a stop is unlocked.
        if not state['active'] or state['current_stop'] == 0:
            return redirect('surveys_tab')

        gnss_snapshot = _capture_gnss_snapshot()

        EnvironmentSurvey.objects.create(
            user=request.user,
            q1=int(request.POST.get('q1')), q2=int(request.POST.get('q2')),
            q3=int(request.POST.get('q3')), q4=int(request.POST.get('q4')),
            q5=int(request.POST.get('q5')), q6=int(request.POST.get('q6')),
            q7=int(request.POST.get('q7')),
            q8=int(request.POST.get('q8')), q9=int(request.POST.get('q9')),
            q10=int(request.POST.get('q10')),
            q11=int(request.POST.get('q11')),
            gnss_snapshot=gnss_snapshot,
            campaign_stop=state['current_stop'],
        )

        # Mark this stop as submitted so the admin can unlock the next one.
        submitted = set(state.get('submitted_stops', set()))
        submitted.add(state['current_stop'])
        r.set('campaign:submitted_stops', ','.join(str(s) for s in sorted(submitted)))

        # If this was the last stop, reset the campaign so the success
        # message shows once (via the ?complete=1 flag) and the state then
        # returns to "no active campaign" instead of persisting indefinitely.
        was_last_stop = state['current_stop'] >= state['total_stops']
        if was_last_stop:
            r.delete('campaign:active', 'campaign:total_stops',
                     'campaign:current_stop', 'campaign:collect_mode',
                     'campaign:submitted_stops', 'campaign:recorded_stops', 'collect_mode')
            # Redirect directly with complete=1 so the thank-you screen shows
            # once; subsequent loads see no active campaign (waiting screen).
            return redirect(reverse('surveys_tab') + '?done=1&complete=1')

        return redirect('surveys_tab_done')

    submitted = request.GET.get('done') == '1'
    campaign_complete_flag = request.GET.get('complete') == '1'
    force_waiting = request.GET.get('waiting') == '1'

    # Determine which screen to show:
    #   - campaign complete (all stops done)
    #   - thank-you / phase-4 (just submitted via ?done=1, not waiting, not complete)
    #   - waiting (no stop unlocked yet, or participant already submitted this stop)
    #   - survey form (stop unlocked and not yet submitted by this participant)
    campaign_complete = (campaign_complete_flag or
                         (state['active'] and state['current_stop'] >= state['total_stops'] and state['total_stops'] > 0))
    # A stop is "done for this participant" if it has already been submitted.
    # The current stop is unlocked (current_stop > 0) but once it's in the
    # submitted set we must keep showing the waiting screen so the form can't
    # be submitted twice. The admin then unlocks the *next* stop, at which
    # point current_stop is no longer in submitted_stops and the form returns.
    current_submitted = state['active'] and state['current_stop'] in set(state.get('submitted_stops', []))
    if force_waiting:
        waiting = True
    else:
        waiting = (not state['active']) or state['current_stop'] == 0 or current_submitted

    return render(request, 'data_cube/survey_environment.html', {
        'phase1_features': phase1_features,
        'phase2_questions': phase2_questions,
        'submitted': submitted,
        'campaign': state,
        'campaign_complete': campaign_complete,
        'waiting': waiting,
    })


@login_required(login_url='admin_login')
def surveys_tab_done(request):
    """Shows the thank-you confirmation after a survey submission."""
    from django.urls import reverse
    state = campaign_state()
    # The campaign keys are deleted when the last stop is submitted, so
    # state['active'] will be False in that case.  The ?complete=1 flag is set
    # by the redirect below so the thank-you screen shows once, after which
    # the survey page returns to the waiting screen (no active campaign).
    campaign_complete = (state['active'] and
                         state['current_stop'] >= state['total_stops'] and
                         state['total_stops'] > 0)
    return redirect(reverse('surveys_tab') + '?done=1&complete=' + ('1' if campaign_complete else '0'))


def _capture_gnss_snapshot():
    """Reads the current phone GNSS fix from Redis and saves a GNSSPhoneMeasurement."""
    from data_cube.models import GNSSPhoneMeasurement
    raw = r.get('gnss_phone')
    if raw:
        try:
            fix = json.loads(raw)
            return GNSSPhoneMeasurement.objects.create(
                latitude=fix.get('lat', 0.0),
                longitude=fix.get('lon', 0.0),
                altitude=fix.get('alt', 0.0),
                satellites=fix.get('sats', 0),
                accuracy=fix.get('accuracy', 0.0),
            )
        except (ValueError, KeyError):
            pass
    return None


# --- API ---
@staff_member_required(login_url='admin_login')
def api_latest_sensors(request):
    """Returns the Redis sensor snapshot as JSON for AJAX calls."""
    data = r.get('sensor_measurements')
    if data:
        return JsonResponse(json.loads(data))
    return JsonResponse({
        "tempC": 0.0, "humRH": 0.0, "preshPa": 0.0, 
        "aqi": 0, "eco2": 0, "tvoc": 0, 
        "ang": [0,0,0], "acc": [0,0,0],
        "lat": 0.0, "lon": 0.0, "alt": 0, "sats": 0, "accuracy": 0.0,
        "sensor_lat": 0.0, "sensor_lon": 0.0, "sensor_alt": 0, "sensor_sats": 0,
        "pm1": 999, "pm25": 999, "pm10": 999,
        "noise": 0
    })

def api_update_gnss(request):
    """Receives GNSS coordinates from the admin smartphone's browser.

    Only authenticated admin (is_staff) users may push GNSS fixes. This ensures
    that when multiple smartphones are connected, only the admin device acts as
    the GNSS source. The fix is stored in Redis under 'gnss_phone' so
    sensor_manager.py can merge it into the sensor snapshot. If a UTC timestamp
    is provided, the RPi5 system clock is also synchronized.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    # Only admin users may push GNSS data
    if not (request.user.is_authenticated and request.user.is_staff):
        return JsonResponse({'status': 'error', 'message': 'Admin access required'}, status=403)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({'status': 'error', 'message': 'Expected a JSON object'}, status=400)

    clock_time = None
    utc_iso = payload.get('utc_time')
    if utc_iso is not None and os.environ.get('PACE_PHONE_CLOCK_SYNC', 'true').lower() == 'true':
        try:
            timestamp = datetime.fromisoformat(utc_iso)
            if timestamp.utcoffset() is None:
                raise ValueError('Missing time zone')
            clock_time = timestamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError, OverflowError):
            return JsonResponse({'status': 'error', 'message': 'Invalid utc_time'}, status=400)

    lat = payload.get('lat')
    lon = payload.get('lon')
    if lat is None or lon is None:
        return JsonResponse({'status': 'error', 'message': 'lat/lon required'}, status=400)

    fix = {
        'lat': float(lat),
        'lon': float(lon),
        'alt': float(payload.get('alt', 0.0) or 0.0),
        'accuracy': float(payload.get('accuracy', 0.0) or 0.0),
        'altitude_accuracy': float(payload.get('alt_accuracy', 0.0) or 0.0),
        'sats': 0,  # Browser API does not expose satellite count
        'timestamp': payload.get('timestamp', 0),
        'received_at': time.time(),
    }

    # Persist the fix for sensor_manager.py to consume
    r.set('gnss_phone', json.dumps(fix))

    if clock_time is not None:
        try:
            subprocess.run(
                ['date', '-u', '-s', clock_time],
                check=True,
                capture_output=True,
                timeout=2,
            )
            logger.info('System clock synced to %s UTC via phone GNSS.', clock_time)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning('Phone clock sync failed: %s', exc)

    return JsonResponse({'status': 'ok'})
