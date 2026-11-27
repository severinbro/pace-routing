# SENSE — Processing and receiving platform

SENSE runs the shared PACE web app and receives HALO recordings. HALO sends
v2 live JPEG previews during capture and v1 verified session archives after
extraction. See the [HALO transfer guide](../HALO/README.md#transfer-to-sense).

## 1. Architecture

```text
[HALO capture Pi]
       │
       │ HTTPS PUT /halo-upload/v1/sessions/<SESSION_ID>
       ▼
┌── SENSE processing Pi (Docker Compose) ──────────────────────┐
│                                                                 │
│  ┌─────────────────────────┐                                    │
│  │  sense-nginx            │  TLS termination (Port 443)       │
│  │  (Reverse Proxy)        │  Streaming without buffering  │
│  └───────────┬─────────────┘                                    │
│              │ Forward HTTP /halo-upload/v1/                    │
│              ▼                                                  │
│  ┌─────────────────────────┐                                    │
│  │  sense-receiver         │  Token auth, streaming SHA256,     │
│  │  (Python Stdlib Server) │  Safe Unpack & Manifest-Prüfung    │
│  └───────────┬─────────────┘                                    │
│              │ Atomic commit (.transfer_complete)              │
│              ▼                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Shared Storage Volume: /data                              │  │
│  │ ├── staging/     (temporäre Downloads & Entpackung)       │  │
│  │ ├── incoming/    (geprüfte, freigegebene Sessions)        │  │
│  │ ├── processing/  (AI HAT+ / SfM processing)     │  │
│  │ ├── completed/   (processed)                     │  │
│  │ └── failed/      (Fehlerhafte Datensätze)                 │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

Nginx also proxies `POST /halo-upload/v2/sessions/<SESSION_ID>/frames` to
the receiver. The same shared Bearer token authenticates both protocols.
A v2 request is limited to 10 MiB, and a JPEG to 5 MiB. SENSE stores live
frames under `data/live/` for downstream processing. Live previews do not
replace the verified v1 archive.

## 2. Directory layout

```text
pace-routing/SENSE/
├── compose.yaml              # Docker Compose (Nginx, Receiver, Dashboard, PACE web, DB, Redis, NTP)
├── .env.example              # Template for ports, UIDs, and paths
├── 3d_print_files/           # SENSE enclosure and mounts (STL/Blender)
├── config/
│   ├── nginx.conf            # Nginx template for streaming and HTTPS termination
│   ├── pace.env.example      # Template for database and Django environment
│   ├── generate_certs.sh     # Helper script for TLS certificates
│   ├── certs/                # TLS certificates (server.crt, server.key)
│   └── upload.token          # Shared Bearer token (0600)
├── docker/
│   ├── receiver.Dockerfile
│   ├── dashboard.Dockerfile
│   └── ntp.Dockerfile
├── dashboard/                 # Internal HALO control and transfer reconciliation
├── receiver/
│   ├── server.py             # Receiver for archive v1 and live v2
│   └── verify.py             # Kryptografische Manifest- & Strukturprüfung
├── data/                     # Local runtime data (ignored by Git)
└── tests/
    └── test_receiver.py      # Receiver integration tests
```

## 3. Install and start SENSE

Use Raspberry Pi OS (64-bit; Debian 13 Trixie on the current installation)
with Docker and its Compose plugin. Run Compose commands from the `SENSE/`
directory. On the PACE network the SENSE host is reachable over SSH at
`<sense-user>@10.42.10.1`. HALO connects to the IP address and validates the
certificate using the TLS name `pace-sense.local`.

### Step 1: Prepare configuration and directories

```bash
cp .env.example .env
mkdir -p data/{staging,incoming,processing,completed,failed}
```

Copy `config/pace.env.example` to `config/pace.env` and replace its database
password placeholders with the same password. Generate a persistent
`PACE_DJANGO_SECRET_KEY`, for example with `openssl rand -hex 50`, and put it
in `config/pace.env`. Keep `PACE_DEBUG=false` on SENSE. Configure the internal
dashboard token, restricted SSH key, and known-hosts files referenced by
`compose.yaml` before starting the services. In `.env`, set `HALO_SSH_TARGET`
to the HALO Linux account and address used by that key.

The template binds the test server to `127.0.0.1:18443` (HTTPS) and
`127.0.0.1:18080` (HTTP). Bind HTTPS to the PACE interface for HALO access.
Adjust the upload settings on HALO to match.

### PACE Wi-Fi connection (SENSE at 10.42.10.1)

Set `SENSE_HTTPS_PORT=10.42.10.1:18443` in `.env`. The local HTTP listener
at `127.0.0.1:18080` redirects to the configured HTTPS address. The
self-signed certificate contains `10.42.10.1` as an IP SAN. After creating
the certificate and token in step 2, copy them into HALO's configuration.

```bash
scp -i ~/.ssh/halo_ed25519 config/upload.token <halo-user>@10.42.10.2:/path/to/pace-routing/HALO/config/halo-upload.token
scp -i ~/.ssh/halo_ed25519 config/certs/server.crt <halo-user>@10.42.10.2:/path/to/pace-routing/HALO/config/sense-server.crt
```

Set these values in HALO's `config/capture.env`.

```text
SENSE_UPLOAD_HOST=10.42.10.1
SENSE_TLS_NAME=pace-sense.local
SENSE_HTTPS_PORT=18443
```

On HALO, check connectivity and the uploader. You can also queue an
older, fully extracted session manually.

```bash
curl --cacert config/sense-server.crt https://10.42.10.1:18443/healthz
sudo docker compose restart uploader
bash ./upload.sh <SESSION_ID>
sudo docker compose logs --tail 20 uploader
```

New extracted sessions are queued automatically. Verify the receipt and
files on SENSE under `data/incoming/<SESSION_ID>/`. HALO trusts the
self-signed certificate through the copied `sense-server.crt`.

### Time synchronization over PACE

SENSE synchronizes its system clock with `systemd-timesyncd`. The
`sense-ntp` Compose service shares that clock over UDP port 123 on
`10.42.10.1`. Chrony runs with `-x`, so it does not adjust SENSE's clock.
Configure HALO's `/etc/systemd/timesyncd.conf.d/pace.conf` as follows.

```ini
[Time]
NTP=10.42.10.1
FallbackNTP=
```

Check synchronization from SENSE. Its `halo` SSH alias connects to
`<halo-user>@10.42.10.2` using `~/.ssh/halo_ed25519`.

```bash
timedatectl show -p NTPSynchronized
docker compose ps ntp
docker exec sense-ntp chronyc clients
ssh halo 'timedatectl show -p NTPSynchronized; timedatectl timesync-status'
```

HALO continues following the SENSE clock if SENSE loses its internet time
source. With NetworkManager`s shared PACE network, install the restricted
dispatcher rule so the dashboard container can reach HALO over SSH.
The rule is reapplied after Wi-Fi restarts.

```bash
sudo install -m 755 config/90-pace-halo-ssh /etc/NetworkManager/dispatcher.d/90-pace-halo-ssh
sudo env CONNECTION_ID=PACE /etc/NetworkManager/dispatcher.d/90-pace-halo-ssh wlan1 up
```

### Step 2: Create TLS certificates and the shared upload token

Use the supplied certificate helper.

```bash
bash config/generate_certs.sh
```

Generate the upload token. HALO must use the same token.

```bash
openssl rand -hex 32 > config/upload.token
chmod 600 config/upload.token
```

### Step 3: Start the containers

```bash
docker compose up -d --build
docker compose logs -f
```

### Step 4: Check the HTTPS endpoint

```bash
curl --cacert config/certs/server.crt https://10.42.10.1:18443/healthz
# Expected: {"status":"ok","node":"sense"}
```

### Recover after moving the checkout or changing accounts

Docker remembers absolute bind-mount paths. After a move, a container can
use an old path and PostgreSQL can initialize an empty database there.
Recreate the database container from the current SENSE directory, then
check the mount and existing Django migrations.

```bash
cd /path/to/pace-routing/SENSE &&
docker compose up -d --no-build --no-deps --force-recreate --wait pace-db &&
docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' sense-pace-db &&
docker compose exec -T -u postgres pace-db sh -ec \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM django_migrations;"'
```

If the mount points to the intended `data/pace-postgres/` and the
migrations are present, start the remaining services with
`docker compose up -d --no-build --wait` and check the HTTPS endpoint.

## 4. Received session layout

The receiver unpacks the v1 archive and validates `SHA256SUMS` before
atomically publishing the session.

```text
data/incoming/<SESSION_ID>/
├── capture.json              # Camera synchronization and UTC status
├── imu.csv                   # BNO085 measurements (15 columns)
├── frames.csv                # Timestamps for all JPEGs
├── pairs.csv                 # Stereo pairs with IMU brackets
├── sync_report.json          # Synchronisationsqualität & Skew
├── frames/
│   ├── left/frame_*.jpg      # Corrected and rotated left frames
│   └── right/frame_*.jpg     # Corrected and rotated right frames
├── SHA256SUMS                # Verifizierte Prüfsummen
├── .capture_complete
├── .extraction_complete
└── .transfer_complete        # JSON-Quittung des Empfängers
```

The internal dashboard revalidates committed sessions and asks HALO to
clean up local video and frame copies after the manifests match. HALO
keeps its metadata and receipts. Unverified sessions retain their media.

## 5. Downstream processing

Downstream jobs can monitor `data/incoming/` for committed sessions. A
worker can atomically move each completed upload to `data/processing/`
and then to `data/completed/` after successful inference, for example
segmentation, stereo depth, or structure from motion. Give the
reconciliation service access to the committed session before moving it
out of `incoming/` if HALO cleanup is needed.

## 6. Tests

Run these checks from `SENSE/`.

```bash
# SENSE receiver and control tests
python3 -m unittest discover -s tests -v

# Validate an extracted session manually
python3 receiver/verify.py data/incoming/<SESSION_ID>
```

## 7. Shared PACE interface

Django runs as `pace-web` with PostgreSQL and Redis. Open
`https://10.42.10.1:18443/login/` to sign in. The login lands on `/about/`.
`/admin-home/` is a compatibility redirect for staff. Signed-in
participants can open the survey and `/map/`. Staff can edit route
stops and use the controllers and live sensor dashboards. The SENSE
Controller at `/sense/` has start/stop controls and tables of saved readings
and surveys. Use Data & Export at `/data-browser/` for downloads.

`/ui/` redirects to `/halo/`. Django authorizes staff actions with its
session and CSRF checks, then calls the private control service using an
internal token. After five failed Django login attempts, further attempts
from that address are blocked for ten minutes. Keep tokens and the
restricted HALO SSH key in `config/` outside Git.

Create the first staff account on a fresh installation.

```bash
docker compose exec pace-web python manage.py createsuperuser
```

HALO preflight checks two cameras, a BNO085 IMU, a BH1750 light sensor, a
thermal camera, synchronized time, and at least 5 GiB available. Recording
ends gracefully after at most 30 minutes or when free space reaches 4 GiB.
SENSE web views can still run without the legacy SENSE sensor hardware.
The SENSE Compose stack does not include the legacy sensor worker or the
database writer. Keep `SENSE_HARDWARE_AVAILABLE=false` unless both are
configured. The Controller shows recording as unavailable in that case.
Saved readings, surveys, and exports are still available.

The offline map reads a finalized archive at `data/maps/offline.mbtiles`
inside the configured SENSE data directory. Build replacements in the
same directory under a different name, close SQLite with no pending WAL,
and then rename the finished file over the live archive.

Check the Django app and HTTPS endpoint.

```bash
docker compose exec -T pace-web python manage.py test data_cube.tests
curl --cacert config/certs/server.crt https://10.42.10.1:18443/healthz
```
