from django.shortcuts import render, redirect
from django.http import JsonResponse
import redis, json

# Connect to Redis
r = redis.Redis(host='redis', port=6379, db=0)

# --- TAB 1: MAP ---
def map_tab(request):
    """Renders the offline OSM map."""
    return render(request, 'data_cube/map.html')

# --- TAB 2: MEASUREMENT DASHBOARD ---
def dashboard_tab(request):
    """Renders the live sensor telemetry grid."""
    return render(request, 'data_cube/dashboard.html')

# --- TAB 3: SURVEY DASHBOARD (LOBBY) ---
def surveys_tab(request):
    """Renders the landing page for the two survey types."""
    return render(request, 'data_cube/surveys_lobby.html')

# --- SURVEY SUB-PAGES ---
def survey_environment(request):
    if request.method == "POST":
        # 1. Capture the form data from the tablet
        # 2. Capture the current sensor state from Redis
        # 3. Create the Database records
        # 4. Redirect back to the lobby
        pass
    return render(request, 'data_cube/survey_environment.html')

def survey_priority(request):
    """Renders the continuous slider survey."""
    # Placeholder for the 10 pairs - we'll define these properly later
    comparison_pairs = [
        {"id": "q1", "a": "Air Quality", "b": "Path Smoothness"},
        {"id": "q2", "a": "Noise Level", "b": "Visual Aesthetics"},
        # Add more as you define them...
    ]
    return render(request, 'data_cube/survey_priority.html', {'pairs': comparison_pairs})

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
        "lat": 0.0, "lon": 0.0, "alt": 0, "sats": 0,
        "pm1": 999, "pm25": 999, "pm10": 999,
        "noise": 0
    })