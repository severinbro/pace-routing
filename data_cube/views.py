from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import LoginView
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse
import redis, json, subprocess, logging, os

logger = logging.getLogger(__name__)

# Connect to Redis
r = redis.Redis(host='redis', port=6379, db=0)


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
    """Redirects admin users to admin_home, non-admin users to surveys_tab."""
    template_name = 'data_cube/login.html'

    def get_success_url(self):
        user = self.request.user
        if user.is_staff:
            return reverse('admin_home')
        return reverse('about')


# --- ADMIN-ONLY: MAP ---
@staff_member_required(login_url='admin_login')
def map_tab(request):
    """Renders the offline OSM map."""
    return render(request, 'data_cube/map.html')

# --- ADMIN-ONLY: MEASUREMENT DASHBOARD ---
@staff_member_required(login_url='admin_login')
def dashboard_tab(request):
    """Renders the live sensor telemetry grid."""
    return render(request, 'data_cube/dashboard.html')

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
    """Renders a custom data browser page with tabbed CMS-style tables."""
    from data_cube.models import (
        GNSSPhoneMeasurement, GNSSSensorMeasurement,
        AtmosphericMeasurement, AccelerometerMeasurement,
        AirQualityMeasurement, ParticulateMeasurement, NoiseMeasurement,
        EnvironmentSurvey,
    )

    tables = [
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

    return render(request, 'data_cube/data_browser.html', {
        'tables': tables,
        'collect_mode': r.get('collect_mode') == b'1',
    })

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


@staff_member_required(login_url='admin_login')
def export_csv(request):
    """Joins the 7 sensor tables on id and streams the result as a .csv file.

    Optional ``start`` and ``end`` query parameters (ISO ``YYYY-MM-DD``) restrict
    the export to the given (inclusive) date range.
    """
    start = request.GET.get('start') or None
    end = request.GET.get('end') or None
    path = write_sensor_csv(start=start, end=end)
    try:
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=export_filename(),
            content_type='text/csv',
        )
        response._resource_closers.append(lambda: os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

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
    try:
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=survey_export_filename(),
            content_type='application/json',
        )
        response._resource_closers.append(lambda: os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

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
    try:
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=weather_export_filename(),
            content_type='text/csv',
        )
        response._resource_closers.append(lambda: os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

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
    try:
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=amenities_export_filename(),
            content_type='text/csv',
        )
        response._resource_closers.append(lambda: os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

# --- ADMIN-ONLY: LANDING PAGE AFTER SIGN IN ---
@staff_member_required(login_url='admin_login')
def admin_home(request):
    """Renders the admin landing page with links to Dashboard, Map and Data."""
    return render(request, 'data_cube/admin_home.html')

# --- ADMIN-ONLY: CAMPAIGN HUB ---
@staff_member_required(login_url='admin_login')
def campaign_hub(request):
    """Renders the campaign management dashboard for admins."""
    return render(request, 'data_cube/campaign_hub.html', campaign_state())

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
    r.delete('campaign:submitted_stops')  # clear submitted-stop tracking
    r.set('collect_mode', '1')  # start collecting sensor data
    r.set('campaign:collect_mode', '1')  # mirror for campaign hub display
    return JsonResponse(campaign_state())

@staff_member_required(login_url='admin_login')
def campaign_abort(request):
    """Aborts the active campaign and stops data collection."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    r.delete('campaign:active', 'campaign:total_stops', 'campaign:current_stop',
             'campaign:collect_mode', 'campaign:submitted_stops', 'collect_mode')
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

@staff_member_required(login_url='admin_login')
def campaign_poweroff(request):
    """Powers off the host machine (Raspberry Pi 5).

    The web container runs in privileged mode, so writing 'o' (poweroff)
    to /proc/sysrq-trigger is forwarded to the host kernel and immediately
    powers down the device.  Intended for a controlled "Switch Off" from
    the campaign hub after a double confirmation from the user.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    logger.warning('Poweroff requested by user %s — shutting down host.',
                   request.user.username)
    try:
        with open('/proc/sysrq-trigger', 'w') as trigger:
            trigger.write('o')
    except OSError as exc:
        logger.error('Poweroff failed: %s', exc)
        return JsonResponse({'error': 'Poweroff failed: ' + str(exc)}, status=500)
    # The host powers off immediately, so this response may never arrive —
    # the client shows a farewell message and the page simply goes blank.
    return JsonResponse({'shutting_down': True})

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
            return redirect('admin_home')
    else:
        form = UserCreationForm()
    return render(request, 'data_cube/create_user.html', {'form': form})

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
                     'campaign:submitted_stops', 'collect_mode')
            # Redirect directly with complete=1 so the thank-you screen shows
            # once; subsequent loads see no active campaign (waiting screen).
            from django.urls import reverse
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

@csrf_exempt
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
        'received_at': __import__('time').time(),
    }

    # Persist the fix for sensor_manager.py to consume
    r.set('gnss_phone', json.dumps(fix))

    # --- Sync the RPi5 system clock if a UTC timestamp is provided ---
    utc_iso = payload.get('utc_time')
    if utc_iso:
        try:
            # Expecting ISO-8601 UTC, e.g. "2026-08-08T14:32:05.123Z"
            # Strip fractional seconds and trailing 'Z' for the `date` command.
            clean = utc_iso.replace('Z', '').split('.')[0]
            # date -s accepts "YYYY-MM-DD HH:MM:SS" UTC when --utc is set
            subprocess.run(
                ['date', '-u', '-s', clean.replace('T', ' ')],
                check=True,
                capture_output=True,
                timeout=2,
            )
            logger.info(f"System clock synced to {clean} UTC via phone GNSS.")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Clock sync failed (CalledProcessError): {e.stderr.decode(errors='ignore')}")
        except subprocess.TimeoutExpired:
            logger.warning("Clock sync timed out.")
        except Exception as e:
            logger.warning(f"Clock sync failed: {e}")

    return JsonResponse({'status': 'ok'})