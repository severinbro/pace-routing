import time
import sys
import json
import redis
import numpy as np
import sounddevice as sd
from smbus2 import SMBus

# PiicoDev Modules
from PiicoDev_BME280 import PiicoDev_BME280
from PiicoDev_ENS160 import PiicoDev_ENS160
from PiicoDev_LIS3DH import PiicoDev_LIS3DH
from PiicoDev_Unified import sleep_ms

# --- Initialization ---
print("----- Initialization -----")

# 1. Setup Redis Connection
try:
    r = redis.Redis(host='redis', port=6379, db=0)
    print("Connected to Redis.")
except Exception as e:
    print(f"Redis Connection Error: {e}")
    sys.exit(1)

# 2. Init Bus 1 for PiicoDev
try:
    # The PiicoDev library defaults to Bus 1 internally
    atmo = PiicoDev_BME280()
    air = PiicoDev_ENS160()
    motion = PiicoDev_LIS3DH()
    motion.range = 2 
    print("I2C Bus 1 (PiicoDev Sensors) Initialized.")
except Exception as e:
    print(f"I2C Bus 1 Error: {e}")
    
# 3. Init Bus 2 for PMSA (GNSS now sourced from the smartphone via Redis)
PMSA_ADDR = 0x12

try:
    heavy_bus = SMBus(2) 
    print("I2C Bus 2 (PMSA) Initialized.")
except Exception as e:
    print(f"I2C Bus 2 Error: {e}")

# --- Global variables ---
# GNSS is now provided by the connected smartphone's browser, pushed to Redis
# under 'gnss_phone' by the /api/update-gnss/ endpoint. The SAM-M10Q hardware
# sensor was unreliable in the field and has been retired.
gps_data = { "lat": 0.0, "lon": 0.0, "alt": 0.0, "sats": 0, "accuracy": 0.0 }
GNSS_STALE_SECONDS = 30  # If no phone fix in 30s, consider GNSS lost

# --- Helper Functions ---
def get_pm_data():
    """Reads PM1.0, PM2.5, and PM10 from the PMSA003I."""
    try:
        # FIX: Change 'bus' to 'heavy_bus'
        data = heavy_bus.read_i2c_block_data(PMSA_ADDR, 0x00, 32)
        
        pm1   = (data[4] << 8) | data[5]
        pm2_5 = (data[6] << 8) | data[7]
        pm10  = (data[8] << 8) | data[9]
        
        return pm1, pm2_5, pm10
    except Exception:
        return 0, 0, 0

def get_noise_db(duration=0.5, fs=44100):
    """Captures audio and returns a relative dB value."""
    try:
        recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='float32')
        sd.wait()
        rms = np.sqrt(np.mean(recording**2))
        db = 20 * np.log10(rms / 1e-4) if rms > 1e-9 else 0
        return float(round(max(0, db), 1))
    except Exception:
        return 0.0

def update_gps_data():
    """Reads the latest GNSS fix pushed by the smartphone from Redis.

    The phone's browser calls /api/update-gnss/ with its geolocation, which
    stores the fix under the 'gnss_phone' key. If the fix is stale (older than
    GNSS_STALE_SECONDS) or missing, the last known values are retained but
    marked as stale.
    """
    global gps_data
    try:
        raw = r.get('gnss_phone')
        if not raw:
            return

        fix = json.loads(raw)
        received_at = fix.get('received_at', 0)
        age = time.time() - received_at

        if age > GNSS_STALE_SECONDS:
            print(f"Phone GNSS fix is stale ({age:.0f}s old). Keeping last known position.")
            return

        gps_data["lat"] = fix.get('lat', gps_data["lat"])
        gps_data["lon"] = fix.get('lon', gps_data["lon"])
        gps_data["alt"] = fix.get('alt', gps_data["alt"])
        gps_data["accuracy"] = fix.get('accuracy', 0.0)
        # The browser geolocation API does not expose satellite count.
        # We leave sats at 0 to indicate "not applicable / phone source".
        gps_data["sats"] = fix.get('sats', 0)

        print(f"Phone GNSS -> Lat {gps_data['lat']:.5f}, Lon {gps_data['lon']:.5f}, "
              f"Alt {gps_data['alt']}m, Acc ±{gps_data['accuracy']:.1f}m")

    except Exception as e:
        print(f"GPS read error (Redis/phone): {e}")

# --- Main Loop ---
print("ElderPace Sensor Worker Running...")

loop_count = 0

while True:
    try:
        # 1. Update GPS State
        update_gps_data()
        
        # 2. Collect Sensor Data
        tempC, presPa, humRH = atmo.values()
        preshPa = presPa / 100
        
        # Air Quality (ENS160)
        aqi_val = air.aqi.value
        eco2_val = air.eco2.value
        tvoc_val = air.tvoc 
        
        # Particulate Matter (PMSA003I)
        pm1, pm25, pm10 = get_pm_data()
        
        # Motion
        accX, accY, accZ = motion.acceleration
        angX, angY, angZ = motion.angle
        
        # Noise
        noise_db = get_noise_db()
        
        # 3. Create Snapshot
        data = {
            "lat": gps_data["lat"],
            "lon": gps_data["lon"],
            "alt": gps_data["alt"],
            "sats": gps_data["sats"],
            "accuracy": gps_data["accuracy"],
            "tempC": round(tempC, 2),
            "preshPa": round(preshPa, 2),
            "humRH": round(humRH, 2),
            "aqi": aqi_val,
            "tvoc": tvoc_val,
            "eco2": eco2_val,
            "pm1": pm1,
            "pm25": pm25,
            "pm10": pm10,
            "acc": [round(accX, 2), round(accY, 2), round(accZ, 2)],
            "ang": [round(angX, 0), round(angY, 0), round(angZ, 0)],
            "noise": noise_db
        }
        
        # 4. Push to Redis
        payload = json.dumps(data)
        
        # Overwrites the key with the newest data every second (for live dashboards)
        r.set('sensor_measurements', payload)
        
        # Only push to the database queue every 10 loops (10 seconds)
        if loop_count % 5 == 0:
            r.rpush('sensor_db_queue', payload)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] GPS: {gps_data['lat']:.5f}, {gps_data['lon']:.5f}, Acc ±{gps_data['accuracy']:.1f}m | Temp: {tempC:.1f}°C | Humidity: {humRH:.1f}% | AQI: {aqi_val} | eCO2: {eco2_val}ppm | TVOC: {tvoc_val}ppb | PM2.5: {pm25}µg/m³ | Noise: {noise_db}dB")
        else:
            print("...")
            
    except Exception as e:
        print(f"Loop error: {e}")
    
    # Increment the loop counter and wait for the next cycle
    loop_count += 1
    time.sleep(1)