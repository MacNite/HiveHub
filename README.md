# HiveScale

**this is very much a WIP - please do not order the PCBs as published now, they are not completely tested and for develpment only.**

**ESP32-based dual beehive scale system** for monitoring the weight, temperature, humidity, power state, and network state of two beehives. Measurements are sent to a self-hosted FastAPI backend backed by PostgreSQL and can be displayed in [HivePal](<https://github.com/martinhrvn/hive-pal>).

> 🌐 **Website & setup guide:** a small static site lives in [`website/`](website/) — a
> feature overview, a step-by-step setup guide, and an in-browser
> [`secrets.h` configurator](website/configurator.html) that builds your firmware
> config (sensors, BLE/GATT options, power modules) without hand-editing macros.
> It deploys to GitHub Pages via `.github/workflows/pages.yml`
> (enable **Settings → Pages → Source: GitHub Actions**), e.g.
> `https://macnite.github.io/HiveScale/`.

---

## Features

- **Dual load cells** using two HX711 amplifiers for two independent hive scales.
- **Per-hive temperature** using DS18B20 probes on a shared 1-Wire bus.
- **Per-hive sound level** using INMP441 microfones with I2S.
- **Per-hive vibration** using an optional LIS3DH / LIS2DH12 accelerometer per hive on I2C, capturing the low-frequency (~20 Hz) pre-swarm signal microphones miss.
- **Ambient temperature and humidity** using an SHT4x sensor on I2C.
- **RTC timekeeping** using a DS3231 so the device can timestamp measurements without depending on NTP.
- **SD card cache and backup** for local buffering when uploads fail and for persistent measurement backup.
- **Claim-code pairing** so devices can be claimed from HivePal without manual database setup.
- **Remote configuration** for sampling interval, scale offsets, calibration factors, and config versioning.
- **Remote commands** for calibration, OTA checks, provisioning, reboot, Wi-Fi reset, and factory reset.
- **OTA firmware updates** with server-side release registration.
- **Wi-Fi provisioning portal** opened by the setup button for field configuration.
- **Multi-network Wi-Fi** with up to three saved networks.
- **Insights** auto-evaluation of data (weight, temperature, sound, vibration) per hive based on [these publications](docs/insights-sources-tldr.md).
- **Optional off-grid mode** with solar lipo charging, INA219 solar telemetry, and MAX17048 LiPo telemetry.
- **Optional [BeeCounter](https://github.com/MacNite/2026-easy-bee-counter)** counting in- and outgoing bees.
- **Optional per-hive accelerometer** (LIS3DH / LIS2DH12 on I2C) for low-frequency comb vibration, including the ~20 Hz pre-swarm signal microphones miss. See [docs/accelerometer.md](docs/accelerometer.md).
- **[HivePal](<https://github.com/martinhrvn/hive-pal>) integration** through dedicated `/api/v1/app/...` endpoints using a HivePal service key and per-user access roles.
- **Breakout PCB design** in KiCad, including fabrication outputs.
- **Docker Compose deployment** for the API and PostgreSQL database.

---

## Repository structure

```text
HiveScale/
├── firmware/                   # ESP32 PlatformIO project
│   ├── src/main.cpp            # Main firmware source
├── server/                     # Python FastAPI backend and insights
├── docker/                     # Docker Compose deployment
├── docs/                       # Hardware, API, deployment, and test docs
├── pcb-design/                 # KiCad breakout PCB design and fabrication files
└── .github/workflows/          # CI: builds and pushes the backend image
```

---

## Hardware

### Core components

All links are affilliate links and support this project directly.

| Component | Role |
|---|---|
| [ESP32 Dev Board](https://s.click.aliexpress.com/e/_c3LV3nfF)| Main controller |
| 2x [HX711](https://s.click.aliexpress.com/e/_c3DkGsAN) + [load cells](https://s.click.aliexpress.com/e/_c33VsCl7) | Weight measurement for scale 1 and scale 2 |
| 2x [DS18B20 waterproof probes](https://s.click.aliexpress.com/e/_c4X4ktmv) | Internal hive temperature probes |
| 2x [INMP441 sound sensors](https://s.click.aliexpress.com/e/_c313NoAd)  | Internal hive sound sensors |
| [SHT4x](https://s.click.aliexpress.com/e/_c3CvaIKz) | Ambient temperature and humidity |
| [DS3231 RTC](https://s.click.aliexpress.com/e/_c4mfPBtR) | Offline timekeeping |
| [MicroSD card module](https://s.click.aliexpress.com/e/_c3oDcFM9) + card | Local cache and backup storage |
| [Momentary pushbutton](https://s.click.aliexpress.com/e/_c4sqg7Lx) | Provisioning and factory reset |
| 3.3 V power supply with at least 1A / or Power Module | ESP32 and peripheral supply |
| [IP-rated enclosure](https://s.click.aliexpress.com/e/_c30msn9R), [glands](https://de.aliexpress.com/item/1005007921366362.html?spm=a2g0o.order_list.order_list_main.181.95e75c5fEc35Ct&gatewayAdapt=glo2deu), frame hardware | Outdoor installation |

### Optional off-grid components

| Component | Firmware flag | Role |
|---|---|---|
| [INA219](https://s.click.aliexpress.com/e/_c3LAZEO9) | `ENABLE_INA219_SOLAR` | Solar/load voltage, shunt voltage, current, and power telemetry |
| [MAX17048](https://s.click.aliexpress.com/e/_c3JKEzrL) | `ENABLE_MAX17048_BATTERY` | LiPo voltage, state-of-charge, and low-battery alert |
| 2x LIS3DH (proto) / LIS2DH12TR (final) | `ENABLE_LIS3DH_ACCEL` | Per-hive low-frequency comb vibration (8–30 Hz swarm band, fanning, activity) at I2C `0x18` / `0x19` |
| [CN3971 / solar charger module](https://s.click.aliexpress.com/e/_c4T7Ve5x) | Hardware only | Solar charging path used by the breakout PCB design |
| [TPS63020 buck-boost module](https://s.click.aliexpress.com/e/_c2uscIy1) | Hardware only | Stable 3.3 V rail for low-power/off-grid builds |
| [TP4056 lipo charging board](https://s.click.aliexpress.com/e/_c4beU1nL) | Hardware only | lipo charging via usb |
| [10.000 mAh Lipo Battery](https://s.click.aliexpress.com/e/_c45jfAGv) | Hardware only | Backup if no solar power is available |
| [6V 4.5W Solar panel](https://s.click.aliexpress.com/e/_c3njKuVF) | Hardware only |  |

### Optional BeeCounter

Information on the BeeCounter can be found here:

 [BeeCounter 2026 GitHub Repo](https://github.com/MacNite/2026-easy-bee-counter)

### Firmware pin mapping

The current firmware pin mapping is defined in `firmware/include/config.h`. The firmware source itself is split into focused units under `firmware/src/` (`main.cpp`, `network.cpp`, `portal.cpp`, `sensors.cpp`, `mics.cpp`, `accel.cpp`, `storage_power.cpp`, `device_prefs.cpp`, `bee_counter_client.cpp`, `globals.cpp`).

| Signal | GPIO | Notes |
|---|---:|---|
| HX711 #1 DOUT | 16 | Scale 1 data |
| HX711 #1 SCK | 17 | Scale 1 clock; held high during deep sleep to power down HX711 |
| HX711 #2 DOUT | 32 | Scale 2 data |
| HX711 #2 SCK | 33 | Scale 2 clock; held high during deep sleep to power down HX711 |
| DS18B20 1-Wire data | 4 | Shared bus for both hive temperature probes; use 4.7 kOhm pull-up to 3.3 V |
| I2C SDA | 21 | RTC, SHT4x, BeeCounter, optional INA219, optional MAX17048 |
| I2C SCL | 22 | RTC, SHT4x, BeeCounter, optional INA219, optional MAX17048 |
| SD CS | 5 | SD card chip select |
| SD SCK | 18 | SD card SPI clock |
| SD MISO | 23 | SD card SPI MISO |
| SD MOSI | 19 | SD card SPI MOSI |
| Setup button | 27 | `INPUT_PULLUP`; short press opens provisioning AP, long press factory resets Preferences |
| INMP441 BCLK | 14 | I2S bit clock, shared by both mics (`ENABLE_INMP441_MICS`) |
| INMP441 WS | 13 | I2S word select, shared by both mics |
| INMP441 SD | 34 | I2S data in from both mics; ESP32 input-only pin |
| BeeCounter | 21 / 22 | Polled over the shared I2C bus at addresses `0x30` / `0x31` |
| LIS3DH / LIS2DH12 | 21 / 22 | Optional per-hive accelerometers on the shared I2C bus at `0x18` / `0x19` (`ENABLE_LIS3DH_ACCEL`); tie each board's CS high (I2C) and SDO low/high for the address |



> See [docs/wiring.md](docs/wiring.md) for detailed wiring and [pcb-design/README.md](pcb-design/README.md) for the KiCad breakout PCB pinout.

---

## Firmware setup

### Prerequisites

- PlatformIO, either the VS Code extension or the CLI.

### Configuration

Copy the local configuration template:

```bash
cp firmware/include/secrets.example.h firmware/include/secrets.h
```

Edit `firmware/include/secrets.h`:

```cpp
#define DEVICE_ID           "hive-001"
#define API_KEY             "your-api-key-here"
#define CLAIM_CODE          "ABCD-1234"
#define CLAIM_CODE_REVISION 1
#define API_BASE_URL        "https://your-backend-domain.com"

#define WIFI1_SSID          "your-wifi-ssid-1"
#define WIFI1_PASS          "your-wifi-password-1"
#define WIFI2_SSID          "your-wifi-ssid-2"
#define WIFI2_PASS          "your-wifi-password-2"
#define WIFI3_SSID          "your-wifi-ssid-3"
#define WIFI3_PASS          "your-wifi-password-3"
```

Values in `secrets.h` are used to seed the device's persistent `Preferences` storage on first boot. Later changes are usually made through the backend or provisioning portal. Set `FORCE_RESEED true` only when you intentionally want to overwrite stored preferences from the build file.

### Optional modules

Optional sensors are enabled per build in `secrets.h`. The INMP441 microphones
are enabled by default in the template; the power-telemetry modules are off:

```cpp
#define ENABLE_INMP441_MICS      1   // stereo I2S mics + per-band FFT
#define ENABLE_LIS3DH_ACCEL      1   // per-hive accelerometers (0x18 / 0x19) + vibration FFT
#define ENABLE_INA219_SOLAR      0   // solar/load telemetry
#define ENABLE_MAX17048_BATTERY  0   // LiPo fuel-gauge telemetry
```

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

- `bogde/HX711`
- `paulstoffregen/OneWire`
- `milesburton/DallasTemperature`
- `adafruit/Adafruit SHT4x Library`
- `adafruit/RTClib`
- `bblanchon/ArduinoJson`
- `kosme/arduinoFFT` — per-band FFT for the INMP441 mics and the LIS3DH/LIS2DH12 vibration bands

Optional libraries are commented out in `platformio.ini`; uncomment them when the
matching flag is set in `secrets.h`:

- `adafruit/Adafruit INA219` — `ENABLE_INA219_SOLAR`
- `sparkfun/SparkFun MAX1704x Fuel Gauge Arduino Library` — `ENABLE_MAX17048_BATTERY`

---

## Wi-Fi provisioning portal

Press the setup button on GPIO27 to manage field configuration without reflashing.

| Action | Result |
|---|---|
| Short press | Starts `HiveScale-Setup-XXXX` AP; open `http://192.168.4.1` |
| Long press, 10 seconds | Clears stored Preferences and reboots |

The portal can edit Wi-Fi networks, backend URL, device ID, claim code, and API settings. It closes automatically after 10 minutes.

---

## Server setup

### Docker Compose

```bash
cd docker
cp .env.example .env
# edit API_KEY, HIVEPAL_SERVICE_API_KEY, database password, and volume paths
docker compose up -d
```

The API listens on port `31115` by default.

| Setting | Default |
|---|---|
| API image | `ghcr.io/macnite/hivescale-api:latest` |
| API port | `31115` |
| Database image | `postgres:16-alpine` |
| Database name/user | `hivescale` |

Change `API_KEY`, `HIVEPAL_SERVICE_API_KEY`, and the PostgreSQL password before exposing the service.

### Manual / local

```bash
cd server
pip install -r requirements.txt
DATABASE_URL="postgresql://hivescale:password@localhost:5432/hivescale" \
API_KEY="your-secret-key" \
HIVEPAL_SERVICE_API_KEY="your-hivepal-service-key" \
PUBLIC_BASE_URL="https://your-domain.example.com" \
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `API_KEY` | Yes | Device API key used by ESP32 firmware in `X-API-Key` |
| `HIVEPAL_SERVICE_API_KEY` | Yes, for HivePal | Service key used by HivePal in `X-HivePal-Service-Key` |
| `PUBLIC_BASE_URL` | Recommended | Public base URL used for OTA firmware download links |
| `FIRMWARE_DIR` | Optional | Firmware binary directory, default `/app/firmware` |
| `DB_POOL_MIN_SIZE` | Optional | Minimum DB connection pool size, default `1` |
| `DB_POOL_MAX_SIZE` | Optional | Maximum DB connection pool size, default `10` |
| `TZ` | Optional | Server timezone, for example `Europe/Berlin` |

The backend auto-creates tables and runs idempotent `ALTER TABLE` statements for off-grid telemetry columns on startup. The SQL migration in `server/migrations/001_offgrid_telemetry.sql` can also be used manually on older deployments.

---

## API overview

Interactive Swagger docs are available at `http://<host>:31115/docs`. See [docs/api.md](docs/api.md) for the full endpoint reference and schemas.

### Device-facing endpoints

Device endpoints require the `X-API-Key` header unless noted otherwise.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/v1/time` | UTC server time for RTC sync |
| `POST` | `/api/v1/measurements` | Submit a measurement, including optional off-grid telemetry |
| `GET` | `/api/v1/measurements/latest` | Latest measurements for dashboards |
| `GET` | `/api/v1/devices/{id}/config` | Get device configuration |
| `PATCH` | `/api/v1/devices/{id}/config` | Update device configuration |
| `GET` | `/api/v1/devices/{id}/firmware` | Check for a firmware update (`?version=` and `?target=hivescale\|beecounter`) |
| `POST` | `/api/v1/firmware/releases` | Register a firmware release (per `target`) |
| `GET` | `/firmware/{filename}` | Download a firmware binary |
| `POST` | `/api/v1/devices/{id}/commands` | Queue a remote command |
| `POST` | `/api/v1/devices/{id}/commands/update-beecounter` | Queue a BeeCounter OTA relay (`?slot=1\|2`) |
| `GET` | `/api/v1/devices/{id}/commands/next` | Claim next pending command |
| `POST` | `/api/v1/devices/{id}/commands/{cmd_id}/result` | Report command result |

### App endpoints for HivePal

App endpoints require both `X-HivePal-Service-Key` and `X-User-Id`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/app/devices/claim` | Claim a device by claim code |
| `GET` | `/api/v1/app/devices` | List devices for the current user |
| `DELETE` | `/api/v1/app/devices/{id}` | Remove the current user's membership |
| `GET` | `/api/v1/app/devices/{id}/config` | Get device config |
| `PATCH` | `/api/v1/app/devices/{id}/config` | Update device config |
| `GET` | `/api/v1/app/devices/{id}/channels` | List channel names |
| `PATCH` | `/api/v1/app/devices/{id}/channels` | Update scale display names |
| `GET` | `/api/v1/app/devices/{id}/measurements` | Measurements with date range filter |
| `GET` | `/api/v1/app/devices/{id}/measurements/latest` | Latest measurements |
| `GET` | `/api/v1/app/devices/{id}/members` | List device members |
| `POST` | `/api/v1/app/devices/{id}/members` | Share a device with another user |
| `DELETE` | `/api/v1/app/devices/{id}/members/{user_id}` | Revoke a member's access |
| `POST` | `/api/v1/app/devices/{id}/calibration/start` | Start calibration mode |
| `POST` | `/api/v1/app/devices/{id}/calibration/stop` | Stop calibration mode |
| `POST` | `/api/v1/app/devices/{id}/firmware` | Upload a firmware binary (multipart) and register it |
| `GET` | `/api/v1/app/devices/{id}/insights` | Rule-based colony insights/alerts |
| `GET` | `/api/v1/app/devices/{id}/insights/summary` | Highest-severity insight summary |

---

## Measurement payload highlights

Core payload fields include weights, hive temperatures, ambient readings, raw HX711 values, firmware version, config version, sensor status, boot count, and time source.

Builds with optional hardware can also send:

- **Acoustic (INMP441):** `mic_ok`, `mic_sample_rate_hz`, `mic_sample_frames`, per-channel `mic_left_*` / `mic_right_*` RMS/peak/normalized levels, and per-band FFT energy (`*_band_sub_bass_dbfs`, `*_band_hum_dbfs`, `*_band_piping_dbfs`, `*_band_stress_dbfs`, `*_band_high_dbfs`).
- **Entrance traffic (BeeCounter):** `bee_counter_1_*` / `bee_counter_2_*` totals, interval in/out counts, gate health, and protocol/status fields (per-gate arrays are kept in `raw_json` only).
- **Vibration (LIS3DH / LIS2DH12):** `accel_1_*` / `accel_2_*` — `accel_N_ok`, `accel_N_sample_rate_hz`, `accel_N_sample_count`, `accel_N_range_g`, broadband `accel_N_rms_mg` / `accel_N_peak_mg`, and per-band energy `accel_N_band_swarm_mg` (8–30 Hz), `accel_N_band_fanning_mg` (30–100 Hz), `accel_N_band_activity_mg` (100–200 Hz).
- **Power telemetry:** `solar_monitor_ok`, `solar_bus_voltage_v`, `solar_shunt_voltage_mv`, `solar_load_voltage_v`, `solar_current_ma`, `solar_power_mw`, `battery_monitor_ok`, `battery_voltage_v`, `battery_soc_percent`, `battery_alert`.
- **Status:** `network_transport`, `calibration_mode`, `boot_count`, `time_source`.

The backend also accepts `cellular_ok` / `cellular_csq` for the future Power Module; the on-device firmware reports `network_transport: "wifi"`.

These fields are stored in dedicated PostgreSQL columns and returned through the latest-measurements and HivePal app APIs.

---

## Claim-code pairing

1. Set `CLAIM_CODE` in `secrets.h` before flashing, for example `ABCD-1234`.
2. The firmware includes the claim code in every measurement until the device is claimed.
3. The backend stores a hash of the claim code and creates an unclaimed device record on the first measurement.
4. HivePal, or another app client, calls `POST /api/v1/app/devices/claim` with the code.
5. The matched device is assigned to the user as `owner`.

To push a new claim code through OTA, change `CLAIM_CODE`, increment `CLAIM_CODE_REVISION`, and publish a new firmware build.

---

## Remote commands

Commands are queued by the server and picked up by the device on its next cycle.

| Command type | Payload | Description |
|---|---|---|
| `calibrate_scale_1` | `{"known_weight_kg": 10.0}` | Calibrate scale 1 with a known weight |
| `calibrate_scale_2` | `{"known_weight_kg": 10.0}` | Calibrate scale 2 with a known weight |
| `start_calibration_mode` | `{"interval_seconds": 5, "timeout_seconds": 600}` | Temporarily use fast measurement cycles for calibration |
| `stop_calibration_mode` | `{}` | Return to normal interval and deep sleep behavior |
| `reboot` | `{}` | Restart the ESP32 |
| `check_ota` / `ota_update` | `{}` | Trigger an immediate OTA check |
| `update_beecounter` | `{"slot": 1, "url": "...", "version": "...", "crc32": 123}` | Relay a firmware image to a BeeCounter over I2C (usually queued via the `update-beecounter` helper) |
| `start_provisioning` | `{}` | Start the Wi-Fi provisioning AP |
| `reset_wifi` | `{}` | Clear saved Wi-Fi credentials and reboot |
| `reset_preferences` / `factory_reset` | `{}` | Clear all Preferences and reboot |

---

## PCB design

The `pcb-design/` directory contains the KiCad design for the HiveScale hardware, split into two boards:

- **Scale Module (V0.2)** — the central board. It accepts off-the-shelf modules on pin headers (no SMD soldering): ESP32, both HX711 amplifiers, load-cell terminals, I2C sensors (RTC, SHT40), SD module, two INMP441 microphones, and the BeeCounter.
- **Power Module** — handles power and connectivity (solar, battery, LTE) and connects to the Scale Module over I2C/ESP-NOW.

Start with [pcb-design/README.md](pcb-design/README.md) for the connector pinout, design intent, fabrication files, and assembly notes.

The current PCB is an early revision and should be prototyped before field deployment.

---

## Useful docs

- [docs/wiring.md](docs/wiring.md) — full wiring reference.
- [docs/offgrid-firmware-notes.md](docs/offgrid-firmware-notes.md) — solar (INA219) and LiPo (MAX17048) power-telemetry firmware behavior.
- [docs/calibration-mode.md](docs/calibration-mode.md) — calibration-mode firmware and backend behavior.
- [docs/ap-mode-sd-download.md](docs/ap-mode-sd-download.md) — AP/setup mode, button handling, and SD-card download.
- [docs/insights.md](docs/insights.md) — rule-based colony insights and detector catalogue.
- [docs/accelerometer.md](docs/accelerometer.md) — per-hive LIS3DH / LIS2DH12 vibration monitoring and the ~20 Hz swarm signal.
- [docs/api.md](docs/api.md) — complete API reference.
- [docs/test-commands.md](docs/test-commands.md) — curl commands for testing the backend.
- [docs/docker-install.md](docs/docker-install.md) — generic Docker setup.
- [docs/truenas-install.md](docs/truenas-install.md) — TrueNAS Scale setup.

---

## License

MIT © 2026 Maximilian Nitschke
