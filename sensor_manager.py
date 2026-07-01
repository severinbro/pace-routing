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
    
# 3. Init Bus 2 for GNSS & PMSA
GPS_ADDR = 0x42
BYTES_AVAIL_REG = 0xFD
DATA_STREAM_REG = 0xFF
PMSA_ADDR = 0x12

try:
    heavy_bus = SMBus(2) 
    print("I2C Bus 2 (GNSS & PMSA) Initialized.")
except Exception as e:
    print(f"I2C Bus 2 Error: {e}")

# --- Global variables ---
gps_data = { "lat": 0.0, "lon": 0.0, "alt": 0.0, "sats": 0 }

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
    global gps_data
    buffer = ""
    try:
        # 1. Check how many bytes are waiting in the SAM-M10Q buffer
        waiting_bytes_list = heavy_bus.read_i2c_block_data(GPS_ADDR, BYTES_AVAIL_REG, 2)
        num_bytes = (waiting_bytes_list[0] << 8) | waiting_bytes_list[1]
        
        # Ignore hardware glitch readings
        if num_bytes == 0 or num_bytes == 65535 or num_bytes > 1024:
            return
            
        time.sleep(0.01)

        print(f"Found {num_bytes} bytes waiting in buffer...")
        
        # 2. Read the bytes in chunks
        while num_bytes > 0:
            chunk_size = min(num_bytes, 16)
            data = heavy_bus.read_i2c_block_data(GPS_ADDR, DATA_STREAM_REG, chunk_size)
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

                            gps_data["lat"] = lat
                            gps_data["lon"] = lon
                            gps_data["alt"] = float(parts[9])
                            gps_data["sats"] = int(parts[7])

                            print(f"SUCCESS -> Lat {lat:.5f}, Lon {lon:.5f}, Alt {gps_data['alt']}m, Sats {gps_data['sats']}")
                        except ValueError:
                            print(f"Parsing error on line: {line}")
                            continue
                    else:
                        print(f"No fix yet. Satellites visible: {parts[7] if len(parts) > 7 else '0'}")
                        visible_sats = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0
                        gps_data["sats"] = visible_sats

    except Exception as e:
        print(f"GPS read error: {e}")

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
            print(f"[{timestamp}] GPS: {gps_data['lat']:.5f}, {gps_data['lon']:.5f}, Sats: {gps_data['sats']} | Temp: {tempC:.1f}°C | Humidity: {humRH:.1f}% | AQI: {aqi_val} | eCO2: {eco2_val}ppm | TVOC: {tvoc_val}ppb | PM2.5: {pm25}µg/m³ | Noise: {noise_db}dB")
        else:
            print("...")
            
    except Exception as e:
        print(f"Loop error: {e}")
    
    # Increment the loop counter and wait for the next cycle
    loop_count += 1
    time.sleep(1)