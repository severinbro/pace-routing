"""Integration checks for the SENSE/HALO admin split."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


class PlatformNavigationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('operator', password='test-password', is_staff=True)
        self.participant = User.objects.create_user('participant', password='test-password')

    def test_staff_can_open_both_sections(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_home')), 'SENSE')
        self.assertContains(self.client.get(reverse('admin_home')), 'HALO')
        self.assertContains(self.client.get(reverse('sense_home')), 'sensor worker')
        self.assertContains(self.client.get(reverse('halo_home')), 'Recording control')

    def test_participant_cannot_open_halo(self):
        self.client.force_login(self.participant)
        response = self.client.get(reverse('halo_home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

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
