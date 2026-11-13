# SENSE — Verarbeitungs- und Empfangsplattform

Dieses Verzeichnis enthält den containerisierten Upload- und Empfangsdienst für die **SENSE-Plattform** (Verarbeitungs-Pi 5 / PACE-Server). Er implementiert serverseitig das **HALO Protocol v1** (`docs/upload-protocol.md`) zum Empfang von Stereo-Bildpaaren, IMU-Sensordaten und Metadaten vom HALO-Kamera-Pi.

---

## 1. Architektur

```text
[HALO Aufnahme-Pi]
       │
       │ HTTPS PUT /halo-upload/v1/sessions/<SESSION_ID>
       ▼
┌── SENSE Verarbeitungs-Pi (Docker Compose) ──────────────────────┐
│                                                                 │
│  ┌─────────────────────────┐                                    │
│  │  sense-nginx            │  TLS-Terminierung (Port 443)       │
│  │  (Reverse Proxy)        │  Streaming ohne Request-Buffering  │
│  └───────────┬─────────────┘                                    │
│              │ Forward HTTP /halo-upload/v1/                    │
│              ▼                                                  │
│  ┌─────────────────────────┐                                    │
│  │  sense-receiver         │  Token-Auth, Streaming-SHA256,     │
│  │  (Python Stdlib Server) │  Safe Unpack & Manifest-Prüfung    │
│  └───────────┬─────────────┘                                    │
│              │ Atomare Ablage (.transfer_complete)              │
│              ▼                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Shared Storage Volume: /data                              │  │
│  │ ├── staging/     (temporäre Downloads & Entpackung)       │  │
│  │ ├── incoming/    (geprüfte, freigegebene Sessions)        │  │
│  │ ├── processing/  (in Bearbeitung durch AI HAT+ / SfM)     │  │
│  │ ├── completed/   (fertig verarbeitet)                     │  │
│  │ └── failed/      (Fehlerhafte Datensätze)                 │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Ordnerstruktur

```text
Software-System/SENSE/
├── compose.yaml              # Docker Compose (Nginx + Receiver)
├── .env.example              # Vorlage für Ports, UIDs, Pfade
├── config/
│   ├── nginx.conf            # Nginx Streaming-Reverse-Proxy
│   ├── receiver.env.example  # Backend-Konfiguration
│   ├── generate_certs.sh     # Hilfsskript für TLS-Zertifikate
│   ├── certs/                # TLS-Zertifikate (server.crt, server.key)
│   └── upload.token          # Gemeinsames Bearer-Secret (0600)
├── docker/
│   └── receiver.Dockerfile   # Leichtgewichtiges Python 3.11-slim Image
├── receiver/
│   ├── server.py             # Multithreaded HTTP-Server (Protocol v1)
│   └── verify.py             # Kryptografische Manifest- & Strukturprüfung
├── data/                     # Lokaler Datenspeicher (Git-ignoriert)
└── tests/
    └── test_receiver.py      # Umfassende Integrationstests
```

---

## 3. Installation & Inbetriebnahme auf dem SENSE-Pi

### Voraussetzungen
- Raspberry Pi OS (Bookworm 64-Bit)
- Docker & Docker Compose Plugin (`sudo apt install docker-ce docker-compose-plugin`)

### Schritt 1: Konfiguration & Verzeichnisse vorbereiten
```bash
cp .env.example .env
mkdir -p data/{staging,incoming,processing,completed,failed}
```

Die Vorlage bindet den lokalen Testserver an `127.0.0.1:18443` (HTTPS) und
`127.0.0.1:18080` (HTTP). Für den späteren Zugriff vom HALO-Pi müssen die
Ports an einer erreichbaren Netzwerkschnittstelle gebunden und die passende
Adresse im HALO-Upload konfiguriert werden.

### PACE WLAN Verbindung (SENSE: 10.42.10.1)

Auf diesem SENSE-Pi ist `SENSE_HTTPS_PORT=10.42.10.1:18443` in `.env` gesetzt.
Das selbstsignierte Zertifikat enthält `10.42.10.1` als IP SAN. Vom SENSE-Pi
aus die Zugangsdaten in HALOs bestehende Konfigurationspfade kopieren:

```bash
scp -i ~/.ssh/halo_ed25519 config/upload.token Pi_Mz@10.42.10.2:/home/Pi_Mz/Documents/HALO/config/halo-upload.token
scp -i ~/.ssh/halo_ed25519 config/certs/server.crt Pi_Mz@10.42.10.2:/home/Pi_Mz/Documents/HALO/config/sense-server.crt
```

Auf HALO in `config/capture.env` diese Werte setzen:

```text
SENSE_UPLOAD_HOST=10.42.10.1
SENSE_TLS_NAME=pace-sense.local
SENSE_HTTPS_PORT=18443
```

Danach im HALO-Projektverzeichnis Verbindung und Uploader prüfen sowie eine
bereits vollständig extrahierte Session manuell einreihen:

```bash
curl --cacert config/sense-server.crt https://10.42.10.1:18443/healthz
sudo docker compose restart uploader
bash ./upload.sh <SESSION_ID>
sudo docker compose logs --tail 20 uploader
```

Neue erfolgreich extrahierte Sessions werden automatisch eingereiht. Auf SENSE
die Bestätigung und Dateien unter `data/incoming/<SESSION_ID>/` prüfen. HALO
vertraut dem selbstsignierten Zertifikat über die kopierte `sense-server.crt`.

### Zeitsynchronisation über PACE

SENSE synchronisiert seine Systemuhr mit `systemd-timesyncd`. Der Docker-Dienst
`sense-ntp` gibt diese Uhr über UDP-Port 123 ausschließlich auf der PACE-Adresse
`10.42.10.1` frei. Sein Chrony-Prozess läuft mit `-x` und verändert die
SENSE-Systemuhr nicht. Der Dienst startet mit Docker automatisch neu.

Auf HALO ist `/etc/systemd/timesyncd.conf.d/pace.conf` so gesetzt:

```ini
[Time]
NTP=10.42.10.1
FallbackNTP=
```

Status von SENSE aus prüfen:

```bash
timedatectl show -p NTPSynchronized
docker compose ps ntp
docker exec sense-ntp chronyc clients
ssh halo 'timedatectl show -p NTPSynchronized; timedatectl timesync-status'
```

HALO verwendet die SENSE-Uhr auch ohne eigenen Internetzugang. Wenn SENSE
seinen Internet-Zeitabgleich verliert, folgt HALO weiterhin der SENSE-Uhr.

### Schritt 2: TLS-Zertifikate & Shared Secret anlegen
Für den lokalen Betrieb oder Testlauf kann das mitgelieferte Generierungsskript genutzt werden:
```bash
bash config/generate_certs.sh
```

Anschließend das gemeinsame Zugriffstoken erzeugen (muss mit HALO übereinstimmen):
```bash
openssl rand -hex 32 > config/upload.token
chmod 600 config/upload.token
```

### Schritt 3: Container starten
```bash
docker compose up -d --build
docker compose logs -f
```

### Schritt 4: Funktionsprüfung (Healthcheck)
```bash
curl --cacert config/certs/server.crt https://10.42.10.1:18443/healthz
# Antwort: {"status":"ok","node":"sense"}
```

---

## 4. Eingehende Session-Struktur

Sobald eine Session von HALO erfolgreich empfangen, entpackt und gegen `SHA256SUMS` geprüft wurde, liegt sie atomar freigegeben unter `data/incoming/<SESSION_ID>/`:

```text
data/incoming/<SESSION_ID>/
├── capture.json              # Software-Sync & UTC-Kamerastatus
├── imu.csv                   # BNO085 Messungen (15 Spalten)
├── frames.csv                # Zeitstempelzuordnung aller JPEGs
├── pairs.csv                 # Stereo-Paarliste inkl. IMU-Brackets
├── sync_report.json          # Synchronisationsqualität & Skew
├── frames/
│   ├── left/frame_*.jpg      # Entzerrte/rotierte linke Frames
│   └── right/frame_*.jpg     # Entzerrte/rotierte rechte Frames
├── SHA256SUMS                # Verifizierte Prüfsummen
├── .capture_complete
├── .extraction_complete
└── .transfer_complete        # JSON-Quittung des Empfängers
```

---

## 5. Anbindung nachgelagerter Pipeline-Schritte (SENSE Downstream)

Verarbeitungsprozesse auf dem SENSE-Pi (z. B. Segmentierung mit dem Raspberry Pi AI HAT+ 2 via Hailo-8, Stereo-Tiefenschätzung oder SfM) überwachen `data/incoming/`:

1. Sobald `.transfer_complete` in `data/incoming/<SESSION_ID>` existiert, verschiebt ein Worker-Prozess die Session atomar nach `data/processing/<SESSION_ID>`.
2. Nach erfolgreicher Inferenz (GVI, SVF, Tiefenkarten) wird das Ergebnis nach `data/completed/<SESSION_ID>` verschoben.

---

## 6. Tests ausführen

Zur Überprüfung des Gesamtsystems gegen echte HALO-Sessions:

```bash
# SENSE Receiver-Testsuite
python3 -m unittest discover -s tests -v

# Manuelle Session-Validierung eines Ordners
python3 receiver/verify.py data/incoming/<SESSION_ID>
```

---

## 7. Gemeinsame PACE-Oberfläche für SENSE und HALO

Das frühere [PACE-Routing-Projekt](https://github.com/severinbro/pace-routing) liegt
hier unter `pace-routing/` und läuft als `pace-web` mit PostgreSQL und Redis in
diesem Compose-Stack. Einstieg im PACE-WLAN:

- **https://10.42.10.1:18443/login/** – bestehender Django-Login mit Benutzername und Passwort.
- **https://10.42.10.1:18443/admin-home/** – Auswahl zwischen SENSE und HALO.
- **https://10.42.10.1:18443/sense/** – alte PACE-Funktionen und Sensoransichten.
- **https://10.42.10.1:18443/halo/** – Aufnahmesteuerung, Vorprüfung, Synchronisation und Transfer.

`/ui/` leitet auf `/halo/` um. HALO verwendet dieselbe Django-Sitzung und den
CSRF-Schutz wie die alte Oberfläche. Nur Mitarbeiterkonten sehen und bedienen
HALO. Fünf fehlgeschlagene Anmeldungen sperren weitere Versuche von dieser
Adresse für zehn Minuten. Der Dashboard-Dienst ist ausschließlich intern im
Docker-Netz erreichbar; Django ruft dessen feste HALO-Operationen mit einem
internen Token auf. Das öffentliche `/halo-upload/v1/` bleibt für HALOs Uploads
erhalten. Der Dashboard-SSH-Schlüssel ist auf die erlaubten HALO-Befehle
beschränkt; Schlüssel und Tokens liegen in `config/` außerhalb der
Versionsverwaltung.

Ein Administratorkonto bei einer neuen Installation anlegen:

```bash
docker compose exec pace-web python manage.py createsuperuser
```

Die HALO-Vorprüfung verlangt zwei Kameras, BNO085, BH1750, Thermalsensor,
synchronisierte Zeit und mindestens 5 GiB freien Speicher. Eine Aufnahme endet
nach spätestens 30 Minuten oder bei 4 GiB freiem Speicher geordnet. HALO
speichert Bilder vorübergehend; nach verifiziertem, quittiertem Upload bleiben
die verarbeiteten JPEGs auf SENSEs SSD. Unbestätigte Rohvideos und Bilder
bleiben auf HALO. Die UI zeigt Empfangsfortschritt und Synchronisationsdaten
auch für ältere Sessions.

Auf diesem SENSE-Pi fehlen die alten I²C-Sensoren. Deshalb starten die alten
Sensor-Worker derzeit nicht; die alte PACE-Weboberfläche ist dennoch verfügbar.
Die defekten Kamera-Steckverbinder auf HALO verhindern derzeit den
Live-Aufnahmetest und halten den Start-Knopf gesperrt.

Softwareprüfung:

```bash
docker compose exec -T pace-web python manage.py test data_cube.tests
curl --cacert config/certs/server.crt https://10.42.10.1:18443/healthz
```
