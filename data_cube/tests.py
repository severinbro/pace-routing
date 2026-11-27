"""Integration checks for the SENSE/HALO admin split."""
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
import zipfile

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse


class PlatformNavigationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('operator', password='test-password', is_staff=True)
        self.participant = User.objects.create_user('participant', password='test-password')

    def test_staff_can_open_both_sections(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(reverse('admin_home')), reverse('about'))
        about_resp = self.client.get(reverse('about'))
        self.assertContains(about_resp, 'SENSE')
        self.assertContains(about_resp, 'HALO')
        self.assertContains(about_resp, 'id="theme-toggle"')
        self.assertContains(about_resp, reverse('campaign_hub'))
        self.assertContains(about_resp, reverse('map_tab'))
        sense_ctrl = self.client.get(reverse('sense_home'))
        self.assertEqual(sense_ctrl.status_code, 200)
        self.assertContains(sense_ctrl, 'SENSE Controller')
        self.assertContains(sense_ctrl, 'Recording control')
        sys_resp = self.client.get(reverse('sense_system_status'))
        self.assertEqual(sys_resp.status_code, 200)
        self.assertIn('system', sys_resp.json())
        self.assertIn('mem_percent', sys_resp.json()['system'])
        sense_resp = self.client.get(reverse('dashboard_tab'))
        self.assertContains(sense_resp, 'sensor worker')
        self.assertContains(sense_resp, 'SENSE Controller')
        self.assertContains(sense_resp, reverse('dashboard_tab'))
        self.assertContains(sense_resp, reverse('data_browser'))
        self.assertNotContains(sense_resp, reverse('create_user'))
        self.assertContains(sense_resp, 'title="Participant Survey"')
        halo_resp = self.client.get(reverse('halo_home'))
        self.assertContains(halo_resp, 'Recording control')
        self.assertContains(halo_resp, 'HALO Controller')
        halo_dash = self.client.get(reverse('halo_dashboard'))
        self.assertContains(halo_dash, 'HALO Controller')
        self.assertContains(halo_dash, 'Sensor Dashboard')
        self.assertContains(halo_dash, 'Data &amp; Export')
        self.assertContains(self.client.get(reverse('campaign_hub')), reverse('create_user'))

    def test_participant_cannot_open_halo(self):
        self.client.force_login(self.participant)
        for route in ('halo_home', 'halo_dashboard', 'halo_data'):
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response['Location'])
        about_resp = self.client.get(reverse('about'))
        self.assertNotContains(about_resp, reverse('campaign_hub'))
        self.assertContains(about_resp, reverse('surveys_tab'))
        self.assertContains(about_resp, reverse('map_tab'))
        map_resp = self.client.get(reverse('map_tab'))
        self.assertEqual(map_resp.status_code, 200)
        self.assertNotContains(map_resp, 'btn-edit-stops')
        self.assertEqual(self.client.get(reverse('api_map_data')).status_code, 200)
        self.assertEqual(self.client.get(reverse('offline_tile', args=[14, 0, 0])).status_code, 204)
        stop_resp = self.client.post(reverse('campaign_stops'), data=json.dumps({'stops': []}), content_type='application/json')
        self.assertEqual(stop_resp.status_code, 302)
        self.assertIn('/login/', stop_resp['Location'])

    def test_halo_data_browser_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = '20261126T120000.000Z-halo-1'
            sdir = root / 'incoming' / sid
            (sdir / 'frames' / 'left').mkdir(parents=True)
            (sdir / 'frames' / 'left' / 'frame_000000.jpg').write_bytes(b'\xff\xd8\xff\xd9')
            (sdir / 'capture.json').write_text(json.dumps({'started_at_utc': '2026-11-26T12:00:00Z', 'clip_duration_seconds': 10}))
            (sdir / 'sync_report.json').write_text(json.dumps({'pairs': 5, 'pairs_exceeding_stereo_skew_limit': 0, 'pairs_without_close_imu_bracket': 0}))
            (sdir / 'pairs.csv').write_text('pair_index,left_frame\n0,frame_000000.jpg\n')
            with override_settings(SENSE_DATA_DIR=root):
                self.client.force_login(self.staff)
                page = self.client.get(reverse('halo_data'))
                self.assertContains(page, sid)
                summary = self.client.get(reverse('halo_export_summary_csv'))
                self.assertEqual(summary.status_code, 200)
                self.assertIn(sid, summary.content.decode())
                artifact = self.client.get(reverse('halo_export_artifact', args=[sid, 'pairs.csv']))
                self.assertEqual(artifact.status_code, 200)
                artifact_bytes = b''.join(artifact.streaming_content)
                self.assertIn(b'pair_index', artifact_bytes)
                bad_artifact = self.client.get(reverse('halo_export_artifact', args=[sid, 'secret.txt']))
                self.assertEqual(bad_artifact.status_code, 400)
                missing = self.client.get(reverse('halo_export_artifact', args=['missing-session', 'pairs.csv']))
                self.assertEqual(missing.status_code, 404)
                zip_resp = self.client.get(reverse('halo_export_zip', args=[sid]))
                self.assertEqual(zip_resp.status_code, 200)
                zip_bytes = b''.join(zip_resp.streaming_content)
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    names = zf.namelist()
                    self.assertIn(f'{sid}/pairs.csv', names)
                    self.assertNotIn(f'{sid}/frames/left/frame_000000.jpg', names)

    def test_halo_actions_require_post_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        client.get(reverse('halo_home'))
        self.assertEqual(client.get(reverse('halo_start')).status_code, 405)
        with patch('data_cube.halo_views.control_request', return_value=(409, {'error': 'HALO is not ready'})):
            self.assertEqual(client.post(reverse('halo_start')).status_code, 403)
            token = client.cookies['csrftoken'].value
            response = client.post(reverse('halo_start'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'HALO is not ready')

    def test_poweroff_actions_require_post_csrf_and_staff(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        client.get(reverse('campaign_hub'))
        token = client.cookies['csrftoken'].value
        self.assertEqual(client.get(reverse('halo_poweroff')).status_code, 405)
        self.assertEqual(client.get(reverse('campaign_poweroff')).status_code, 405)
        with patch('data_cube.halo_views.control_request', return_value=(200, {'shutting_down': True})) as mock_ctrl:
            self.assertEqual(client.post(reverse('halo_poweroff')).status_code, 403)
            response = client.post(reverse('halo_poweroff'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['shutting_down'])
        mock_ctrl.assert_called_once_with('poweroff', 'POST')
        with patch('data_cube.views._schedule_host_poweroff', return_value={'shutting_down': True, 'method': 'systemd-logind'}):
            self.assertEqual(client.post(reverse('campaign_poweroff')).status_code, 403)
            response = client.post(reverse('campaign_poweroff'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['shutting_down'])

    def test_old_login_limits_failed_attempts(self):
        from data_cube.views import r
        address = '198.51.100.42'
        key = 'login:failures:' + address
        r.delete(key)
        try:
            for _ in range(5):
                self.assertEqual(self.client.post(reverse('admin_login'),
                    {'username': 'operator', 'password': 'wrong'}, HTTP_X_REAL_IP=address).status_code, 200)
            self.assertEqual(self.client.post(reverse('admin_login'),
                {'username': 'operator', 'password': 'test-password'}, HTTP_X_REAL_IP=address).status_code, 429)
        finally:
            r.delete(key)

    def test_offline_mbtiles_serving(self):
        png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
        with tempfile.TemporaryDirectory() as tmp:
            mbtiles = Path(tmp) / 'offline.mbtiles'
            z, x, y = 13, 4283, 2776
            tms_y = (1 << z) - 1 - y
            with sqlite3.connect(str(mbtiles)) as conn:
                conn.execute('CREATE TABLE metadata (name TEXT, value TEXT)')
                conn.execute('INSERT INTO metadata VALUES (?, ?)', ('name', 'Test Mainz'))
                conn.execute('INSERT INTO metadata VALUES (?, ?)', ('center', '8.2473,49.9929,14'))
                conn.execute('CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)')
                conn.execute('INSERT INTO tiles VALUES (?, ?, ?, ?)', (z, x, tms_y, sqlite3.Binary(png_bytes)))
                conn.commit()
            with override_settings(OFFLINE_MBTILES_PATH=mbtiles):
                self.client.force_login(self.staff)
                map_resp = self.client.get(reverse('map_tab'))
                self.assertEqual(map_resp.status_code, 200)
                self.assertContains(map_resp, '/tiles/{z}/{x}/{y}.png')
                self.assertContains(map_resp, 'Test Mainz')
                tile_resp = self.client.get(reverse('offline_tile', args=[z, x, y]))
                self.assertEqual(tile_resp.status_code, 200)
                self.assertEqual(tile_resp['Content-Type'], 'image/png')
                self.assertEqual(tile_resp.content, png_bytes)
                missing_resp = self.client.get(reverse('offline_tile', args=[z, x + 1, y]))
                self.assertEqual(missing_resp.status_code, 204)

    def test_create_participant_shows_errors_and_success(self):
        self.client.force_login(self.staff)
        bad = self.client.post(reverse('create_user'), {
            'username': 'walker01',
            'password1': '1234',
            'password2': '1234',
        })
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'too short')
        good = self.client.post(reverse('create_user'), {
            'username': 'walker01',
            'password1': 'FieldWalk2026!',
            'password2': 'FieldWalk2026!',
        }, follow=True)
        self.assertEqual(good.status_code, 200)
        self.assertContains(good, 'walker01')
        self.assertContains(good, 'was created')
        created = User.objects.get(username='walker01')
        self.assertFalse(created.is_staff)

    def test_campaign_stops_and_map_data(self):
        from data_cube.views import r
        keys = (
            'campaign:planned_stops', 'campaign:recorded_stops',
            'campaign:active', 'campaign:total_stops', 'campaign:current_stop',
            'campaign:collect_mode', 'campaign:submitted_stops', 'collect_mode',
        )
        orig = {k: r.get(k) for k in keys}
        try:
            self.client.force_login(self.staff)
            save_resp = self.client.post(
                reverse('campaign_stops'),
                data=json.dumps({
                    'stops': [
                        {'lat': 49.9929, 'lon': 8.2473, 'label': 'Start Dom'},
                        {'lat': 49.9985, 'lon': 8.2711, 'label': 'Rheinpromenade'},
                    ]
                }),
                content_type='application/json',
            )
            self.assertEqual(save_resp.status_code, 200)
            self.assertEqual(len(save_resp.json()['stops']), 2)
            self.assertEqual(self.client.post(reverse('campaign_start'), {'total_stops': '2'}).status_code, 200)
            self.assertEqual(self.client.post(reverse('campaign_unlock_stop')).status_code, 200)

            self.client.force_login(self.participant)
            map_data = self.client.get(reverse('api_map_data'))
            self.assertEqual(map_data.status_code, 200)
            payload = map_data.json()
            self.assertTrue(payload['campaign']['active'])
            self.assertEqual(payload['campaign']['current_stop'], 1)
            self.assertEqual(len(payload['stops']), 2)
            self.assertEqual(payload['stops'][0]['status'], 'current')
            self.assertEqual(payload['stops'][0]['label'], 'Start Dom')
            self.assertEqual(payload['stops'][1]['status'], 'upcoming')
        finally:
            for k, val in orig.items():
                if val is None:
                    r.delete(k)
                else:
                    r.set(k, val)
