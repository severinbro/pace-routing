from django.urls import path
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from . import views
from . import halo_views

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/img/favicon.png', permanent=False)),
    # --- TAB NAVIGATION ---
    # The empty path renders the environmental survey directly
    path('', views.surveys_tab, name='surveys_tab'),
    path('survey/done/', views.surveys_tab_done, name='surveys_tab_done'),
    path('map/', views.map_tab, name='map_tab'),
    path('tiles/<int:z>/<int:x>/<int:y>.png', views.offline_tile, name='offline_tile'),
    path('api/map-data/', views.api_map_data, name='api_map_data'),
    path('campaign/stops/', views.campaign_stops, name='campaign_stops'),
    path('dashboard/', views.dashboard_tab, name='dashboard_tab'),
    path('api/toggle-collect-mode/', views.toggle_collect_mode, name='toggle_collect_mode'),
    path('data-browser/', views.data_browser, name='data_browser'),
    path('export-csv/', views.export_csv, name='export_csv'),
    path('export-survey-json/', views.export_survey_json, name='export_survey_json'),
    path('export-weather-csv/', views.export_weather_csv, name='export_weather_csv'),
    path('export-amenities-csv/', views.export_amenities_csv, name='export_amenities_csv'),
    path('about/', views.about, name='about'),

    # --- CAMPAIGN MANAGEMENT (admin only) ---
    path('campaign/', views.campaign_hub, name='campaign_hub'),
    path('campaign/start/', views.campaign_start, name='campaign_start'),
    path('campaign/abort/', views.campaign_abort, name='campaign_abort'),
    path('campaign/unlock-stop/', views.campaign_unlock_stop, name='campaign_unlock_stop'),
    path('campaign/toggle-collect/', views.campaign_toggle_collect, name='campaign_toggle_collect'),
    path('campaign/status/', views.campaign_status, name='campaign_status'),
    path('campaign/poweroff/', views.campaign_poweroff, name='campaign_poweroff'),

    # --- AUTH & LANDING ---
    # Successful sign-ins land on the project info page.
    path('login/', views.RoleLoginView.as_view(), name='admin_login'),
    path('logout/', auth_views.LogoutView.as_view(), name='admin_logout'),
    path('admin-home/', views.admin_home, name='admin_home'),
    path('sense/', views.sense_home, name='sense_home'),
    path('halo/', halo_views.halo_home, name='halo_home'),
    path('halo/dashboard/', halo_views.halo_dashboard, name='halo_dashboard'),
    path('halo/data/', halo_views.halo_data, name='halo_data'),
    path('halo/data/export-summary-csv/', halo_views.halo_export_summary_csv, name='halo_export_summary_csv'),
    path('halo/data/<str:session_id>/export/zip/', halo_views.halo_export_zip, name='halo_export_zip'),
    path('halo/data/<str:session_id>/export/<str:artifact>/', halo_views.halo_export_artifact, name='halo_export_artifact'),
    path('halo/api/status/', halo_views.halo_status, name='halo_status'),
    path('halo/api/sessions/', halo_views.halo_sessions, name='halo_sessions'),
    path('halo/api/recordings/start/', halo_views.halo_start, name='halo_start'),
    path('halo/api/recordings/stop/', halo_views.halo_stop, name='halo_stop'),
    path('halo/api/poweroff/', halo_views.halo_poweroff, name='halo_poweroff'),
    path('create-user/', views.create_user, name='create_user'),
    path('sign-up/', views.sign_up, name='sign_up'),

    # --- API ENDPOINTS ---
    path('api/latest-sensors/', views.api_latest_sensors, name='api_latest_sensors'),
    path('api/sense-system/', views.sense_system_status, name='sense_system_status'),
    path('api/update-gnss/', views.api_update_gnss, name='api_update_gnss'),
]
