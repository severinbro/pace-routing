import json
import redis
from django.core.management.base import BaseCommand
from data_cube.models import (
    GNSSPhoneMeasurement,
    GNSSSensorMeasurement,
    AtmosphericMeasurement, 
    AccelerometerMeasurement, 
    AirQualityMeasurement,
    ParticulateMeasurement, 
    NoiseMeasurement        
)

class Command(BaseCommand):
    help = 'Listens to Redis queue and saves sensor data to separated PostgreSQL tables'

    def handle(self, *args, **options):
        r = redis.Redis(host='redis', port=6379, db=0)
        self.stdout.write(self.style.SUCCESS('Started listening to sensor_db_queue...'))

        while True:
            try:
                queue_name, message = r.blpop('sensor_db_queue')
                # Only persist sensor data when collection mode is active.
                # The data_browser toggle controls this via the "collect_mode" key.
                if r.get('collect_mode') != b'1':
                    continue

                data = json.loads(message.decode('utf-8'))
                
                # 1a. Save Phone GNSS
                if data.get('lat') is not None and data.get('lon') is not None:
                    GNSSPhoneMeasurement.objects.create(
                        latitude=data.get('lat'),
                        longitude=data.get('lon'),
                        altitude=data.get('alt'),
                        satellites=data.get('sats'),
                        accuracy=data.get('accuracy')
                    )

                # 1b. Save Sensor GNSS (SAM-M10Q)
                if data.get('sensor_lat') is not None and data.get('sensor_lon') is not None:
                    GNSSSensorMeasurement.objects.create(
                        latitude=data.get('sensor_lat'),
                        longitude=data.get('sensor_lon'),
                        altitude=data.get('sensor_alt'),
                        satellites=data.get('sensor_sats')
                    )

                # 2. Save Atmosphere
                AtmosphericMeasurement.objects.create(
                    temperature=data.get('tempC'),
                    pressure=data.get('preshPa'),
                    humidity=data.get('humRH')
                )

                # 3. Save Accelerometer (Unpacking the arrays)
                acc = data.get('acc', [0, 0, 0])
                ang = data.get('ang', [0, 0, 0])
                AccelerometerMeasurement.objects.create(
                    accX=acc[0], accY=acc[1], accZ=acc[2],
                    angleX=ang[0], angleY=ang[1], angleZ=ang[2]
                )

                # 4. Save Air Quality
                AirQualityMeasurement.objects.create(
                    aqi=data.get('aqi'),
                    tvoc=data.get('tvoc'),
                    eco2=data.get('eco2')
                )
                
                # 5. Save Particulate & Noise (If you added the models)
                ParticulateMeasurement.objects.create(
                    pm1=data.get('pm1'), pm25=data.get('pm25'), pm10=data.get('pm10')
                )
                NoiseMeasurement.objects.create(noise_db=data.get('noise'))
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error saving data: {e}"))