#!/usr/bin/env python3
"""Local SENSE control dashboard, using only Python's standard library."""
from datetime import datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from verify import verify_session_structure

DATA = Path(os.environ.get('SENSE_DATA_ROOT', '/data'))
AUTH = Path(os.environ.get('DASHBOARD_AUTH_FILE', '/config/dashboard-auth.json'))
SSH_KEY = '/config/halo-dashboard-key'
KNOWN_HOSTS = '/config/known_hosts'
INTERNAL_TOKEN_FILE = Path(os.environ.get('DASHBOARD_INTERNAL_TOKEN_FILE', '/config/dashboard-internal.token'))
HALO = 'Pi_Mz@10.42.10.2'
SESSION_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
FAILURES = {}
FAIL_LOCK = threading.Lock()
RECONCILED = set()


def credentials():
    return json.loads(AUTH.read_text())


def password_valid(password):
    data = credentials()
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(data['salt']), 300000)
    return hmac.compare_digest(actual.hex(), data['password_hash'])


def signed_session(session_id, expires):
    key = bytes.fromhex(credentials()['secret'])
    message = f'{session_id}.{expires}'
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    return f'{message}.{signature}'


def parse_session(value):
    try:
        session_id, expires, signature = value.split('.')
        if int(expires) < time.time() or not re.fullmatch(r'[a-f0-9]{32}', session_id):
            return None
        if not hmac.compare_digest(signed_session(session_id, expires), value):
            return None
        return session_id
    except (ValueError, AttributeError):
        return None


def csrf_token(session_id):
    key = bytes.fromhex(credentials()['secret'])
    return hmac.new(key, (session_id + ':csrf').encode(), hashlib.sha256).hexdigest()


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
        if not expected or not hmac.compare_digest(expected, provided):
            self.respond(403, {'error': 'Internal access denied'})
            return False
        return True

    def respond(self, status, data, cookie=None):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(payload)

    def session(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        item = cookies.get('sense_session')
        return parse_session(item.value) if item else None

    def require_session(self):
        session_id = self.session()
        if session_id is None:
            self.respond(HTTPStatus.UNAUTHORIZED, {'error': 'Login required'})
        return session_id

    def body(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size < 1 or size > 4096:
            raise ValueError('Invalid request size')
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/internal/status', '/internal/sessions'):
            if not self.internal_authorized():
                return
            self.respond(200, status_payload() if path.endswith('status') else sessions_payload())
            return
        if path in ('/', '/ui', '/ui/'):
            page = Path('/app/dashboard/index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(page)
            return
        session_id = self.require_session()
        if not session_id:
            return
        if path == '/api/auth':
            self.respond(200, {'csrf': csrf_token(session_id)})
        elif path == '/api/status':
            self.respond(200, status_payload())
        elif path == '/api/sessions':
            self.respond(200, sessions_payload())
        else:
            self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path in ('/internal/recordings/start', '/internal/recordings/stop'):
            if not self.internal_authorized():
                return
            verb = path.rsplit('/', 1)[-1]
            try:
                result = halo_command(verb)
                self.respond(409 if result.get('error') else 200, result)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                self.respond(503, {'error': str(exc)[:200]})
            return
        if path == '/api/login':
            client = self.headers.get('X-Real-IP', self.client_address[0])
            with FAIL_LOCK:
                recent = [x for x in FAILURES.get(client, []) if time.time() - x < 600]
                FAILURES[client] = recent
                if len(recent) >= 5:
                    self.respond(429, {'error': 'Too many attempts; try again in 10 minutes'})
                    return
            try:
                password = self.body().get('password', '')
            except (ValueError, TypeError):
                self.respond(400, {'error': 'Invalid request'})
                return
            if not isinstance(password, str) or not password_valid(password):
                with FAIL_LOCK:
                    FAILURES.setdefault(client, []).append(time.time())
                self.respond(401, {'error': 'Incorrect password'})
                return
            with FAIL_LOCK:
                FAILURES.pop(client, None)
            session_id = secrets.token_hex(16)
            cookie = 'sense_session=' + signed_session(session_id, int(time.time() + 12 * 3600)) + '; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=43200'
            self.respond(200, {'csrf': csrf_token(session_id)}, cookie)
            return
        session_id = self.require_session()
        if not session_id:
            return
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf_token(session_id)):
            self.respond(403, {'error': 'Invalid CSRF token'})
            return
        if path == '/api/logout':
            self.respond(200, {'ok': True}, 'sense_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')
            return
        if path not in ('/api/recordings/start', '/api/recordings/stop'):
            self.respond(404, {'error': 'Not found'})
            return
        verb = 'start' if path.endswith('/start') else 'stop'
        try:
            result = halo_command(verb)
            self.respond(409 if result.get('error') else 200, result)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self.respond(503, {'error': str(exc)[:200]})


def main():
    credentials()
    threading.Thread(target=reconcile_loop, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__':
    main()
