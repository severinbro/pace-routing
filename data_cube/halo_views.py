"""Staff-only HALO screen and server-side bridge to SENSE's control service."""
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST


def control_request(path, method='GET'):
    try:
        token = Path(settings.HALO_INTERNAL_TOKEN_FILE).read_text().strip()
    except OSError:
        return 503, {'error': 'HALO control token is not configured'}
    if not token:
        return 503, {'error': 'HALO control token is not configured'}
    url = settings.HALO_DASHBOARD_URL.rstrip('/') + '/internal/' + path
    request = Request(url, method=method, headers={'X-SENSE-Internal-Token': token})
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        try:
            return error.code, json.load(error)
        except (ValueError, OSError):
            return error.code, {'error': 'HALO control request failed'}
    except (URLError, OSError, ValueError) as error:
        return 503, {'error': 'HALO control is unavailable: ' + str(error)[:160]}


@staff_member_required(login_url='admin_login')
@require_GET
def halo_home(request):
    return render(request, 'data_cube/halo_home.html')


@staff_member_required(login_url='admin_login')
@require_GET
def halo_status(request):
    code, payload = control_request('status')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@require_GET
def halo_sessions(request):
    code, payload = control_request('sessions')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@require_POST
def halo_start(request):
    code, payload = control_request('recordings/start', 'POST')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@require_POST
def halo_stop(request):
    code, payload = control_request('recordings/stop', 'POST')
    return JsonResponse(payload, status=code)
