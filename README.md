# HiveHub

> **Project renamed: HiveScale → HiveHub.** The project outgrew its original
> "dual scale" scope and now acts as a **data collector / hub for many different
> types of beehive sensors and scales** (up to 16 hives per ESP32), so the name
> was changed to match. A few internal identifiers — the database measurement
> columns, the OTA `target` value, the Docker image name (`…/hivescale-api`), the
> device's stored-config (NVS) namespace and MQTT topics — still use the old
> `hivescale` name on purpose: changing them would need data/firmware migrations
> and could strand existing measurements or deployed-device config. The
> third-party **beehivemonitoring.com "HiveScale"** wireless weight scale is an
> unrelated product and keeps its own name.

**Hardware status: all published PCBs are tested and working.** The recommended build is the **XIAO ESP32-C6 on the Scale Module V0.4** with **NAU7802**, **MAX17048**, **TPS63020**, **TP4056**, RTC, SD card and SHT40 — optionally expanded to **16 scales** with the NAU7802 breakout PCB (TCA9548A mux + up to 8× NAU7802). See [pcb-design/README.md](pcb-design/README.md). The old ESP32 30-pin board is obsolete and no longer recommended.

**HiveHub is an ESP32-based data collector for beehive sensors and scales.** It
gathers weight, temperature, humidity, sound, vibration, power state, and network
state from one or more hives (up to 16 per ESP32) and sends the readings to a
self-hosted FastAPI backend backed by PostgreSQL, where they can be displayed in
[HivePal](https://github.com/martinhrvn/hive-pal).

### Natively supported sensors

Every sensor is optional and compiled in per device — HiveHub reads the
following directly on the ESP32:

- **SHT4x / SHT40** — ambient temperature and humidity (I2C).
- **DS18B20** — per-hive in-hive temperature probes (1-Wire).
- **INMP441** — in-hive sound via I2S MEMS microphones with per-band FFT.
- **MAX17048** — LiPo battery voltage, state-of-charge, and low-battery alerts (I2C).
- **Wired load cells via HX711 or NAU7802** — the hive scales themselves; NAU7802
  over I2C (optionally behind a TCA9548A mux) scales to many channels for
  multi-hive setups.

On top of these wired sensors, HiveHub also bridges a range of wireless BLE/GATT
sensors and scales (HolyIot, RuuviTag, HiveInside, and beehivemonitoring.com
HiveHeart / HiveScale devices) — see [Features](#features) below.

> 🌐 **Website & setup guide:** a small static site lives in [`website/`](website/) — a
> feature overview, a step-by-step setup guide, a complete
> ["Build your own" guide](website/build.html) (BOM with price estimates, wiring,
> firmware, backend — downloadable as PDF), and an in-browser
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
- **Per-hive vibration** — from a paired in-hive BLE sensor (a HiveInside ESP32-C6 gives full FFT bands; a HolyIot/RuuviTag beacon gives a low-rate magnitude), capturing the ~20 Hz pre-swarm signal microphones miss. (The old wired LIS3DH/LIS2DH12 driver has been removed — in-hive vibration is BLE-only.)
- **In-hive BLE sensors** — pair up to two battery beacons (HolyIot 25015, RuuviTag, HiveInside ESP32-C6, or beehivemonitoring.com HiveHeart) for temperature/humidity/pressure/vibration with no wiring.
- **Ambient temperature & humidity** — an SHT4x on I2C.
- **RTC timekeeping** — a DS3231 timestamps measurements without depending on NTP.
- **SD card cache & backup** — local buffering when uploads fail, plus an append-only persistent backup that can be downloaded in AP mode and re-imported via HivePal.
- **Claim-code pairing** — claim devices from HivePal without manual database setup.
- **Remote configuration & commands** — sampling interval, scale offsets/factors, calibration, OTA checks, provisioning, reboot, Wi-Fi reset, and factory reset.
- **OTA firmware updates** — owner-scoped releases with an accept-to-apply gate; the device also relays firmware to a HiveInside sensor (over BLE GATT). There is currently **no remote BeeCounter firmware-update path**: the old OTA-over-I2C relay was removed and a BeeCounter OTA over BLE/GATT is planned but not implemented yet.
- **Wi-Fi provisioning portal** — opened by the setup button for field configuration, including pairing wireless sensors.
- **Multi-network Wi-Fi** — up to three saved networks.
- **Insights** — backend auto-evaluation of weight, temperature, sound, vibration, and entrance traffic per hive, based on [these publications](docs/insights-sources-tldr.md).
- **Insight alert notifications (optional)** — get swarm / robbing / winter-risk alerts by **e-mail (SMTP)** and/or **Web Push** to your phone or an installable dashboard PWA (Android, iOS 16.4+, desktop) when an insight first fires or escalates. Off by default; see [Insight alert notifications](docs/notifications.md).
- **Optional off-grid mode** — solar/LiPo charging (CN3791 MPPT + TPS63020) with MAX17048 LiPo telemetry.
- **Optional entrance bee counters** — wireless [HiveTraffic](docs/hivetraffic-bee-counter.md) counters over **BLE/GATT only** (wired I2C BeeCounters are no longer supported).
- **Built-in web dashboard (optional)** — a login-free, dependency-free dashboard served from the backend at `/dashboard` for single-owner self-hosts: device dropdown, per-hive selection, charts for every data group, plus device-config editing, hive renaming, firmware/OTA and calibration controls. Off by default (`ENABLE_LOCAL_DASHBOARD`); see [server/dashboard/README.md](server/dashboard/README.md) and the [live demo](https://macnite.github.io/HiveHub/dashboard-demo/).
- **[HivePal](https://github.com/martinhrvn/hive-pal) integration** — dedicated `/api/v1/app/...` endpoints using a HivePal service key, per-user JWTs, and per-user access roles.
- **Optional MQTT bridge** — mirror every reading to an MQTT broker (Home Assistant, Node-RED, openHAB…) with Home Assistant auto-discovery, alongside the built-in PostgreSQL store. Off by default; see [MQTT / Home Assistant integration](#mqtt--home-assistant-integration).
- **PCB designs (tested and working)** — KiCad ESP32-C6 Scale Module (recommended) and NAU7802 breakout board with fabrication outputs, plus the Power Module and legacy 30-pin Scale Module.
- **Docker Compose deployment** — the API and PostgreSQL database.

---

## Repository structure

```text
HiveHub/
├── firmware/                   # ESP32 PlatformIO project (src/, include/)
├── server/                     # Python FastAPI backend, insights, migrations
│   └── dashboard/              # Built-in login-free web dashboard (served at /dashboard)
├── docker/                     # Docker Compose deployment
├── website/                    # Static site + secrets.h configurator (GitHub Pages)
│   └── dashboard-demo/         # Backend-free dashboard demo (sample data)
├── docs/                       # Hardware, API, deployment, and feature docs
├── pcb-design/                 # KiCad breakout PCB design and fabrication files
├── test-data/                  # Mock server and decoder/insight unit tests
└── .github/workflows/          # CI: backend image build + website Pages deploy
```

---

## Hardware

### Core components

All links are affiliate links and support this project directly.

The recommended build is the **XIAO ESP32-C6 on the Scale Module V0.4** — the complete
bill of materials with price estimates and shop links lives on the
[**Build your own** page](website/build.html) (also downloadable as
[CSV](website/assets/hivehub-bom.csv)).

| Component | Role |
|---|---|
| Seeed Studio XIAO ESP32-C6 | Main controller (**recommended** — plugs into the Scale Module V0.4 PCB) |
| NAU7802 (I2C, on the Scale Module / breakout PCB) + [load cells](https://s.click.aliexpress.com/e/_c33VsCl7) | Weight measurement (**recommended** — 2 scales on the Scale Module, up to 16 via the NAU7802 breakout PCB with its TCA9548A mux + up to 8× NAU7802) |
| [SHT4x / SHT40](https://s.click.aliexpress.com/e/_c3CvaIKz) | Ambient temperature and humidity |
| [DS3231 RTC](https://s.click.aliexpress.com/e/_c4mfPBtR) | Offline timekeeping (remove its 4.7 kΩ I2C pull-ups — see the I2C pull-up note in [docs/wiring.md](docs/wiring.md)) |
| [MicroSD card module](https://s.click.aliexpress.com/e/_c3oDcFM9) + card | Local cache and backup storage |
| [MAX17048](https://s.click.aliexpress.com/e/_c3JKEzrL) | LiPo battery gauge (on the Scale Module) |
| [TPS63020 buck-boost](https://s.click.aliexpress.com/e/_c2uscIy1) | Battery/5 V input to the regulated 3.3 V rail |
| [TP4056 USB-C charger](https://s.click.aliexpress.com/e/_c4beU1nL) | LiPo charging |
| [Momentary pushbutton](https://s.click.aliexpress.com/e/_c4sqg7Lx) | Provisioning and factory reset |
| [IP-rated enclosure](https://s.click.aliexpress.com/e/_c30msn9R), [glands](https://de.aliexpress.com/item/1005007921366362.html), frame hardware | Outdoor installation |
| [ESP32 Dev Board](https://s.click.aliexpress.com/e/_c3LV3nfF) + 2x [HX711](https://s.click.aliexpress.com/e/_c3DkGsAN) | Legacy 30-pin build (obsolete — no longer recommended) |

### Optional sensors (enable per device in `secrets.h`)

| Component | Firmware flag | Role |
|---|---|---|
| 2x [DS18B20 waterproof probes](https://s.click.aliexpress.com/e/_c4X4ktmv) | `ENABLE_DS18B20_HIVE_TEMP` | Internal hive temperature (or use an in-hive BLE sensor) |
| 2x [INMP441 sound sensors](https://s.click.aliexpress.com/e/_c313NoAd) | `ENABLE_INMP441_MICS` | Internal hive sound with per-band FFT |
| [MAX17048](https://s.click.aliexpress.com/e/_c3JKEzrL) | `ENABLE_MAX17048_BATTERY` | LiPo voltage, state-of-charge, and low-battery alert |
| In-hive BLE sensor (HolyIot 25015 / RuuviTag / HiveInside / HiveHeart) | `ENABLE_BLE_SCAN`, `ENABLE_BEEHIVE_GATT` | Temp / humidity / pressure / vibration, no wiring — paired by MAC |
| [CN3791 solar charger](https://s.click.aliexpress.com/e/_c4T7Ve5x) · [10 Ah LiPo](https://s.click.aliexpress.com/e/_c45jfAGv) · [6 V 4.5 W solar panel](https://s.click.aliexpress.com/e/_c3njKuVF) | Hardware only | Off-grid charging path feeding the Scale Module's TPS63020 |

### Optional bee counters

Entrance traffic counting (in/out bees) is **BLE/GATT-only**:

- **HiveTraffic** — the wireless [2026-easy-bee-counter](https://github.com/MacNite/2026-easy-bee-counter) board; see [docs/hivetraffic-bee-counter.md](docs/hivetraffic-bee-counter.md). Pairable to any hive.
- Wired I2C BeeCounters (the old `0x30`/`0x31` slave path, including firmware updates over I2C) are **no longer supported**. BeeCounter firmware update over GATT is planned but not implemented yet, so there is currently no remote BeeCounter update path.

### Firmware pin mapping

Pins are defined in `firmware/include/config.h` (with optional per-device overrides in `secrets.h`). The firmware source is split into focused units under `firmware/src/` (`main.cpp`, `hivehub_network.cpp`, `portal.cpp`, `sensors.cpp`, `scale_bus.cpp`, `i2c_bus.cpp`, `hive_config.cpp`, `mics.cpp`, `ble_sensor.cpp`, `beehive_gatt.cpp`, `bee_counter_client.cpp`, `storage_power.cpp`, `device_prefs.cpp`, `status_led.cpp`, `globals.cpp`).

The table below is the **legacy 30-pin ESP32** map. The recommended **XIAO ESP32-C6** build (`pio run -e xiao_esp32c6`) has its own pin map — NAU7802 scales over I2C on D4/D5, DS18B20 on D1, SD on D3/D8–D10, button on D2, no HX711/INMP441 — see [docs/wiring.md](docs/wiring.md) and [pcb-design/README.md](pcb-design/README.md).

| Signal | GPIO | Notes |
|---|---:|---|
| HX711 #1 DOUT / SCK | 16 / 17 | Scale 1; SCK held high during deep sleep to power down the HX711 |
| HX711 #2 DOUT / SCK | 32 / 33 | Scale 2; SCK held high during deep sleep to power down the HX711 |
| DS18B20 1-Wire data | 4 | Shared bus for both probes; 4.7 kΩ pull-up to 3.3 V (`ENABLE_DS18B20_HIVE_TEMP`) |
| I2C SDA / SCL | 21 / 22 | RTC, SHT4x, NAU7802/TCA9548A, optional MAX17048 — shared bus at an explicit **100 kHz** |
| SD CS / SCK / MISO / MOSI | 5 / 18 / 23 / 19 | MicroSD over SPI |
| Setup button | 27 | `INPUT_PULLUP`; short press opens provisioning AP, long press factory resets |
| INMP441 BCLK / WS / SD | 14 / 13 / 34 | I2S, shared by both mics; GPIO34 is input-only (`ENABLE_INMP441_MICS`) |

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

Optional libraries:

- `sparkfun/SparkFun MAX1704x Fuel Gauge Arduino Library` — `ENABLE_MAX17048_BATTERY`

---

## Wi-Fi provisioning portal

Press the setup button (D2 on the XIAO ESP32-C6, GPIO27 on the legacy 30-pin board) to manage field configuration without reflashing.

| Action | Result |
|---|---|
| Short press | Starts `HiveHub-Setup-XXXX` AP; open `http://192.168.4.1` |
| Long press, 10 seconds | Clears stored Preferences and reboots |

The portal is organised **by hive**: it edits Wi-Fi networks, backend URL, device ID, claim code and API settings, and manages the hive registry — add a hive, pick its scale source (auto-detected NAU7802 channels or a wireless HiveScale) and one in-hive sensor (a DS18B20 probe by ROM or a BLE/GATT sensor by MAC, with a built-in **BLE scan**). It also offers live **scale calibration** (tare + known weight, fully offline) and an **SD-card download** (TAR) of the on-device backup. It closes automatically after 10 minutes. See [docs/multi-hive.md](docs/multi-hive.md) and [docs/ap-mode-sd-download.md](docs/ap-mode-sd-download.md).

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
| `ENABLE_LOCAL_DASHBOARD` | Optional | Serve the built-in login-free dashboard at `/dashboard` and the auth-free `/api/v1/local/*` read API (default off — single-owner self-hosts on a trusted LAN only) |
| `INSIGHTS_RECONCILE_*` | Optional | Background insight-history reconciliation (see `server/.env.example`) |
| `SMTP_*` / `NOTIFY_MIN_SEVERITY` | Optional | E-mail channel for insight alert notifications (off by default — see [Insight alert notifications](docs/notifications.md)) |
| `WEB_PUSH_ENABLED` / `VAPID_*` | Optional | Web Push channel for insight alert notifications (off by default — see [Insight alert notifications](docs/notifications.md)) |
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
signal, and bee-counter totals. Any in-hive wireless modules (a
beehivemonitoring.com HiveHeart/HiveScale or a HolyIOT BLE sensor) are exposed
as their own Home Assistant devices, nested under the hub, each with its own
battery and link-signal entities (and, for the HiveHeart, sound
frequency/energy/peak) — so a hive carrying several modules reports a distinct
signal and battery per device. All other fields remain available in the raw
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
| `GET` | `/api/v1/devices/{id}/firmware` | Check for a firmware update (`?version=`, `?target=hivescale`, `?board=`) |
| `POST` | `/api/v1/firmware/releases` | Register a firmware release (master/admin) |
| `GET` | `/firmware/{filename}` | Download a firmware binary |
| `POST` | `/api/v1/devices/{id}/commands` | Queue a remote command (master/admin) |
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
| `POST` | `/api/v1/app/devices/{id}/commands/update-hiveinside` | Trigger a HiveInside OTA relay |
| `GET` | `/api/v1/app/devices/{id}/insights[/summary\|/history]` | Rule-based colony insights, summary, and history |

### Local dashboard endpoints (optional, auth-free)

Enabled only when `ENABLE_LOCAL_DASHBOARD=true`. **No authentication** and **not scoped to a user** — they serve every device on the server, so keep them on a trusted LAN / behind your own reverse proxy. They power the built-in `/dashboard` UI and mirror the read paths above. When disabled, every route returns `404`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/local/devices` | List all devices + scale-channel names |
| `GET` | `/api/v1/local/devices/{id}/measurements[/latest]` | Measurements (date filter) / latest |
| `GET`/`PATCH` | `/api/v1/local/devices/{id}/config` | Read / update device config (interval, scale offsets/factors, temp comp) |
| `GET`/`PATCH` | `/api/v1/local/devices/{id}/channels` | Read / rename the scale-channel (hive) display names |
| `GET` | `/api/v1/local/devices/{id}/insights/summary` | Highest-severity insight summary |
| `GET`/`POST` | `/api/v1/local/devices/{id}/firmware/status` · (upload) · `/approve` | OTA status / upload binary / approve |
| `POST` | `/api/v1/local/devices/{id}/calibration/start` · `/stop` | Start / stop calibration mode |
| `POST` | `/api/v1/local/devices/{id}/temp-compensation/fit` | Fit a load-cell temperature coefficient |
| `GET` | `/api/v1/local/notifications/config` | Which alert channels are enabled + VAPID public key |
| `POST` | `/api/v1/local/notifications/subscribe` · `/unsubscribe` | Register / forget this browser's Web Push subscription |
| `POST` | `/api/v1/local/notifications/test` | Send a test alert over every enabled channel |

---

## Measurement payload highlights

Core fields include weights, hive/ambient temperatures and humidity, raw HX711 values, firmware/config version, sensor status, boot count, and time source. Builds with optional hardware can also send:

- **Acoustic (INMP441):** `mic_ok`, per-channel `mic_left_*` / `mic_right_*` RMS/peak levels and per-band FFT energy (`*_band_sub_bass_dbfs`, `*_band_hum_dbfs`, `*_band_piping_dbfs`, `*_band_stress_dbfs`, `*_band_high_dbfs`).
- **In-hive BLE sensor:** `hive_N_humidity_percent`, `ble_N_humidity_percent`, `ble_N_pressure_hpa`, and the beehivemonitoring.com `hiveheart_N_*` / `hivescale_N_*` fields.
- **Vibration:** `accel_N_ok`, broadband `accel_N_rms_mg` / `accel_N_peak_mg`, and per-band energy `accel_N_band_swarm_mg` (8–30 Hz) / `_fanning_mg` (30–100 Hz) / `_activity_mg` (100–200 Hz) — populated by the in-hive BLE sensor.
- **Entrance traffic (HiveTraffic BeeCounter, BLE/GATT):** `bee_counter_1_*` / `bee_counter_2_*` lifetime totals, gate health, and status fields; the backend derives the interval in/out counts by differencing consecutive totals.
- **Power telemetry:** `battery_*` (MAX17048) fields; the backend also still accepts the legacy `solar_*` fields.
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
| `update_hiveinside` | `{"slot": 1, "url": "...", "version": "...", "crc32": 123}` | Relay firmware to a HiveInside sensor over BLE GATT (usually via the `update-hiveinside` helper) |
| `start_provisioning` | `{}` | Start the Wi-Fi provisioning AP |
| `reset_wifi` | `{}` | Clear saved Wi-Fi credentials and reboot |
| `reset_preferences` / `factory_reset` | `{}` | Clear all Preferences and reboot |

---

## PCB design

The `pcb-design/` directory contains the KiCad designs — **all published boards are tested and working**:

- **ESP32-C6 Scale Module (V0.4, recommended)** — the central board. Off-the-shelf modules on pin headers (no SMD soldering): XIAO ESP32-C6, NAU7802 for 2 scales, DS3231 RTC, SHT40, SD module, DS18B20 bus, MAX17048 battery gauge, TP4056 USB-C charger, and TPS63020 buck-boost regulator with a power-source selection jumper.
- **NAU7802 breakout PCB (v0.2)** — I2C frontend for up to 16 wired scales (TCA9548A mux + up to 8× NAU7802; when it is used, no NAU7802 goes on the Scale Module); optionally carries its own XIAO MCU as a standalone BLE scale sensor.
- **Power Module (V0.3)** — off-grid power (solar, battery) for a Scale Module; probably discontinued soon.
- **ESP32 30-pin Scale Module (V0.3)** — the legacy board (ESP32 DevKit + 2× HX711 + INMP441 mics); obsolete and no longer recommended.

Start with [pcb-design/README.md](pcb-design/README.md) for the recommended setup, connector pinouts, fabrication files, and assembly notes.

---

## Documentation

A full index is in [docs/README.md](docs/README.md). Highlights:

- [docs/wiring.md](docs/wiring.md) — full wiring reference.
- [docs/api.md](docs/api.md) — complete API reference.
- [server/dashboard/README.md](server/dashboard/README.md) — the built-in web dashboard ([live demo](https://macnite.github.io/HiveHub/dashboard-demo/)).
- [docs/insights.md](docs/insights.md) — rule-based colony insights and detector catalogue.
- [docs/notifications.md](docs/notifications.md) — insight alert notifications by e-mail and Web Push.
- [docs/holyiot-ble-sensor.md](docs/holyiot-ble-sensor.md) · [docs/ruuvitag-ble-sensor.md](docs/ruuvitag-ble-sensor.md) · [docs/beehivemonitoring-gatt.md](docs/beehivemonitoring-gatt.md) — in-hive BLE sensors.
- [docs/hivetraffic-bee-counter.md](docs/hivetraffic-bee-counter.md) — wireless entrance counter.
- [docs/temperature-compensation.md](docs/temperature-compensation.md) — load-cell drift correction.
- [docs/calibration-mode.md](docs/calibration-mode.md) · [docs/ap-mode-sd-download.md](docs/ap-mode-sd-download.md) · [docs/offgrid-firmware-notes.md](docs/offgrid-firmware-notes.md) — operation.
- [docs/docker-install.md](docs/docker-install.md) · [docs/truenas-install.md](docs/truenas-install.md) · [docs/test-commands.md](docs/test-commands.md) — deployment & testing.

---

## License

MIT © 2026 Maximilian Nitschke
