from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # --- TAB NAVIGATION ---
    # The empty path '' makes the Surveys Lobby the landing page for pace-routing.pi
    path('', views.surveys_tab, name='surveys_tab'),               
    path('map/', views.map_tab, name='map_tab'),
    path('dashboard/', views.dashboard_tab, name='dashboard_tab'),

    # --- SURVEY SUB-PAGES ---
    path('surveys/environment/', views.survey_environment, name='survey_environment'),
    path('surveys/priority/', views.survey_priority, name='survey_priority'),

    # --- ADMIN AUTH & LANDING ---
    path('login/', auth_views.LoginView.as_view(template_name='data_cube/login.html'), name='admin_login'),
    path('logout/', auth_views.LogoutView.as_view(), name='admin_logout'),
    path('admin-home/', views.admin_home, name='admin_home'),

    # --- API ENDPOINTS ---
    path('api/latest-sensors/', views.api_latest_sensors, name='api_latest_sensors'),
]