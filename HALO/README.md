# HALO — Head-mounted Ambiance and Localization Optics

HALO runs Redis, an on-demand `capture` service, a background `extractor`, and
    a background `uploader`. Camera 0 is left; camera 1 is right. The SENSE Pi runs
the HTTPS receiver and stores accepted sessions under `data/incoming/`.

## Setup and timed recording

From the `HALO` project folder:

```bash
sudo install -m 644 config/99-halo-redis.conf /etc/sysctl.d/99-halo-redis.conf
sudo sysctl -p /etc/sysctl.d/99-halo-redis.conf
sudo docker compose build
sudo docker compose up -d redis extractor uploader
bash ./record.sh 120
sudo docker compose logs -f extractor
```

`record.sh 120` records for 120 seconds; pass any duration of at least 1 second,
such as `bash ./record.sh 15.5`. The script ensures Redis and the extractor are
running, then starts one capture. A second invocation refuses to use the
cameras while a timed recording is active. Capture returns after the videos
and IMU log are saved. Frame extraction runs in the background. A session is
ready when its `.extraction_complete` file
appears. The extractor checks the session folder for work that was not queued
or was interrupted, then retries it. Redis stores its queue under `data/redis/`
with append-only persistence.

Docker is enabled at boot on this Pi. Redis and the extractor use
`restart: unless-stopped`, so once started they come back after a reboot.
Capture has no automatic restart and is excluded from a plain
`sudo docker compose up -d`; it runs only when explicitly requested.
If Redis or the extractor is manually stopped, `bash ./record.sh DURATION_SECONDS`
starts them again.

The host's Redis memory-overcommit setting is installed from
`config/99-halo-redis.conf` into `/etc/sysctl.d/` during setup. Check it with
`sysctl vm.overcommit_memory`; it should report `1` after a reboot too.

All recordings and images are saved under `data/sessions/<session-id>/`:

```text
data/
├── focus/
│   ├── left.jpg
│   └── right.jpg
├── redis/                # local queue persistence
└── sessions/
    └── <session-id>/
        ├── video/left.mkv, right.mkv
        ├── frames/left/frame_000000.jpg, ...
        ├── frames/right/frame_000000.jpg, ...
        ├── frames/thermal/frame_000000.raw, frame_000000.png, frame_000000_color.png, ...
        ├── logs/
        ├── imu.csv
        ├── light.csv (when BH1750 readings are available)
        ├── capture.json
        ├── frames.csv
        ├── pairs.csv
        ├── sync_report.json
        ├── thermal.csv, thermal.json
        └── SHA256SUMS
```

`data/` is local runtime data and is ignored by Git and Docker builds. Capture
settings are in `config/capture.env`; `DATA_ROOT` is the container path and
normally should not be changed. `CLIP_DURATION_SECONDS` is the fallback for a
direct `sudo docker compose run --rm capture once`; `record.sh` supplies its own
duration. Extracted JPEGs rotate 180° by default (`IMAGE_ROTATION=180`).
The current `FRAME_INTERVAL_SECONDS=1` setting extracts roughly one stereo
pair per second, not every encoded video frame. Both source videos are kept
locally until a confirmed upload-and-cleanup path is implemented.

New recordings use Raspberry Pi software camera sync: camera 1/right starts as
the client, then camera 0/left starts as the server. `capture.json` schema v2
contains the encoder's first-frame UTC time for each camera. A capture that
does not confirm software sync or a plausible UTC clock remains in a hidden
`.partial` session folder for diagnosis; it is not queued as synchronized.

The extractor probes source-video presentation timestamps, chooses a nearby
stereo exposure in each roughly one-second time bucket, and validates that
the resulting JPEGs match those timestamps. `frames.csv` maps **every** JPEG
to its source PTS and capture UTC nanoseconds, including any unpaired image.
`pairs.csv` keeps its original first four columns, then records left/right
capture times, absolute stereo skew, pair midpoint, and zero-based indices
of the surrounding `imu.csv` rows. `sync_report.json` counts unpaired images
and pairs outside the timing-quality limits. Large skew or absent/slow IMU
brackets are flagged, never silently corrected; the images remain available.
The IMU `utc_ns` is the midpoint of the host read window, not a guaranteed
sensor-sample timestamp. Raw acceleration, gyro, and quaternion values remain
in `imu.csv` along with read-window start, end, and duration.

## Ambient light sensor

The BH1750 shares I²C bus 1 with the BNO085 at a different address. HALO
uses `LIGHT_ADDRESS=0x23` by default; set it to `0x5c` if the module's address
pin is high. Verify the breakout's pin labels before connecting it to the
Pi's 3.3 V I²C bus. `LIGHT_ENABLED=true` records during each timed session at
`LIGHT_INTERVAL_SECONDS=0.2` (5 Hz) in continuous high-resolution mode.

`light.csv` contains lux and both UTC and monotonic host-read midpoint times,
plus read-window start/end UTC and duration. These are **not** exact sensor
integration timestamps. The extractor adds `light_sample_index`, `light_lux`,
`light_gap_ns`, and `light_status` to `pairs.csv`. A nearest reading more than
250 ms from the stereo-pair midpoint is marked missing rather than assigned.
`capture.json` records BH1750 mode, address, interval, and capture status;
`sync_report.json` counts light samples and pair gaps. A missing or disconnected
BH1750 does not stop camera capture. Existing sessions remain unchanged. When
present, `light.csv` is checksummed and included in the SENSE upload archive.

To test the light sensor without CSI cameras, run from the HALO project folder:

```bash
i2cdetect -y 1 0x23 0x23
light_test_id="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "data/light-test/$light_test_id"
sudo docker compose run --rm --no-deps --entrypoint python3 capture \
  /app/capture/bin/light_logger.py \
  --output "/data/light-test/$light_test_id/light.csv" \
  --bus 1 --address 0x23 --interval 0.2 --duration 5
head "data/light-test/$light_test_id/light.csv"
```

If the address jumper is high, scan `0x5c` and use `--address 0x5c` instead.

A session gets `.extraction_complete` only after image counts and timestamp
mappings agree. Deterministic timing failures get `.extraction_failed` and are
not retried forever; their source videos and diagnostic logs remain in place.
Prior session files are not migrated or rewritten. For any model-building
workflow, inspect `sync_report.json` and `pairs.csv` timing flags first.

## Thermal camera

New sessions also use the Thermal Master P2 Pro (`0bda:5830`) connected to
HALO over USB. `config/capture.env` enables it and selects the stable
`/dev/v4l/by-id/...video-index0` capture node; index1 is a metadata node.
The capture container already has `/dev` mounted. It records the 256×384
YUYV stream at 25 Hz to a lossless temporary file while stereo capture runs.
The extractor chooses the frame nearest each stereo pair time, saves its
256×192 apparent-temperature grid as little-endian `uint16` raw counts, a
grayscale PNG, and an absolute-color PNG. All three are rotated 180° to match
the stereo images. The color PNG maps a fixed 0–60 °C range to a black-purple-
red-orange-yellow palette by default; values outside the range are clamped.
Change `THERMAL_COLOR_MIN_C` and `THERMAL_COLOR_MAX_C` in `config/capture.env`
to set a different fixed range before recording. The range and palette are
written to `thermal.json` so colors can be interpreted consistently across
sessions. Color is derived from the temperature grid, not per-image brightness.
For the raw grid, `°C = count / 64 - 273.15`; these are camera-provided apparent
temperatures, not independently calibrated surface temperatures.

`thermal.csv` maps each stereo pair to the raw, grayscale, and color files, UTC time, skew, and
quality status. The added columns at the end of `pairs.csv` repeat this
mapping. A difference over 50 ms is flagged; a gap over 250 ms, a bad frame,
or a disconnected camera is marked missing. Stereo and IMU data still finish
and transfer in those cases. `thermal.json` records the pixel format and any
source error. The temporary 25 Hz stream is removed once extraction succeeds;
it remains for diagnosis if its source could not be decoded.

## Focus the cameras

Stop continuous recording, list available camera indices, then capture a focus
image from each camera. Run this from the project folder:

```bash
sudo docker compose stop capture
rpicam-hello --list-cameras
mkdir -p data/focus
set -e
rpicam-still --camera 0 --rotation 180 --nopreview --timeout 2000 --width 1920 --height 1080 --output data/focus/left.jpg
test -s data/focus/left.jpg
rpicam-still --camera 1 --rotation 180 --nopreview --timeout 2000 --width 1920 --height 1080 --output data/focus/right.jpg
test -s data/focus/right.jpg
```

`set -e` stops at the first failed capture and displays its error. If the camera
list has different indices, use the listed values. For M12 lenses, loosen the
small locking ring, turn the lens barrel in small steps while checking details
at full size, then gently retighten it. Focus both on a detailed target at the
expected recording distance.

## Queue and storage

Compose runs Redis, the capture command, and the extractor on a private Docker
network. Capture and extractor communicate through Redis using the session ID;
Redis does not carry image or video bytes. Both containers mount the same host
`data/` directory, which is where the session files are written and read. The
processing Pi is not connected to this Compose network yet.

Check queue depth with:

```bash
sudo docker compose exec redis redis-cli LLEN halo:frames:pending
sudo docker compose exec redis redis-cli LLEN halo:frames:processing
```

The extractor has a one-core CPU limit and lower process priority so recording
gets resources first. `MAX_LOCAL_SESSIONS` (default 10) applies to the optional
continuous `daemon` mode, not to timed `record.sh` calls. A queue protects
captured videos awaiting extraction; it cannot guarantee that a camera or
encoder never drops frames during recording, or store an unlimited backlog.
Timed recordings are not automatically deleted, so monitor available disk space.

The capture container needs camera, DMA, and I²C device access, so run it on
the camera Pi. Redis is reachable only on this Compose network; no host port is
published.

## Transfer to SENSE

After extraction finishes, HALO marks the session `.transfer_pending`. The
uploader scans for those sessions and sends each once over HTTPS to the SENSE
Nginx proxy using `PUT /halo-upload/v1/sessions/<SESSION_ID>`. It authenticates
with the shared Bearer token and sends the protocol version, session ID, and
archive SHA256 headers expected by the receiver. SENSE validates the archive
and its `SHA256SUMS` before committing the session atomically to
`SENSE/data/incoming/<SESSION_ID>/`. A successful response creates a local
`.transfer_complete` receipt. Failed attempts stay pending and retry; they do
not remove the local session.

The transfer archive includes the extracted JPEGs, selected thermal frames,
thermal timing metadata, IMU data, timing manifests,
and completion markers. Raw video is left on HALO by default
(`UPLOAD_INCLUDE_VIDEO=false`); the archive gets a checksum manifest covering
exactly its transmitted files. The `config/capture.env` settings specify
`SENSE_UPLOAD_HOST`, `SENSE_TLS_NAME`, and `SENSE_HTTPS_PORT`. On the PACE
WLAN, use `SENSE_UPLOAD_HOST=10.42.10.1`,
`SENSE_TLS_NAME=pace-sense.local`, and `SENSE_HTTPS_PORT=18443`. The TLS name
matches the copied SENSE certificate while the connection uses its WLAN IP.

The uploader mounts `config/halo-upload.token` and `config/sense-server.crt`
read-only; neither credential is copied into the image or project archive.
They are ignored by Git and Docker build context. To manually queue an older
completed session, run `bash ./upload.sh SESSION_ID`. New recordings are
queued automatically after successful extraction. Check transfer status with:

```bash
sudo docker compose logs -f uploader
test -f data/sessions/SESSION_ID/.transfer_complete
```

Redis remains private to the HALO Compose network. The Pi-to-Pi connection is
HTTPS through SENSE's published port 18443; the two Docker networks are separate.

## Time synchronization over PACE

HALO has no direct internet time source. Its `systemd-timesyncd` client uses
SENSE at `10.42.10.1` over NTP (UDP 123). The persistent configuration is
`/etc/systemd/timesyncd.conf.d/pace.conf`:

```ini
[Time]
NTP=10.42.10.1
FallbackNTP=
```

Check with `timedatectl show -p NTPSynchronized` and
`timedatectl timesync-status`. SENSE must be running and have a synchronized
clock for accurate HALO UTC timestamps.
