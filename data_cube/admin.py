import os

from django.contrib import admin

from .models import (
    GNSSMeasurement,
    AtmosphericMeasurement,
    AccelerometerMeasurement,
    AirQualityMeasurement,
    ParticulateMeasurement,
    NoiseMeasurement,
    EnvironmentSurvey,
)


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
