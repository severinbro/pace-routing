"""Integration checks for the SENSE/HALO admin split."""
import io
import json
from pathlib import Path
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
        self.assertRedirects(self.client.get(reverse('sense_home')), reverse('dashboard_tab'))
        sense_resp = self.client.get(reverse('dashboard_tab'))
        self.assertContains(sense_resp, 'sensor worker')
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
