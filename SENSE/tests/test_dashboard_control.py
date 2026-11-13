"""Software checks for dashboard authentication and HALO controller states."""
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'receiver'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load('dashboard_server', ROOT / 'dashboard/server.py')
controller = load('halo_controller', ROOT.parent / 'HALO/control/controller.py')


class DashboardControlTests(unittest.TestCase):
    def test_signed_login_session_and_csrf(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / 'auth.json'
            salt = secrets.token_bytes(16)
            import hashlib
            auth.write_text(json.dumps({'salt': salt.hex(),
                'password_hash': hashlib.pbkdf2_hmac('sha256', b'password', salt, 300000).hex(),
                'secret': secrets.token_bytes(32).hex()}))
            with patch.object(dashboard, 'AUTH', auth):
                self.assertTrue(dashboard.password_valid('password'))
                self.assertFalse(dashboard.password_valid('wrong'))
                value = dashboard.signed_session('a' * 32, int(time.time() + 60))
                self.assertEqual(dashboard.parse_session(value), 'a' * 32)
                self.assertIsNone(dashboard.parse_session(value[:-1] + '0'))
                self.assertIsNone(dashboard.parse_session(dashboard.signed_session('a' * 32, 1)))
                self.assertNotEqual(dashboard.csrf_token('a' * 32), dashboard.csrf_token('b' * 32))

    def test_stale_recording_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.json'
            state.write_text('{"stage":"recording","session_id":"old"}')
            with patch.object(controller, 'STATE', state), patch.object(controller, 'active', return_value=False), patch.object(controller, 'sessions', return_value=[]), patch.object(controller, 'readiness', return_value={'ready': False, 'checks': {}}):
                result = controller.status()
            self.assertFalse(result['recording_active'])
            self.assertEqual(result['stage'], 'failed')

    def test_start_rejects_missing_sensors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(controller, 'DATA', Path(tmp)), patch.object(controller, 'active', return_value=False), patch.object(controller, 'readiness', return_value={'ready': False, 'checks': {'stereo_cameras': {'ok': False}}}), patch.object(controller, 'status', return_value={'stage': 'idle'}):
                result = controller.start()
            self.assertEqual(result['error'], 'HALO is not ready')
            self.assertFalse((Path(tmp) / '.ui_stop').exists())
