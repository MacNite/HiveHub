# HiveHub

> **Project renamed: HiveScale → HiveHub.** The project outgrew its original
> "dual scale" scope and now acts as a **data collector / hub for many different
> types of beehive sensors and scales** (up to 18 hives per ESP32), so the name
> was changed to match. A few internal identifiers — the database measurement
> columns, the OTA `target` value, the Docker image name (`…/hivescale-api`), the
> device's stored-config (NVS) namespace and MQTT topics — still use the old
> `hivescale` name on purpose: changing them would need data/firmware migrations
> and could strand existing measurements or deployed-device config. The
> third-party **beehivemonitoring.com "HiveScale"** wireless weight scale is an
> unrelated product and keeps its own name.

**This is very much a WIP — please do not order the PCBs as published now; they are not fully tested and are for development only.**

**HiveHub is an ESP32-based data collector for beehive sensors and scales.** It
gathers weight, temperature, humidity, sound, vibration, power state, and network
state from one or more hives (up to 18 per ESP32) and sends the readings to a
self-hosted FastAPI backend backed by PostgreSQL, where they can be displayed in
[HivePal](https://github.com/martinhrvn/hive-pal).

### Natively supported sensors

Every sensor is optional and compiled in per device — HiveHub reads the
following directly on the ESP32:

- **SHT4x / SHT40** — ambient temperature and humidity (I2C).
- **DS18B20** — per-hive in-hive temperature probes (1-Wire).
- **INMP441** — in-hive sound via I2S MEMS microphones with per-band FFT.
- **INA219** — solar / load voltage, current, and power telemetry (I2C).
- **MAX17048** — LiPo battery voltage, state-of-charge, and low-battery alerts (I2C).
- **Wired load cells via HX711 or NAU7802** — the hive scales themselves; NAU7802
  over I2C (optionally behind a TCA9548A mux) scales to many channels for
  multi-hive setups.

On top of these wired sensors, HiveHub also bridges a range of wireless BLE/GATT
sensors and scales (HolyIot, RuuviTag, HiveInside, and beehivemonitoring.com
HiveHeart / HiveScale devices) — see [Features](#features) below.

> 🌐 **Website & setup guide:** a small static site lives in [`website/`](website/) — a
> feature overview, a step-by-step setup guide, and an in-browser
> [`secrets.h` configurator](website/configurator.html) that builds your firmware
> config (sensors, BLE/GATT options, power modules) without hand-editing macros.
> It deploys to GitHub Pages via `.github/workflows/pages.yml`
> (enable **Settings → Pages → Source: GitHub Actions**), e.g.
> `https://macnite.github.io/HiveHub/`.

---

## Features

Every sensor is optional and compiled in per device — start with weight and add the rest.

- **Dual load cells** — two HX711 amplifiers drive two independent hive scales (the one always-on measurement).
- **Backend load-cell temperature compensation** — corrects HX711 thermal drift on read from the stored raw values; see [docs/temperature-compensation.md](docs/temperature-compensation.md).
- **Per-hive temperature** — optional DS18B20 probes on a shared 1-Wire bus (off by default), or an in-hive BLE sensor as the source.
- **Per-hive in-hive sound** — optional INMP441 stereo I2S microphones with per-band FFT (off by default).
- **Per-hive vibration** — from a paired in-hive BLE sensor (a HiveInside ESP32-C6 gives full FFT bands; a HolyIot/RuuviTag beacon gives a low-rate magnitude), capturing the ~20 Hz pre-swarm signal microphones miss. A legacy wired LIS3DH/LIS2DH12 driver is retained for custom builds.
- **In-hive BLE sensors** — pair up to two battery beacons (HolyIot 25015, RuuviTag, HiveInside ESP32-C6, or beehivemonitoring.com HiveHeart) for temperature/humidity/pressure/vibration with no wiring.
- **Ambient temperature & humidity** — an SHT4x on I2C.
- **RTC timekeeping** — a DS3231 timestamps measurements without depending on NTP.
- **SD card cache & backup** — local buffering when uploads fail, plus an append-only persistent backup that can be downloaded in AP mode and re-imported via HivePal.
- **Claim-code pairing** — claim devices from HivePal without manual database setup.
- **Remote configuration & commands** — sampling interval, scale offsets/factors, calibration, OTA checks, provisioning, reboot, Wi-Fi reset, and factory reset.
- **OTA firmware updates** — owner-scoped releases with an accept-to-apply gate; the device also relays firmware to a BeeCounter (over I2C) and a HiveInside sensor (over BLE GATT).
- **Wi-Fi provisioning portal** — opened by the setup button for field configuration, including pairing wireless sensors.
- **Multi-network Wi-Fi** — up to three saved networks.
- **Insights** — backend auto-evaluation of weight, temperature, sound, vibration, and entrance traffic per hive, based on [these publications](docs/insights-sources-tldr.md).
- **Optional off-grid mode** — solar/LiPo charging with INA219 solar telemetry and MAX17048 LiPo telemetry.
- **Optional entrance bee counters** — wired [BeeCounter](https://github.com/MacNite/2026-easy-bee-counter) (I2C) or wireless HiveTraffic (BLE/GATT).
- **[HivePal](https://github.com/martinhrvn/hive-pal) integration** — dedicated `/api/v1/app/...` endpoints using a HivePal service key, per-user JWTs, and per-user access roles.
- **Optional MQTT bridge** — mirror every reading to an MQTT broker (Home Assistant, Node-RED, openHAB…) with Home Assistant auto-discovery, alongside the built-in PostgreSQL store. Off by default; see [MQTT / Home Assistant integration](#mqtt--home-assistant-integration).
- **Breakout PCB design** — KiCad Scale Module + Power Module with fabrication outputs.
- **Docker Compose deployment** — the API and PostgreSQL database.

---

## Repository structure

```text
HiveHub/
├── firmware/                   # ESP32 PlatformIO project (src/, include/)
├── server/                     # Python FastAPI backend, insights, migrations
├── docker/                     # Docker Compose deployment
├── website/                    # Static site + secrets.h configurator (GitHub Pages)
├── docs/                       # Hardware, API, deployment, and feature docs
├── pcb-design/                 # KiCad breakout PCB design and fabrication files
├── test-data/                  # Mock server and decoder/insight unit tests
└── .github/workflows/          # CI: backend image build + website Pages deploy
```

---

## Hardware

### Core components

All links are affiliate links and support this project directly.

| Component | Role |
|---|---|
| [ESP32 Dev Board](https://s.click.aliexpress.com/e/_c3LV3nfF)| Main controller |
| 2x [HX711](https://s.click.aliexpress.com/e/_c3DkGsAN) + [load cells](https://s.click.aliexpress.com/e/_c33VsCl7) | Weight measurement for scale 1 and scale 2 |
| [SHT4x](https://s.click.aliexpress.com/e/_c3CvaIKz) | Ambient temperature and humidity |
| [DS3231 RTC](https://s.click.aliexpress.com/e/_c4mfPBtR) | Offline timekeeping |
| [MicroSD card module](https://s.click.aliexpress.com/e/_c3oDcFM9) + card | Local cache and backup storage |
| [Momentary pushbutton](https://s.click.aliexpress.com/e/_c4sqg7Lx) | Provisioning and factory reset |
| 3.3 V power supply with at least 1 A / or Power Module | ESP32 and peripheral supply |
| [IP-rated enclosure](https://s.click.aliexpress.com/e/_c30msn9R), [glands](https://de.aliexpress.com/item/1005007921366362.html), frame hardware | Outdoor installation |

### Optional sensors (enable per device in `secrets.h`)

| Component | Firmware flag | Role |
|---|---|---|
| 2x [DS18B20 waterproof probes](https://s.click.aliexpress.com/e/_c4X4ktmv) | `ENABLE_DS18B20_HIVE_TEMP` | Internal hive temperature (or use an in-hive BLE sensor) |
| 2x [INMP441 sound sensors](https://s.click.aliexpress.com/e/_c313NoAd) | `ENABLE_INMP441_MICS` | Internal hive sound with per-band FFT |
| [INA219](https://s.click.aliexpress.com/e/_c3LAZEO9) | `ENABLE_INA219_SOLAR` | Solar/load voltage, current, and power telemetry |
| [MAX17048](https://s.click.aliexpress.com/e/_c3JKEzrL) | `ENABLE_MAX17048_BATTERY` | LiPo voltage, state-of-charge, and low-battery alert |
| In-hive BLE sensor (HolyIot 25015 / RuuviTag / HiveInside / HiveHeart) | `ENABLE_BLE_SCAN`, `ENABLE_BEEHIVE_GATT` | Temp / humidity / pressure / vibration, no wiring — paired by MAC |
| 2x LIS3DH (proto) / LIS2DH12TR (final) | `ENABLE_LIS3DH_ACCEL` | **Legacy** wired vibration driver (in-hive vibration now comes from a BLE sensor) |
| [CN3791 solar charger](https://s.click.aliexpress.com/e/_c4T7Ve5x) · [TPS63020 buck-boost](https://s.click.aliexpress.com/e/_c2uscIy1) · [TP4056](https://s.click.aliexpress.com/e/_c4beU1nL) · [10 Ah LiPo](https://s.click.aliexpress.com/e/_c45jfAGv) · [6 V 4.5 W solar panel](https://s.click.aliexpress.com/e/_c3njKuVF) | Hardware only | Off-grid power path used by the Power Module / breakout PCB |

### Optional bee counters

Entrance traffic counting (in/out bees) is available wired over I2C or wireless over BLE:

- **[BeeCounter](https://github.com/MacNite/2026-easy-bee-counter)** — wired I2C counter at `0x30` / `0x31`.
- **HiveTraffic** — wireless BLE/GATT counter; see [docs/hivetraffic-bee-counter.md](docs/hivetraffic-bee-counter.md).

### Firmware pin mapping

Pins are defined in `firmware/include/config.h` (with optional per-device overrides in `secrets.h`). The firmware source is split into focused units under `firmware/src/` (`main.cpp`, `hivescale_network.cpp`, `portal.cpp`, `sensors.cpp`, `mics.cpp`, `accel.cpp`, `ble_sensor.cpp`, `beehive_gatt.cpp`, `bee_counter_client.cpp`, `storage_power.cpp`, `device_prefs.cpp`, `globals.cpp`).

| Signal | GPIO | Notes |
|---|---:|---|
| HX711 #1 DOUT / SCK | 16 / 17 | Scale 1; SCK held high during deep sleep to power down the HX711 |
| HX711 #2 DOUT / SCK | 32 / 33 | Scale 2; SCK held high during deep sleep to power down the HX711 |
| DS18B20 1-Wire data | 4 | Shared bus for both probes; 4.7 kΩ pull-up to 3.3 V (`ENABLE_DS18B20_HIVE_TEMP`) |
| I2C SDA / SCL | 21 / 22 | RTC, SHT4x, BeeCounter, optional INA219 / MAX17048 |
| SD CS / SCK / MISO / MOSI | 5 / 18 / 23 / 19 | MicroSD over SPI |
| Setup button | 27 | `INPUT_PULLUP`; short press opens provisioning AP, long press factory resets |
| INMP441 BCLK / WS / SD | 14 / 13 / 34 | I2S, shared by both mics; GPIO34 is input-only (`ENABLE_INMP441_MICS`) |
| BeeCounter | 21 / 22 | I2C at `0x30` / `0x31` |
| LIS3DH / LIS2DH12 (legacy) | 21 / 22 | I2C at `0x18` / `0x19` (`ENABLE_LIS3DH_ACCEL`) |

> See [docs/wiring.md](docs/wiring.md) for detailed wiring and [pcb-design/README.md](pcb-design/README.md) for the KiCad breakout PCB pinout.

---

## Firmware setup

### Prerequisites

- PlatformIO (VS Code extension or CLI).

### Configuration

```bash
cp firmware/include/secrets.example.h firmware/include/secrets.h
```

Edit `firmware/include/secrets.h`:

```cpp
#define DEVICE_ID           "hive-001"
#define API_KEY             "your-api-key-here"   // unique per device — see note below
#define CLAIM_CODE          "ABCD-1234"
#define CLAIM_CODE_REVISION 1
#define API_BASE_URL        "https://your-backend-domain.com"   // HTTPS required (TLS is verified)

#define WIFI1_SSID          "your-wifi-ssid-1"
#define WIFI1_PASS          "your-wifi-password-1"
// WIFI2_* and WIFI3_* are optional fallbacks
```

Values in `secrets.h` seed the device's persistent `Preferences` on first boot. Later changes are usually made through the backend or provisioning portal. Set `FORCE_RESEED true` only when you intentionally want to overwrite stored preferences from the build file.

> **Per-device API key:** give each device its own unique `API_KEY` (generate one
> with `openssl rand -hex 32`). The backend registers the key against the device's
> `device_id` on first contact and rejects mismatches afterwards, so a leaked key
> only affects that one device. It no longer has to match the server's `API_KEY`
> environment variable — that value is now only the master/admin key for tooling.

### TLS / certificate verification

The firmware verifies the backend's TLS certificate. It ships the ISRG Root X1
(Let's Encrypt) root CA in `firmware/include/ca_cert.h` and syncs the clock over
NTP after connecting so validity can be checked. Therefore:

- The backend must be reachable over **HTTPS with a valid certificate** (a reverse proxy with Let's Encrypt is the simplest setup).
- For a CA other than Let's Encrypt, replace the certificate in `firmware/include/ca_cert.h` (instructions are in that file).
- NTP (UDP port 123) must be reachable from the device's network.

### Optional modules

Optional sensors are enabled per build in `secrets.h`. The defaults shipped in `secrets.example.h`:

```cpp
#define ENABLE_DS18B20_HIVE_TEMP 0   // wired in-hive temperature probes (default off)
#define ENABLE_INMP441_MICS      0   // stereo I2S mics + per-band FFT (default off)
#define ENABLE_BLE_SCAN          1   // in-hive BLE sensor bridge — HolyIot/RuuviTag/HiveInside (default on)
#define ENABLE_BEEHIVE_GATT      0   // beehivemonitoring.com HiveHeart / HiveScale over GATT (default off)
#define ENABLE_INA219_SOLAR      0   // solar/load telemetry (default off)
#define ENABLE_MAX17048_BATTERY  0   // LiPo fuel-gauge telemetry (default off)
```

The [secrets.h configurator](website/configurator.html) writes these (and the wireless-sensor macros) for you.

> Cellular (SIM7080G) transport is no longer part of this firmware. LTE, solar,
> and battery handling now live on a separate **Power Module** that connects to
> the Scale Module over I2C/ESP-NOW. The ESP32 firmware itself is Wi-Fi only.

### Flash

```bash
cd firmware
pio run --target upload
pio device monitor   # 115200 baud
```

### PlatformIO dependencies

`platformio.ini` installs the required libraries automatically:

- `bogde/HX711`, `paulstoffregen/OneWire`, `milesburton/DallasTemperature`
- `adafruit/Adafruit SHT4x Library`, `adafruit/RTClib`, `bblanchon/ArduinoJson`
- `kosme/arduinoFFT` — per-band FFT for the INMP441 mics and the vibration bands
- `h2zero/NimBLE-Arduino` (2.x) — the in-hive BLE sensor bridge and GATT clients

Optional libraries are commented out; uncomment them when the matching flag is set:

- `adafruit/Adafruit INA219` — `ENABLE_INA219_SOLAR`
- `sparkfun/SparkFun MAX1704x Fuel Gauge Arduino Library` — `ENABLE_MAX17048_BATTERY`

---

## Wi-Fi provisioning portal

Press the setup button on GPIO27 to manage field configuration without reflashing.

| Action | Result |
|---|---|
| Short press | Starts `HiveHub-Setup-XXXX` AP; open `http://192.168.4.1` |
| Long press, 10 seconds | Clears stored Preferences and reboots |

The portal edits Wi-Fi networks, backend URL, device ID, claim code, API settings, and the **Wireless sensors** list — add up to six BLE sensors (at most two in-hive, two scales, two bee counters), choosing each one's type and MAC, or scan for nearby devices. It also offers an **SD-card download** (TAR) of the on-device backup. It closes automatically after 10 minutes. See [docs/ap-mode-sd-download.md](docs/ap-mode-sd-download.md).

---

## Server setup

### Docker Compose

```bash
cd docker
cp .env.example .env
# edit API_KEY, HIVEPAL_SERVICE_API_KEY, HIVEPAL_JWT_SECRET, database password, and volume paths
docker compose up -d
```

The API listens on port `31115` by default.

| Setting | Default |
|---|---|
| API image | `ghcr.io/macnite/hivescale-api:latest` |
| API port | `31115` |
| Database image | `postgres:16-alpine` |
| Database name/user | `hivescale` |

Change `API_KEY`, `HIVEPAL_SERVICE_API_KEY`, `HIVEPAL_JWT_SECRET`, and the PostgreSQL password before exposing the service. See [docs/docker-install.md](docs/docker-install.md) and [docs/truenas-install.md](docs/truenas-install.md) for full guides.

### Manual / local

```bash
cd server
pip install -r requirements.txt
DATABASE_URL="postgresql://hivescale:password@localhost:5432/hivescale" \
API_KEY="your-master-admin-key" \
HIVEPAL_SERVICE_API_KEY="your-hivepal-service-key" \
HIVEPAL_JWT_SECRET="must-match-hivepal-jwt-secret" \
PUBLIC_BASE_URL="https://your-domain.example.com" \
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `API_KEY` | Yes | Master/admin key in `X-API-Key` for server-to-server tooling (firmware-release registration, command queueing, latest-measurements, time). Devices use their own per-device keys, registered on first contact. |
| `HIVEPAL_SERVICE_API_KEY` | Yes, for HivePal | Service key sent by HivePal in `X-HivePal-Service-Key` |
| `HIVEPAL_JWT_SECRET` | Yes, for HivePal | Shared HS256 secret used to verify the per-user `Authorization: Bearer` tokens HivePal sends. Must match HivePal's `JWT_SECRET`. |
| `PUBLIC_BASE_URL` | Recommended | Public base URL used for OTA firmware download links |
| `FIRMWARE_DIR` | Optional | Firmware binary directory, default `/app/firmware` |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | Optional | DB connection pool bounds (default `1` / `10`) |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_DEFAULT` | Optional | Per-client-IP rate limit (default on, `120/minute`) |
| `MAX_BODY_BYTES` / `MAX_FIRMWARE_BYTES` | Optional | Request-body and firmware-upload size caps (default 256 KiB / 16 MiB) |
| `INSIGHTS_RECONCILE_*` | Optional | Background insight-history reconciliation (see `server/.env.example`) |
| `MQTT_*` | Optional | MQTT bridge to Home Assistant / Node-RED / openHAB (off by default — see [MQTT / Home Assistant integration](#mqtt--home-assistant-integration)) |
| `TZ` | Optional | Server timezone, for example `Europe/Berlin` |

The backend auto-creates tables and runs idempotent `ALTER TABLE` statements on startup; the SQL files in `server/migrations/` can also be applied manually.

---

## MQTT / Home Assistant integration

The backend can **optionally** mirror every measurement to an MQTT broker in
addition to storing it in PostgreSQL — so HiveHub data flows into
[Home Assistant](https://www.home-assistant.io/), Node-RED, openHAB or any MQTT
consumer. It is **off by default** and purely additive: the bridge runs in a
background thread and is fail-soft, so a broker outage never affects ingestion
or the API.

Enable it by setting `MQTT_ENABLED=true` and pointing `MQTT_HOST` at your broker
(see `server/.env.example` / `docker/env.example` for the full list):

```bash
MQTT_ENABLED=true
MQTT_HOST=192.168.1.10        # your broker (e.g. the Mosquitto add-on)
MQTT_PORT=1883
MQTT_USERNAME=hivescale       # optional
MQTT_PASSWORD=...             # optional
MQTT_HA_DISCOVERY=true        # auto-create Home Assistant entities
```

### Topics

With `MQTT_BASE_TOPIC=hivescale` (the default):

| Topic | Payload |
|---|---|
| `hivescale/bridge/availability` | `online` / `offline` (bridge last-will) |
| `hivescale/<device_id>/availability` | `online` / `offline` (per device) |
| `hivescale/<device_id>/state` | Retained JSON of the latest reading (every non-null measurement field) |

### Home Assistant

When `MQTT_HA_DISCOVERY=true`, the bridge publishes
[MQTT-discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
configs the first time it sees each device. Home Assistant then automatically
creates one device per scale with a curated set of sensors — scale weights,
hive/ambient temperature & humidity, battery voltage/charge, solar power, Wi-Fi
signal, and bee-counter totals. All other fields remain available in the raw
`state` JSON for custom templated sensors. Just make sure the
[MQTT integration](https://www.home-assistant.io/integrations/mqtt/) is set up
and pointed at the same broker.

---

## API overview

Interactive Swagger docs are at `http://<host>:31115/docs`. See [docs/api.md](docs/api.md) for the full reference and schemas.

### Device-facing endpoints

Require the `X-API-Key` header (per-device key, except where noted as master/admin).

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check (no auth) |
| `GET` | `/api/v1/time` | UTC server time for RTC sync (master/admin) |
| `POST` | `/api/v1/measurements` | Submit a measurement (incl. optional telemetry) |
| `GET` | `/api/v1/measurements/latest` | Latest measurements for dashboards (master/admin) |
| `GET`/`PATCH` | `/api/v1/devices/{id}/config` | Get / update device configuration |
| `GET` | `/api/v1/devices/{id}/firmware` | Check for a firmware update (`?version=` & `?target=hivescale\|beecounter`) |
| `POST` | `/api/v1/firmware/releases` | Register a firmware release (master/admin) |
| `GET` | `/firmware/{filename}` | Download a firmware binary |
| `POST` | `/api/v1/devices/{id}/commands` | Queue a remote command (master/admin) |
| `POST` | `/api/v1/devices/{id}/commands/update-beecounter` | Queue a BeeCounter OTA relay (`?slot=1\|2`) |
| `POST` | `/api/v1/devices/{id}/commands/update-hiveinside` | Queue a HiveInside OTA relay over BLE (`?slot=1\|2`) |
| `GET` | `/api/v1/devices/{id}/commands/next` | Claim next pending command |
| `POST` | `/api/v1/devices/{id}/commands/{cmd_id}/result` | Report command result |

### App endpoints for HivePal

Require both `X-HivePal-Service-Key` and a per-user `Authorization: Bearer <hivepal-jwt>` token (verified with `HIVEPAL_JWT_SECRET`).

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/app/devices/claim` | Claim a device by claim code |
| `GET`/`DELETE` | `/api/v1/app/devices` · `/{id}` | List devices · remove own membership |
| `GET`/`PATCH` | `/api/v1/app/devices/{id}/config` | Get / update config (incl. `tempco_*`) |
| `GET`/`PATCH` | `/api/v1/app/devices/{id}/channels` | List / update scale display names |
| `GET` | `/api/v1/app/devices/{id}/measurements[/latest]` | Measurements (with date filter) / latest |
| `POST` | `/api/v1/app/devices/{id}/measurements/import` | Bulk-import SD-card backup rows (idempotent) |
| `POST` | `/api/v1/app/devices/{id}/temp-compensation/fit` | Fit a load-cell temperature coefficient |
| `GET`/`POST`/`DELETE` | `/api/v1/app/devices/{id}/members[...]` | List / share / revoke device access |
| `POST` | `/api/v1/app/devices/{id}/calibration/start` · `/stop` | Start / stop calibration mode |
| `POST` | `/api/v1/app/devices/{id}/firmware` | Upload a firmware binary (multipart) and register it |
| `GET`/`POST` | `/api/v1/app/devices/{id}/firmware/status` · `/approve` | OTA status / accept-to-apply approval |
| `POST` | `/api/v1/app/devices/{id}/commands/update-beecounter` · `/update-hiveinside` | Trigger a sub-device OTA relay |
| `GET` | `/api/v1/app/devices/{id}/insights[/summary\|/history]` | Rule-based colony insights, summary, and history |

---

## Measurement payload highlights

Core fields include weights, hive/ambient temperatures and humidity, raw HX711 values, firmware/config version, sensor status, boot count, and time source. Builds with optional hardware can also send:

- **Acoustic (INMP441):** `mic_ok`, per-channel `mic_left_*` / `mic_right_*` RMS/peak levels and per-band FFT energy (`*_band_sub_bass_dbfs`, `*_band_hum_dbfs`, `*_band_piping_dbfs`, `*_band_stress_dbfs`, `*_band_high_dbfs`).
- **In-hive BLE sensor:** `hive_N_humidity_percent`, `ble_N_humidity_percent`, `ble_N_pressure_hpa`, and the beehivemonitoring.com `hiveheart_N_*` / `hivescale_N_*` fields.
- **Vibration:** `accel_N_ok`, broadband `accel_N_rms_mg` / `accel_N_peak_mg`, and per-band energy `accel_N_band_swarm_mg` (8–30 Hz) / `_fanning_mg` (30–100 Hz) / `_activity_mg` (100–200 Hz) — populated by the in-hive BLE sensor (or the legacy wired driver).
- **Entrance traffic (BeeCounter / HiveTraffic):** `bee_counter_1_*` / `bee_counter_2_*` totals, interval in/out counts, gate health, and status fields.
- **Power telemetry:** `solar_*` (INA219) and `battery_*` (MAX17048) fields.
- **Status:** `network_transport`, `calibration_mode`, `boot_count`, `time_source`.

The backend also accepts `cellular_ok` / `cellular_csq` for the future Power Module; on-device firmware reports `network_transport: "wifi"`. Fields are stored in dedicated PostgreSQL columns (plus `raw_json`) and returned through the latest-measurements and HivePal app APIs.

---

## Claim-code pairing

1. Set `CLAIM_CODE` in `secrets.h` before flashing, for example `ABCD-1234`.
2. The firmware includes the claim code in measurements until its first successful upload, then stops sending it to limit exposure. (Bumping `CLAIM_CODE_REVISION` makes it send the new code once more.)
3. The backend stores a hash of the claim code and creates an unclaimed device record on the first measurement.
4. HivePal (or another app client) calls `POST /api/v1/app/devices/claim` with the code.
5. The matched device is assigned to the user as `owner`.

To push a new claim code through OTA, change `CLAIM_CODE`, increment `CLAIM_CODE_REVISION`, and publish a new firmware build.

---

## Remote commands

Commands are queued by the server and picked up by the device on its next cycle.

| Command type | Payload | Description |
|---|---|---|
| `calibrate_scale_1` / `calibrate_scale_2` | `{"known_weight_kg": 10.0}` | Calibrate a scale with a known weight |
| `start_calibration_mode` | `{"interval_seconds": 5, "timeout_seconds": 600}` | Temporarily use fast measurement cycles |
| `stop_calibration_mode` | `{}` | Return to normal interval and deep sleep |
| `reboot` | `{}` | Restart the ESP32 |
| `check_ota` / `ota_update` | `{}` | Trigger an immediate OTA check |
| `update_beecounter` | `{"slot": 1, "url": "...", "version": "...", "crc32": 123}` | Relay firmware to a BeeCounter over I2C (usually via the `update-beecounter` helper) |
| `update_hiveinside` | `{"slot": 1, "url": "...", "version": "...", "crc32": 123}` | Relay firmware to a HiveInside sensor over BLE GATT (usually via the `update-hiveinside` helper) |
| `start_provisioning` | `{}` | Start the Wi-Fi provisioning AP |
| `reset_wifi` | `{}` | Clear saved Wi-Fi credentials and reboot |
| `reset_preferences` / `factory_reset` | `{}` | Clear all Preferences and reboot |

---

## PCB design

The `pcb-design/` directory contains the KiCad design, split into two boards:

- **Scale Module** — the central board. It accepts off-the-shelf modules on pin headers (no SMD soldering): ESP32, both HX711 amplifiers, load-cell terminals, I2C sensors (RTC, SHT40), SD module, two INMP441 microphones, and the BeeCounter.
- **Power Module** — handles power and connectivity (solar, battery, LTE) and connects to the Scale Module over I2C/ESP-NOW.

Start with [pcb-design/README.md](pcb-design/README.md) for the connector pinout, design intent, fabrication files, and assembly notes. The current PCB is an early revision and should be prototyped before field deployment.

---

## Documentation

A full index is in [docs/README.md](docs/README.md). Highlights:

- [docs/wiring.md](docs/wiring.md) — full wiring reference.
- [docs/api.md](docs/api.md) — complete API reference.
- [docs/insights.md](docs/insights.md) — rule-based colony insights and detector catalogue.
- [docs/holyiot-ble-sensor.md](docs/holyiot-ble-sensor.md) · [docs/ruuvitag-ble-sensor.md](docs/ruuvitag-ble-sensor.md) · [docs/beehivemonitoring-gatt.md](docs/beehivemonitoring-gatt.md) — in-hive BLE sensors.
- [docs/hivetraffic-bee-counter.md](docs/hivetraffic-bee-counter.md) — wireless entrance counter.
- [docs/temperature-compensation.md](docs/temperature-compensation.md) — load-cell drift correction.
- [docs/calibration-mode.md](docs/calibration-mode.md) · [docs/ap-mode-sd-download.md](docs/ap-mode-sd-download.md) · [docs/offgrid-firmware-notes.md](docs/offgrid-firmware-notes.md) — operation.
- [docs/docker-install.md](docs/docker-install.md) · [docs/truenas-install.md](docs/truenas-install.md) · [docs/test-commands.md](docs/test-commands.md) — deployment & testing.

---

## License

MIT © 2026 Maximilian Nitschke
