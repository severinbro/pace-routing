from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator


## ----- Sensor Tables -----

# 1. GNSS Data Table (SAM-Q10M)
class GNSSMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    latitude = models.DecimalField(max_digits=12, decimal_places=9)
    longitude = models.DecimalField(max_digits=12, decimal_places=9)
    altitude = models.FloatField(null=True)
    satellites = models.IntegerField(default=0)
    
    @property
    def date_display(self):
        return self.timestamp.strftime('%d-%m-%Y')

# 2. Atmospheric Data Table (BME280)
class AtmosphericMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    temperature = models.FloatField()
    humidity = models.FloatField()
    pressure = models.FloatField()

# 3. Accelerometer Data Table (LIS3DH)
class AccelerometerMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    accX = models.FloatField()
    accY = models.FloatField()
    accZ = models.FloatField()
    angleX = models.FloatField()
    angleY = models.FloatField()
    angleZ = models.FloatField()

# 4. Air Quality Data Table (ENS160)
class AirQualityMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    aqi = models.IntegerField()
    tvoc = models.IntegerField()
    eco2 = models.IntegerField()

# 5. Particulate Matter (PMSA003I)
class ParticulateMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    pm1 = models.IntegerField(default=0)
    pm25 = models.IntegerField(default=0)
    pm10 = models.IntegerField(default=0)

# 6. Noise Level
class NoiseMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    noise_db = models.FloatField(default=0.0)


## ----- Survey Tables -----

# 1. Environment Survey Form (Likert-Scales)
class EnvironmentSurvey(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # Questions 1-10
    q1 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q2 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q3 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q4 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q5 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q6 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q7 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q8 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q9 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    q10 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])

    # Foreign keys to link a survey to the exact sensor state at that moment
    gnss_snapshot = models.OneToOneField(GNSSMeasurement, on_delete=models.CASCADE, null=True)

# 2. Relative Importance Survey Form (AHP, Pairwise Comparison)
class RelativeImportanceSurvey(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # Questions (Continuous Slider: 0.0 (Feature A) to 1.0 (Feature B). 0.5 is Neutral)
    q1 = models.FloatField(default=0.5)
    q2 = models.FloatField(default=0.5)
    q3 = models.FloatField(default=0.5)
    q4 = models.FloatField(default=0.5)
    q5 = models.FloatField(default=0.5)
    q6 = models.FloatField(default=0.5)
    q7 = models.FloatField(default=0.5)
    q8 = models.FloatField(default=0.5)
    q9 = models.FloatField(default=0.5)
    q10 = models.FloatField(default=0.5)

    # Foreign keys to link a survey to the exact sensor state at that moment
    gnss_snapshot = models.OneToOneField(GNSSMeasurement, on_delete=models.CASCADE, null=True)