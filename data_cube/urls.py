from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # --- TAB NAVIGATION ---
    # The empty path renders the environmental survey directly
    path('', views.surveys_tab, name='surveys_tab'),
    path('survey/done/', views.surveys_tab_done, name='surveys_tab_done'),
    path('map/', views.map_tab, name='map_tab'),
    path('dashboard/', views.dashboard_tab, name='dashboard_tab'),
    path('api/toggle-collect-mode/', views.toggle_collect_mode, name='toggle_collect_mode'),
    path('data-browser/', views.data_browser, name='data_browser'),
    path('export-gpkg/', views.export_gpkg, name='export_gpkg'),
    path('export-survey-json/', views.export_survey_json, name='export_survey_json'),

    # --- AUTH & LANDING ---
    # Custom login view redirects admin -> admin_home, non-admin -> surveys_tab
    path('login/', views.RoleLoginView.as_view(), name='admin_login'),
    path('logout/', auth_views.LogoutView.as_view(), name='admin_logout'),
    path('admin-home/', views.admin_home, name='admin_home'),
    path('create-user/', views.create_user, name='create_user'),
    path('sign-up/', views.sign_up, name='sign_up'),

    # --- API ENDPOINTS ---
    path('api/latest-sensors/', views.api_latest_sensors, name='api_latest_sensors'),
    path('api/update-gnss/', views.api_update_gnss, name='api_update_gnss'),
]