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

PMSA_ADDR = 0x12
GPS_ADDR = 0x42
BYTES_AVAIL_REG = 0xFD
DATA_STREAM_REG = 0xFF

# 1. Setup Redis Connection
try:
    r = redis.Redis(host='redis', port=6379, db=0)
    print("Connected to Redis.")
except Exception as e:
    print(f"Redis Connection Error: {e}")
    sys.exit(1)

# 2. Init Bus 1 for PiicoDev & SAM-M10Q GNSS (all on 3.3V)
try:
    # The PiicoDev library defaults to Bus 1 internally
    atmo = PiicoDev_BME280()
    air = PiicoDev_ENS160()
    motion = PiicoDev_LIS3DH()
    motion.range = 2 
    print("I2C Bus 1 (PiicoDev Sensors & GNSS Sensor) Initialized.")
except Exception as e:
    print(f"I2C Bus 1 Error: {e}")

try:
    gnss_bus = SMBus(1)
    print("I2C Bus 1 (SAM-M10Q GNSS) Initialized.")
except Exception as e:
    print(f"I2C Bus 1 (GNSS) Error: {e}")
    
# 3. Init Bus 2 for PMSA (5V, only the particulate matter sensor)
try:
    heavy_bus = SMBus(2) 
    print("I2C Bus 2 (PMSA) Initialized.")
except Exception as e:
    print(f"I2C Bus 2 Error: {e}")

# --- Global variables ---
# Two GNSS sources:
#   1. Phone GNSS — pushed by the admin smartphone's browser to Redis ('gnss_phone')
#   2. Sensor GNSS — read directly from the SAM-M10Q over I2C
gps_phone  = { "lat": 0.0, "lon": 0.0, "alt": 0.0, "sats": 0, "accuracy": 0.0 }
gps_sensor = { "lat": 0.0, "lon": 0.0, "alt": 0.0, "sats": 0 }
GNSS_STALE_SECONDS = 30  # If no phone fix in 30s, consider phone GNSS lost

# --- Helper Functions ---
def get_pm_data():
    """Reads PM1.0, PM2.5, and PM10 from the PMSA003I."""
    try:
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

def update_gps_phone():
    """Reads the latest GNSS fix pushed by the admin smartphone from Redis."""
    global gps_phone
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

        gps_phone["lat"] = fix.get('lat', gps_phone["lat"])
        gps_phone["lon"] = fix.get('lon', gps_phone["lon"])
        gps_phone["alt"] = fix.get('alt', gps_phone["alt"])
        gps_phone["accuracy"] = fix.get('accuracy', 0.0)
        gps_phone["sats"] = fix.get('sats', 0)

        print(f"Phone GNSS  -> Lat {gps_phone['lat']:.5f}, Lon {gps_phone['lon']:.5f}, "
              f"Alt {gps_phone['alt']}m, Acc ±{gps_phone['accuracy']:.1f}m")

    except Exception as e:
        print(f"GPS read error (Redis/phone): {e}")

def update_gps_sensor():
    """Reads GNSS data directly from the SAM-M10Q sensor over I2C Bus 2."""
    global gps_sensor
    buffer = ""
    try:
        # 1. Check how many bytes are waiting in the SAM-M10Q buffer
        waiting_bytes_list = gnss_bus.read_i2c_block_data(GPS_ADDR, BYTES_AVAIL_REG, 2)
        num_bytes = (waiting_bytes_list[0] << 8) | waiting_bytes_list[1]

        # Ignore hardware glitch readings
        if num_bytes == 0 or num_bytes == 65535 or num_bytes > 1024:
            return

        time.sleep(0.01)

        # 2. Read the bytes in chunks
        while num_bytes > 0:
            chunk_size = min(num_bytes, 16)
            data = gnss_bus.read_i2c_block_data(GPS_ADDR, DATA_STREAM_REG, chunk_size)
            for byte in data:
                if byte == 0xFF:
                    continue
                buffer += chr(byte)
            num_bytes -= chunk_size
            time.sleep(0.02)

        # 3. Parse the NMEA sentences
        if '$GNGGA' in buffer or '$GPGGA' in buffer:
            lines = buffer.split('\n')
            for line in lines:
                if (line.startswith('$GNGGA') or line.startswith('$GPGGA')):
                    parts = line.split(',')
                    if len(parts) > 9 and parts[6] != '0' and parts[6] != '':
                        try:
                            lat_raw = float(parts[2])
                            lat_deg = int(lat_raw / 100)
                            lat_min = lat_raw - (lat_deg * 100)
                            lat = lat_deg + (lat_min / 60)
                            if parts[3] == 'S': lat = -lat

                            lon_raw = float(parts[4])
                            lon_deg = int(lon_raw / 100)
                            lon_min = lon_raw - (lon_deg * 100)
                            lon = lon_deg + (lon_min / 60)
                            if parts[5] == 'W': lon = -lon

                            gps_sensor["lat"] = lat
                            gps_sensor["lon"] = lon
                            gps_sensor["alt"] = float(parts[9])
                            gps_sensor["sats"] = int(parts[7])

                            print(f"Sensor GNSS -> Lat {lat:.5f}, Lon {lon:.5f}, "
                                  f"Alt {gps_sensor['alt']}m, Sats {gps_sensor['sats']}")
                        except ValueError:
                            pass
                    else:
                        visible_sats = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0
                        gps_sensor["sats"] = visible_sats

    except Exception as e:
        print(f"GPS read error (I2C/sensor): {e}")

# --- Main Loop ---
print("Pace Sensor Worker Running...")

loop_count = 0

# Accumulators for 5-second averaging (noise & particulate are high-entropy)
pm_samples = {"pm1": [], "pm25": [], "pm10": []}
noise_samples = []

while True:
    try:
        # 1. Update GNSS State (both sources)
        update_gps_phone()
        update_gps_sensor()
        
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

        # Accumulate high-entropy readings for 5-second averaging
        pm_samples["pm1"].append(pm1)
        pm_samples["pm25"].append(pm25)
        pm_samples["pm10"].append(pm10)
        noise_samples.append(noise_db)
        
        # 3. Create Snapshot
        data = {
            # Phone GNSS
            "lat": gps_phone["lat"],
            "lon": gps_phone["lon"],
            "alt": gps_phone["alt"],
            "sats": gps_phone["sats"],
            "accuracy": gps_phone["accuracy"],
            # Sensor GNSS (SAM-M10Q)
            "sensor_lat": gps_sensor["lat"],
            "sensor_lon": gps_sensor["lon"],
            "sensor_alt": gps_sensor["alt"],
            "sensor_sats": gps_sensor["sats"],
            # Other sensors
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
        
        # Only push to the database queue every 5 loops (5 seconds).
        # Noise & particulate are averaged over the last 5 seconds to reduce entropy.
        if loop_count % 5 == 0:
            avg_pm1   = round(sum(pm_samples["pm1"])   / len(pm_samples["pm1"]))
            avg_pm25  = round(sum(pm_samples["pm25"])  / len(pm_samples["pm25"]))
            avg_pm10  = round(sum(pm_samples["pm10"])  / len(pm_samples["pm10"]))
            avg_noise = round(sum(noise_samples) / len(noise_samples), 1)

            db_data = dict(data)
            db_data["pm1"]   = avg_pm1
            db_data["pm25"]  = avg_pm25
            db_data["pm10"]  = avg_pm10
            db_data["noise"] = avg_noise

            r.rpush('sensor_db_queue', json.dumps(db_data))

            # Reset accumulators for the next 5-second window
            pm_samples = {"pm1": [], "pm25": [], "pm10": []}
            noise_samples = []

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] Phone: {gps_phone['lat']:.5f}, {gps_phone['lon']:.5f}, Acc ±{gps_phone['accuracy']:.1f}m | "
                  f"Sensor: {gps_sensor['lat']:.5f}, {gps_sensor['lon']:.5f}, Sats {gps_sensor['sats']} | "
                  f"Temp: {tempC:.1f}°C | Humidity: {humRH:.1f}% | AQI: {aqi_val} | eCO2: {eco2_val}ppm | "
                  f"TVOC: {tvoc_val}ppb | PM2.5(avg): {avg_pm25}µg/m³ | Noise(avg): {avg_noise}dB")
        else:
            print("...")
            
    except Exception as e:
        print(f"Loop error: {e}")
    
    # Increment the loop counter and wait for the next cycle
    loop_count += 1
    time.sleep(1)