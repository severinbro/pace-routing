"""Staff-only HALO screen and server-side bridge to SENSE's control service."""
import csv
from datetime import datetime
from functools import wraps
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST


def _require_halo_enabled(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not settings.HALO_ENABLED:
            raise Http404('HALO integration is disabled')
        return view_func(request, *args, **kwargs)
    return wrapper

SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*\Z')
EXPORTABLE_ARTIFACTS = (
    'pairs.csv',
    'frames.csv',
    'imu.csv',
    'light.csv',
    'thermal.csv',
    'sync_report.json',
    'capture.json',
)


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
@_require_halo_enabled
@require_GET
def halo_home(request):
    return render(request, 'data_cube/halo_home.html')


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_status(request):
    code, payload = control_request('status')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_sessions(request):
    code, payload = control_request('sessions')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_POST
def halo_start(request):
    code, payload = control_request('recordings/start', 'POST')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_POST
def halo_stop(request):
    code, payload = control_request('recordings/stop', 'POST')
    return JsonResponse(payload, status=code)


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_POST
def halo_poweroff(request):
    code, payload = control_request('poweroff', 'POST')
    return JsonResponse(payload, status=code)


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _resolve_incoming_session(session_id: str) -> tuple[Path | None, str | None]:
    if not SESSION_RE.fullmatch(session_id):
        return None, 'Invalid session ID'
    incoming_root = Path(settings.SENSE_DATA_DIR).resolve() / 'incoming'
    session_dir = (incoming_root / session_id).resolve()
    if session_dir.parent != incoming_root or not session_dir.is_dir():
        return None, 'Session not found'
    return session_dir, None


def _collect_halo_sessions() -> list[dict]:
    data_root = Path(settings.SENSE_DATA_DIR)
    incoming_root = data_root / 'incoming'
    live_root = data_root / 'live'
    if not incoming_root.is_dir():
        return []
    sessions = []
    for folder in sorted(incoming_root.iterdir(), reverse=True):
        if not folder.is_dir() or not SESSION_RE.fullmatch(folder.name):
            continue
        capture = _read_json_file(folder / 'capture.json')
        sync = _read_json_file(folder / 'sync_report.json')
        live_dir = live_root / folder.name
        live_frames = sum(1 for _ in live_dir.glob('frame_*')) if live_dir.is_dir() else 0
        total_bytes = sum(f.stat().st_size for f in folder.rglob('*') if f.is_file())
        artifacts = [name for name in EXPORTABLE_ARTIFACTS if (folder / name).is_file()]
        sessions.append({
            'id': folder.name,
            'started_at_utc': capture.get('started_at_utc', ''),
            'duration_s': capture.get('clip_duration_seconds'),
            'pairs': sync.get('pairs'),
            'unpaired_left': sync.get('unpaired_left_images'),
            'unpaired_right': sync.get('unpaired_right_images'),
            'skew_exceeded': sync.get('pairs_exceeding_stereo_skew_limit'),
            'imu_gaps': sync.get('pairs_without_close_imu_bracket'),
            'light_pairs': sync.get('pairs_with_light'),
            'thermal_missing': sync.get('pairs_without_thermal'),
            'live_frames': live_frames,
            'size_mib': round(total_bytes / (1024 * 1024), 2),
            'artifacts': artifacts,
        })
    return sessions


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_dashboard(request):
    return render(request, 'data_cube/halo_dashboard.html')


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_data(request):
    sessions = _collect_halo_sessions()
    return render(request, 'data_cube/halo_data.html', {
        'sessions': sessions,
        'total_sessions': len(sessions),
        'total_pairs': sum(s['pairs'] or 0 for s in sessions),
        'total_live_frames': sum(s['live_frames'] for s in sessions),
    })


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_export_summary_csv(request):
    sessions = _collect_halo_sessions()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'session_id', 'started_at_utc', 'duration_s', 'pairs',
        'unpaired_left', 'unpaired_right', 'stereo_skew_exceeded',
        'imu_gaps', 'light_pairs', 'thermal_missing', 'live_frames', 'size_mib',
    ])
    for item in sessions:
        writer.writerow([
            item['id'], item['started_at_utc'], item['duration_s'], item['pairs'],
            item['unpaired_left'], item['unpaired_right'], item['skew_exceeded'],
            item['imu_gaps'], item['light_pairs'], item['thermal_missing'],
            item['live_frames'], item['size_mib'],
        ])
    filename = f"halo_sessions_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(buffer.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_export_artifact(request, session_id, artifact):
    if artifact not in EXPORTABLE_ARTIFACTS:
        return JsonResponse({'error': 'Unsupported artifact'}, status=400)
    if not SESSION_RE.fullmatch(session_id):
        return JsonResponse({'error': 'Invalid session ID'}, status=400)
    session_dir, err = _resolve_incoming_session(session_id)
    if err:
        return JsonResponse({'error': err}, status=404)
    target = (session_dir / artifact).resolve()
    if target.parent != session_dir or not target.is_file():
        return JsonResponse({'error': 'Artifact not found'}, status=404)
    ctype = 'application/json' if artifact.endswith('.json') else 'text/csv'
    return FileResponse(
        open(target, 'rb'),
        as_attachment=True,
        filename=f'{session_id}_{artifact}',
        content_type=ctype,
    )


@staff_member_required(login_url='admin_login')
@_require_halo_enabled
@require_GET
def halo_export_zip(request, session_id):
    if not SESSION_RE.fullmatch(session_id):
        return JsonResponse({'error': 'Invalid session ID'}, status=400)
    session_dir, err = _resolve_incoming_session(session_id)
    if err:
        return JsonResponse({'error': err}, status=404)
    include_frames = request.GET.get('include_frames') == '1'
    fd, tmp_name = tempfile.mkstemp(prefix=f'halo_{session_id}_', suffix='.zip')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(session_dir.rglob('*')):
                if not item.is_file() or item.is_symlink():
                    continue
                rel = item.relative_to(session_dir)
                if not include_frames and rel.parts and rel.parts[0] == 'frames':
                    continue
                zf.write(item, arcname=f'{session_id}/{rel.as_posix()}')
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    suffix = 'full' if include_frames else 'metadata'
    response = FileResponse(
        open(tmp_name, 'rb'),
        as_attachment=True,
        filename=f'{session_id}_{suffix}.zip',
        content_type='application/zip',
    )
    response._resource_closers.append(lambda: os.remove(tmp_name) if os.path.exists(tmp_name) else None)
    return response
