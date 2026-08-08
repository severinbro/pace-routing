from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.contrib.auth.models import User


## ----- Sensor Tables -----

# 1. GNSS Data Table (sourced from the connected smartphone's browser)
class GNSSMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    latitude = models.DecimalField(max_digits=12, decimal_places=9)
    longitude = models.DecimalField(max_digits=12, decimal_places=9)
    altitude = models.FloatField(null=True)
    satellites = models.IntegerField(default=0)
    accuracy = models.FloatField(default=0.0, help_text="Position accuracy in meters (from phone GPS)")
    
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

# 6. Noise Level (USB Microphone)
class NoiseMeasurement(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    noise_db = models.FloatField(default=0.0)


## ----- Survey Tables -----

# 1. Environment Survey Form (7-point Likert-Scales, 3 phases)
class EnvironmentSurvey(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Phase 1: Environmental features (7 questions)
    q1 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Noise
    q2 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Air Quality
    q3 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Air Temperature
    q4 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Aesthetics
    q5 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Diversity
    q6 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Urban Design
    q7 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Accessibility

    # Phase 2: Personal perception (3 questions)
    q8 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Safety
    q9 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])   # Enjoyment
    q10 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])  # Stress

    # Phase 3: Overall comfort (1 question)
    q11 = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(7)])  # Comfort

    # Foreign keys to link a survey to the exact sensor state at that moment
    gnss_snapshot = models.OneToOneField(GNSSMeasurement, on_delete=models.CASCADE, null=True)