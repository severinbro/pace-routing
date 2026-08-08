# Personalized Assessment of Urban Comfort for Explainable Routing (PACE)

> **Project Status: Under Construction**
> The PACE software ecosystem, including the containerized edge-processing environment and the GraphSAGE data fusion scripts, is currently undergoing final refinement. This repository is being prepared for upcoming empirical field trials.

---

## Project Overview

Visit our website to learn more about the project and participate in our pilot surveys:
**[pace-routing.com](https://www.pace-routing.com)**

---

## Current Status

* ✅ The source code for the localized Django web application and survey interface.
* ✅ The Raspberry Pi edge-processing and sensor polling scripts (Docker/Gunicorn environment).
* ❌ Documentation and installation outline.
* ❌ The Python-based GraphSAGE and GraphLIME data fusion pipeline for network spatial interpolation.

---

## How to run?

### 1. Hardware & OS Configuration

Unlock the Pi 5's USB power limit and explicitly enable both I2C Bus 1 and Bus 2.

1. Open the boot configuration file:
```bash
sudo nano /boot/firmware/config.txt

```


2. Find the optional hardware interfaces section and ensure both I2C lines are active:
```ini
# Uncomment some or all of these to enable the optional hardware interfaces
dtparam=i2c_arm=on
dtoverlay=i2c2,baudrate=100000

```


3. Scroll to the very bottom and add the USB power override for the BrosTrend Wi-Fi adapter under the `[all]` tag:
```ini
[all]
usb_max_current_enable=1

```


4. Save, exit (`CTRL+O`, `Enter`, `CTRL+X`), and **reboot the Pi** entirely.

### 2. Wi-Fi Hotspot & Driver Setup

1. To ensure the Pi broadcasts its own offline network in the field, create the hotspot:
```bash
sudo nmcli device wifi hotspot ifname wlan1 ssid "pace" password "pace2026"
sudo nmcli connection modify Hotspot connection.autoconnect yes connection.autoconnect-retries 0

```


> **Note:** It is recommended to add a USB-WiFi dongle (e.g. [BrosTrend AX900 Mini](https://www.brostrend.com/products/ax7l)) to emphasize the WiFi signal for more reach and stability.


2. **Install Dependencies:** Make sure [Docker](https://docs.docker.com/engine/install/) is installed.

### 3. Download the Project & Map Tiles

1. Clone this repository to your Raspberry Pi:
```bash
git clone https://github.com/severinbro/pace-routing.git
cd pace-routing

```

2. To use the map in the field without the internet, you must download the OpenStreetMap data for your target region and name it region.osm.pbf (e.g. download from [Geofabrik](https://www.geofabrik.de/)). Place this file directly into the ./data/maps/ directory in the project root. The container's internal map creation process will parse this data upon startup.

### 4. Generate the TLS Certificate

The browser's Geolocation API (used to push the admin phone's GPS coordinates to the Pi) requires HTTPS. A self-signed certificate is generated for the Pi's hotspot:

```bash
bash generate_cert.sh
```

This creates `nginx/certs/pace.crt` and `nginx/certs/pace.key`. The certificate is valid for 10 years and covers the IP `10.42.0.1`.

> **Note:** On first visit, the phone's browser will show a "Your connection is not private" warning. Tap **Advanced → Proceed to 10.42.0.1 (unsafe)**. This is normal for self-signed certs on a private network — you only need to do this once per browser.

### 5. Launch the System

Build and start the Docker containers. The `privileged: true` flag and device mappings in the compose file will automatically pass the I2C buses and USB microphone into the containers.

```bash
docker compose up --build -d

```

The app will now be available on any device connected to the Pi's hotspot at **`https://10.42.0.1`** (HTTPS, port 443 — the default). HTTP requests are automatically redirected to HTTPS.

Per default, the participant is directed to the survey page. Tabs displaying the dashboard, map, and data browser are invisible at first. To access those, sign in with an admin account.

### 6. GNSS & System Time

When an admin signs in on their smartphone, the browser continuously pushes the phone's GPS coordinates to the Pi via the Geolocation API. These coordinates are used for:

- Live position on the map
- GNSS snapshots attached to survey responses
- Synchronizing the Pi's system clock (the admin phone's GPS time is authoritative)

Only the admin phone provides GNSS data — participant phones do not interfere.