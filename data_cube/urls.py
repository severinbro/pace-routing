from django.urls import path
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

    # --- API ENDPOINTS ---
    path('api/latest-sensors/', views.api_latest_sensors, name='api_latest_sensors'),
]