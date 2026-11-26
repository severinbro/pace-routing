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

    def test_poweroff_rejects_active_recording_and_schedules_when_idle(self):
        with patch.object(controller, 'active', return_value=True):
            result = controller.poweroff()
        self.assertIn('error', result)
        fake_proc = type('Proc', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        with patch.object(controller, 'active', return_value=False), patch.object(controller, 'run', return_value=fake_proc) as mock_run:
            result = controller.poweroff()
        self.assertEqual(result, {'shutting_down': True, 'delay_seconds': 2})
        mock_run.assert_called_once_with(
            'sudo', '-n', '/usr/bin/systemd-run', '--unit=halo-ui-poweroff',
            '--on-active=2s', '/usr/bin/systemctl', 'poweroff'
        )

    def test_telemetry_reads_latest_imu_light_and_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            sdir = data / 'sessions' / '20261126T120000.000Z-halo-1'
            sdir.mkdir(parents=True)
            (sdir / 'imu.csv').write_text(
                'utc_ns,accel_x_m_s2,accel_y_m_s2,accel_z_m_s2,gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s,quat_i,quat_j,quat_k,quat_real\n'
                '1000,0.1,0.2,9.8,0.0,0.0,0.0,0.0,0.0,0.0,1.0\n'
            )
            (sdir / 'light.csv').write_text('utc_ns,lux\n1000,123.5\n')
            outbox = data / 'live_outbox' / '20261126T120000.000Z-halo-1'
            outbox.mkdir(parents=True)
            (outbox / 'frame_000000.meta.json').write_text('{}')
            with patch.object(controller, 'DATA', data), patch.object(controller, 'SESSIONS', data / 'sessions'), patch.object(controller, 'active', return_value=False):
                tel = controller.telemetry()
            self.assertEqual(tel['live_outbox_frames'], 1)
            self.assertEqual(tel['light']['lux'], 123.5)
            self.assertAlmostEqual(tel['imu']['accel_z'], 9.8, places=2)
            self.assertAlmostEqual(tel['imu']['pitch_deg'], 0.0, places=2)
