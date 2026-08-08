from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import LoginView
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse
import redis, json, subprocess, logging

logger = logging.getLogger(__name__)

# Connect to Redis
r = redis.Redis(host='redis', port=6379, db=0)


# --- CUSTOM LOGIN (role-based redirect) ---
class RoleLoginView(LoginView):
    """Redirects admin users to admin_home, non-admin users to surveys_tab."""
    template_name = 'data_cube/login.html'

    def get_success_url(self):
        user = self.request.user
        if user.is_staff:
            return reverse('admin_home')
        return reverse('surveys_tab')


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

# --- ADMIN-ONLY: LANDING PAGE AFTER SIGN IN ---
@staff_member_required(login_url='admin_login')
def admin_home(request):
    """Renders the admin landing page with links to Dashboard, Map and Data."""
    return render(request, 'data_cube/admin_home.html')

# --- ADMIN-ONLY: CREATE NON-ADMIN USER ---
@staff_member_required(login_url='admin_login')
def create_user(request):
    """Allows an admin to create a new non-admin (surveyor) account."""
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

# --- TAB: SURVEY DASHBOARD (LOBBY) ---
def surveys_tab(request):
    """Renders the landing page for the two survey types."""
    return render(request, 'data_cube/surveys_lobby.html')

# --- SURVEY SUB-PAGES ---
@login_required(login_url='admin_login')
def survey_environment(request):
    from data_cube.models import EnvironmentSurvey, GNSSMeasurement

    if request.method == 'POST':
        # 1. Capture the current GNSS fix from Redis
        gnss_snapshot = _capture_gnss_snapshot()

        # 2. Create the survey record with the logged-in user
        EnvironmentSurvey.objects.create(
            user=request.user,
            q1=int(request.POST.get('q1')), q2=int(request.POST.get('q2')),
            q3=int(request.POST.get('q3')), q4=int(request.POST.get('q4')),
            q5=int(request.POST.get('q5')), q6=int(request.POST.get('q6')),
            q7=int(request.POST.get('q7')), q8=int(request.POST.get('q8')),
            q9=int(request.POST.get('q9')), q10=int(request.POST.get('q10')),
            gnss_snapshot=gnss_snapshot,
        )
        return redirect('surveys_tab')

    return render(request, 'data_cube/survey_environment.html')

@login_required(login_url='admin_login')
def survey_priority(request):
    """Renders the continuous slider survey."""
    from data_cube.models import RelativeImportanceSurvey

    comparison_pairs = [
        {"id": "q1", "a": "Air Quality", "b": "Path Smoothness"},
        {"id": "q2", "a": "Noise Level", "b": "Visual Aesthetics"},
        {"id": "q3", "a": "Greenery", "b": "Path Width"},
        {"id": "q4", "a": "Lighting", "b": "Safety"},
        {"id": "q5", "a": "Cleanliness", "b": "Accessibility"},
        {"id": "q6", "a": "Shade", "b": "Air Flow"},
        {"id": "q7", "a": "Traffic Volume", "b": "Pedestrian Density"},
        {"id": "q8", "a": "Surface Quality", "b": "Noise Reflection"},
        {"id": "q9", "a": "Rest Areas", "b": "Connectivity"},
        {"id": "q10", "a": "Aesthetics", "b": "Functionality"},
    ]

    if request.method == 'POST':
        gnss_snapshot = _capture_gnss_snapshot()

        RelativeImportanceSurvey.objects.create(
            user=request.user,
            q1=float(request.POST.get('q1', 0.5)), q2=float(request.POST.get('q2', 0.5)),
            q3=float(request.POST.get('q3', 0.5)), q4=float(request.POST.get('q4', 0.5)),
            q5=float(request.POST.get('q5', 0.5)), q6=float(request.POST.get('q6', 0.5)),
            q7=float(request.POST.get('q7', 0.5)), q8=float(request.POST.get('q8', 0.5)),
            q9=float(request.POST.get('q9', 0.5)), q10=float(request.POST.get('q10', 0.5)),
            gnss_snapshot=gnss_snapshot,
        )
        return redirect('surveys_tab')

    return render(request, 'data_cube/survey_priority.html', {'pairs': comparison_pairs})


def _capture_gnss_snapshot():
    """Reads the current phone GNSS fix from Redis and saves a GNSSMeasurement."""
    from data_cube.models import GNSSMeasurement
    raw = r.get('gnss_phone')
    if raw:
        try:
            fix = json.loads(raw)
            return GNSSMeasurement.objects.create(
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