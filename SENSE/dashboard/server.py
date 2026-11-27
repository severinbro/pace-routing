#!/usr/bin/env python3
"""Internal SENSE service for HALO control and transfer reconciliation."""
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from verify import verify_session_structure

DATA = Path(os.environ.get('SENSE_DATA_ROOT', '/data'))
SSH_KEY = '/config/halo-dashboard-key'
KNOWN_HOSTS = '/config/known_hosts'
INTERNAL_TOKEN_FILE = Path(os.environ.get('DASHBOARD_INTERNAL_TOKEN_FILE', '/config/dashboard-internal.token'))
HALO = os.environ.get('HALO_SSH_TARGET', '').strip()
SESSION_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
RECONCILED = set()


def halo_command(*args):
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + KNOWN_HOSTS,
               '-i', SSH_KEY, HALO, *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError((result.stdout or result.stderr).strip()[:250])
    return json.loads(result.stdout)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def transfers():
    folder = DATA / 'metrics'
    if not folder.exists():
        return []
    return sorted((read_json(p) for p in folder.glob('*.json') if SESSION_RE.fullmatch(p.stem)),
                  key=lambda x: x.get('updated_at', 0), reverse=True)[:30]


def sense_sessions():
    incoming = DATA / 'incoming'
    results = []
    if incoming.exists():
        for path in sorted(incoming.iterdir(), reverse=True):
            if not path.is_dir() or not SESSION_RE.fullmatch(path.name):
                continue
            receipt = read_json(path / '.transfer_complete')
            if receipt.get('state') != 'committed':
                continue
            results.append({'id': path.name, 'stage': 'received', 'sync': read_json(path / 'sync_report.json'),
                            'capture': read_json(path / 'capture.json'), 'receipt': receipt})
    return results[:50]


def merged_sessions(halo_items):
    merged = {item['id']: item for item in halo_items}
    for item in sense_sessions():
        current = merged.get(item['id'], {})
        merged[item['id']] = {**current, **item, 'raw_video_present': current.get('raw_video_present')}
    progress = {item.get('session_id'): item for item in transfers()}
    for item in merged.values():
        item['transfer'] = progress.get(item['id'])
    return sorted(merged.values(), key=lambda item: item['id'], reverse=True)[:50]


def status_payload():
    try:
        halo = halo_command('status')
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        halo = {'online': False, 'stage': 'offline', 'error': str(exc)[:200],
                'readiness': {}, 'sessions': []}
    return {'halo': halo, 'transfers': transfers(),
            'sense_time_utc': datetime.now(timezone.utc).isoformat()}


def sessions_payload():
    try:
        halo = halo_command('sessions').get('sessions', [])
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        halo = []
    return {'sessions': merged_sessions(halo)}


def reconcile_once():
    for item in sense_sessions():
        session_id = item['id']
        digest = item['receipt'].get('archive_sha256', '')
        if (session_id, digest) in RECONCILED:
            continue
        path = DATA / 'incoming' / session_id
        valid, _ = verify_session_structure(path)
        if not valid:
            continue
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            continue
        try:
            manifest_hash = hashlib.sha256((path / 'SHA256SUMS').read_bytes()).hexdigest()
            halo_command('cleanup', session_id, digest, manifest_hash)
            RECONCILED.add((session_id, digest))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            pass


def reconcile_loop():
    while True:
        try:
            reconcile_once()
        except Exception:
            pass
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def internal_authorized(self):
        try:
            expected = INTERNAL_TOKEN_FILE.read_text().strip()
        except OSError:
            expected = ''
        provided = self.headers.get('X-SENSE-Internal-Token', '')
        if not expected or not hmac.compare_digest(expected.encode('utf-8'), provided.encode('utf-8')):
            self.respond(403, {'error': 'Internal access denied'})
            return False
        return True

    def respond(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/internal/status', '/internal/sessions'):
            if not self.internal_authorized():
                return
            self.respond(200, status_payload() if path.endswith('status') else sessions_payload())
            return
        self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path in ('/internal/recordings/start', '/internal/recordings/stop', '/internal/poweroff'):
            if not self.internal_authorized():
                return
            verb = path.rsplit('/', 1)[-1]
            try:
                result = halo_command(verb)
                self.respond(409 if result.get('error') else 200, result)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                self.respond(503, {'error': str(exc)[:200]})
            return
        self.respond(404, {'error': 'Not found'})


def main():
    if not HALO:
        raise SystemExit("Set HALO_SSH_TARGET before starting the dashboard service.")
    threading.Thread(target=reconcile_loop, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__':
    main()
