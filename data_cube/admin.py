import os

from django.contrib import admin
from django.urls import path
from django.http import FileResponse

from .models import (
    GNSSMeasurement,
    AtmosphericMeasurement,
    AccelerometerMeasurement,
    AirQualityMeasurement,
    ParticulateMeasurement,
    NoiseMeasurement,
    EnvironmentSurvey,
)
from .exports import write_sensor_gpkg, export_filename


## ----- Sensor Tables -----

@admin.register(GNSSMeasurement)
class GNSSMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'latitude', 'longitude', 'altitude', 'satellites', 'accuracy')
    ordering = ('-id',)


@admin.register(AtmosphericMeasurement)
class AtmosphericMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'temperature', 'humidity', 'pressure')
    ordering = ('-id',)


@admin.register(AccelerometerMeasurement)
class AccelerometerMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'accX', 'accY', 'accZ', 'angleX', 'angleY', 'angleZ')
    ordering = ('-id',)


@admin.register(AirQualityMeasurement)
class AirQualityMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'aqi', 'tvoc', 'eco2')
    ordering = ('-id',)


@admin.register(ParticulateMeasurement)
class ParticulateMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'pm1', 'pm25', 'pm10')
    ordering = ('-id',)


@admin.register(NoiseMeasurement)
class NoiseMeasurementAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'noise_db')
    ordering = ('-id',)


## ----- Survey Tables -----

@admin.register(EnvironmentSurvey)
class EnvironmentSurveyAdmin(admin.ModelAdmin):
    list_display = ('id', 'timestamp', 'user', 'q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7', 'q8', 'q9', 'q10', 'q11', 'gnss_snapshot')
    list_filter = ('user',)
    ordering = ('-id',)


## ----- Custom "Export GPKG" view, hooked into the admin site -----

def export_gpkg_view(request):
    """Joins the 6 sensor tables on id and streams the result as a .gpkg file."""
    path = write_sensor_gpkg()
    try:
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=export_filename(),
            content_type='application/geopackage+sqlite3',
        )
        response._resource_closers.append(lambda: os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise


_original_get_urls = admin.site.get_urls


def _get_urls():
    custom_urls = [
        path(
            'export-gpkg/',
            admin.site.admin_view(export_gpkg_view),
            name='export_gpkg',
        ),
    ]
    return custom_urls + _original_get_urls()


admin.site.get_urls = _get_urls
