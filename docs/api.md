# HiveHub API reference

The HiveHub backend exposes a FastAPI REST API and Swagger UI at `http://<host>:31115/docs`.

---

## Base URL

```text
http://<host>:31115
```

Replace `<host>` with the server IP address or domain name.

> **TLS:** the ESP32 firmware now verifies the backend's TLS certificate (it
> ships the ISRG Root X1 / Let's Encrypt root CA in `firmware/include/ca_cert.h`
> and syncs time over NTP for validity checks). Production devices must reach
> the API over **HTTPS with a valid certificate** — put the API behind a reverse
> proxy that terminates TLS. Plain-HTTP examples below are for host-side `curl`
> testing only.

---

## Authentication

HiveHub uses separate credentials for device traffic and HivePal app traffic.

### Device key

ESP32 firmware and administrative device tooling use `X-API-Key`.

```text
X-API-Key: your-api-key
```

There are two kinds of device key:

- **Per-device key** — each device has its own unique key (set as `API_KEY` in
  that device's `secrets.h`). On the first request from a new `device_id`, the
  backend stores a hash of the presented key and binds it to that device.
  Every later request for that device must present the same key, so a leaked
  key only affects the one device it belongs to. Used by the device-initiated
  endpoints: `POST /api/v1/measurements`, `GET/PATCH …/config`,
  `GET …/firmware`, `GET …/commands/next`, and `POST …/commands/{id}/result`.
- **Master/admin key** — the value of the server `API_KEY` environment
  variable. Used for server-to-server / tooling endpoints that no device
  calls: `GET /api/v1/measurements/latest`, `POST /api/v1/firmware/releases`,
  `POST …/commands` (queueing), `…/commands/update-hiveinside`, and
  `GET /api/v1/time`.

> A device's per-device key no longer has to match the server `API_KEY`. To
> rotate or re-register a device key, clear its stored hash with
> `UPDATE devices SET api_key_hash = NULL WHERE device_id = '…';` and the next
> request re-registers whatever key the device presents.

### HivePal service key + user token

HivePal uses `X-HivePal-Service-Key` plus a per-user JWT on all app endpoints.

```text
X-HivePal-Service-Key: your-hivepal-service-key
Authorization: Bearer <hivepal-jwt>
```

The service key must match HiveHub's `HIVEPAL_SERVICE_API_KEY`. The JWT is the
access token HivePal issues to its own users; HiveHub verifies its signature
with the shared `HIVEPAL_JWT_SECRET` (HS256) and reads the user ID from the
token's `sub` claim to enforce per-device ownership and roles. The legacy
plaintext `X-User-Id` header is no longer accepted.

---

## General endpoints

### `GET /health`

Health check. No authentication required. `version` is the backend server
version (`SERVER_VERSION` in `server/config.py`).

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /api/v1/time`

Returns current UTC server time for RTC sync.

**Auth:** `X-API-Key` (master/admin key)

```json
{
  "timestamp": "2026-05-01T12:00:00+00:00",
  "unix": 1777636800,
  "timezone": "UTC"
}
```

---

## Measurements

### `POST /api/v1/measurements`

Submit a measurement from a device. On the first measurement from a new `device_id`, the backend creates a device record and a default config row, and registers the presented `X-API-Key` as that device's per-device key. If a `claim_code` is included, it is hashed and stored so a HivePal user can claim the device.

**Auth:** `X-API-Key` (per-device key — registered on first contact, enforced thereafter)

> **Multi-hive payload (firmware v0.20.0+).** Devices now report up to 16 hives in
> a `hives` array (see below); the flat `scale_1/2_*` / `hive_1/2_*` fields are the
> legacy two-hive form and remain accepted. The server stores per-hive data in the
> `hive_readings` table, mirrors hives 1–2 onto the legacy columns, and on read
> returns **both** a `hives` array and synthesized `scale_N_*`/`hive_N_*` flat keys
> for every hive. See [multi-hive.md](multi-hive.md).
>
> ```json
> {
>   "device_id": "hive_scale_18_01",
>   "hive_count": 2,
>   "hives": [
>     { "index": 1, "weight_kg": 41.2, "raw_weight": 8345012, "scale_source": "nau7802",
>       "temp_c": 34.1, "temp_source": "ds18b20", "humidity_percent": 55.0,
>       "accel": { "ok": true, "rms_mg": 12.5 },
>       "ble": { "present": true, "sensor_type": "HolyIot 25015", "pressure_hpa": 1011.2 } },
>     { "index": 2, "weight_kg": 38.0, "scale_source": "nau7802", "temp_c": 33.0,
>       "bee_counter": { "ok": true, "total_in": 1200, "total_out": 1190 } }
>   ]
> }
> ```
>
> Per-hive object fields: `index` (1–18, required), `name`, `weight_kg`,
> `raw_weight`, `scale_source`, `temp_c`, `temp_source`, `humidity_percent`, and
> nested `accel{}`, `ble{}`, `mic{}`, `bee_counter{}` objects.

#### Request fields

| Field | Type | Required | Description |
|---|---|---:|---|
| `device_id` | string | Yes | Unique device identifier |
| `hives` | array | No | Per-hive readings (up to 16); see the multi-hive note above |
| `hive_count` | int | No | Number of hives the device has configured |
| `claim_code` | string | No | Pairing code; the firmware sends it only until its first successful upload, after which it is omitted to limit exposure |
| `timestamp` | ISO datetime | No | Measurement time; server receive time is used if omitted |
| `scale_1_weight_kg` | number | No | Scale 1 weight in kilograms |
| `scale_2_weight_kg` | number | No | Scale 2 weight in kilograms |
| `hive_1_temp_c` | number | No | Hive 1 internal temperature |
| `hive_2_temp_c` | number | No | Hive 2 internal temperature |
| `hive_1_humidity_percent` | number | No | Hive 1 internal relative humidity (from a paired in-hive BLE sensor) |
| `hive_2_humidity_percent` | number | No | Hive 2 internal relative humidity (from a paired in-hive BLE sensor) |
| `ambient_temp_c` | number | No | Ambient temperature |
| `ambient_humidity_percent` | number | No | Ambient relative humidity |
| `battery_voltage` | number | No | Legacy battery/supply voltage field |
| `battery_voltage_v` | number | No | Battery voltage from MAX17048; preferred off-grid field |
| `battery_soc_percent` | number | No | LiPo state-of-charge percentage |
| `battery_alert` | boolean | No | MAX17048 alert state |
| `battery_monitor_ok` | boolean | No | Whether MAX17048 was detected/read successfully |
| `solar_monitor_ok` | boolean | No | Legacy INA219 solar monitor detected/read successfully (no longer part of the recommended build) |
| `solar_bus_voltage_v` | number | No | Legacy INA219 bus voltage |
| `solar_shunt_voltage_mv` | number | No | Legacy INA219 shunt voltage |
| `solar_load_voltage_v` | number | No | Calculated load voltage |
| `solar_current_ma` | number | No | Solar/load current |
| `solar_power_mw` | number | No | Solar/load power |
| `network_transport` | string | No | `wifi` (current firmware), `sim7080g`, or another future transport label |
| `cellular_ok` | boolean | No | Cellular data connection status (Power Module) |
| `cellular_csq` | integer | No | Cellular signal quality (CSQ) value (Power Module) |
| `calibration_mode` | boolean | No | Whether firmware was in calibration mode for this reading |
| `boot_count` | integer | No | ESP32 RTC boot counter |
| `time_source` | string | No | Time source such as `rtc`, `server`, `cellular`, or `compile` |
| `rssi_dbm` | integer | No | Wi-Fi RSSI or CSQ-derived approximate RSSI |
| `firmware_version` | string | No | Running firmware version |
| `config_version` | integer | No | Config version currently applied by the device |
| `sd_ok` | boolean | No | SD card status |
| `rtc_ok` | boolean | No | RTC status |
| `sht_ok` | boolean | No | SHT4x status |
| `scale_1_raw` | integer | No | Raw HX711 reading for scale 1 |
| `scale_2_raw` | integer | No | Raw HX711 reading for scale 2 |

#### Acoustic fields (INMP441 stereo mics)

| Field | Type | Required | Description |
|---|---|---:|---|
| `mic_ok` | boolean | No | At least one microphone was read successfully |
| `mic_sample_rate_hz` | integer | No | I2S sample rate used for the capture |
| `mic_sample_frames` | integer | No | Number of stereo frames captured |
| `mic_left_ok` / `mic_right_ok` | boolean | No | Per-channel read status |
| `mic_left_rms_dbfs` / `mic_right_rms_dbfs` | number | No | Broadband RMS level in dBFS |
| `mic_left_peak_dbfs` / `mic_right_peak_dbfs` | number | No | Peak level in dBFS |
| `mic_left_rms_normalized` / `mic_right_rms_normalized` | number | No | Linear RMS as a fraction of full scale (0–1) |
| `mic_{left,right}_band_sub_bass_dbfs` | number | No | 50–150 Hz band energy (dBFS) |
| `mic_{left,right}_band_hum_dbfs` | number | No | 150–300 Hz colony-hum band energy |
| `mic_{left,right}_band_piping_dbfs` | number | No | 300–550 Hz piping/tooting band energy |
| `mic_{left,right}_band_stress_dbfs` | number | No | 550–1500 Hz agitation band energy |
| `mic_{left,right}_band_high_dbfs` | number | No | 1500–3000 Hz band energy |

#### Entrance-counter fields (HiveTraffic BeeCounter — BLE/GATT only)

One HiveTraffic counter may be paired per hive; it is read over **BLE/GATT**
(the only supported bee-counter transport — the wired I2C path was removed).
Each block is independent; a paired-but-unreachable unit reports
`bee_counter_N_ok=false` and the rest of its fields null; an unpaired hive
sends no bee-counter fields at all. Current firmware reports **lifetime totals
only** — the interval columns are backfilled server-side by differencing
consecutive totals. For `N` in `1`, `2`:

| Field | Type | Description |
|---|---|---|
| `bee_counter_N_ok` | boolean | Counter reached and parsed this cycle |
| `bee_counter_N_protocol_version` | integer | Counter firmware revision (BLE `fw` field) |
| `bee_counter_N_status_flags` | integer | Status bitfield |
| `bee_counter_N_uptime_s` | integer | Counter uptime in seconds |
| `bee_counter_N_num_gates` / `_gates_healthy` | integer | Gate count and healthy-gate count |
| `bee_counter_N_total_in` / `_total_out` | integer | Cumulative (lifetime) in/out counts |
| `bee_counter_N_glitch_count` | integer | Diagnostics |

Legacy inputs still accepted for historical rows / not-yet-reflashed devices on
the removed wired firmware: `bee_counter_N_interval_in`/`_interval_out`
(device-reported intervals), `_busy_retries`, `_read_attempts`,
`_latch_succeeded`, and the per-gate 24-byte arrays kept only in `raw_json`
(`bee_counter_N_per_gate_in` / `bee_counter_N_per_gate_out`). New firmware
never sends them.

#### Vibration fields (in-hive accelerometer)

Per-hive vibration, populated from a paired in-hive **BLE sensor** (a HiveInside
ESP32-C6 supplies the full FFT bands; a HolyIot 25015 / RuuviTag beacon supplies
only broadband `accel_N_rms_mg` / `accel_N_peak_mg`) — see
[accelerometer.md](accelerometer.md). (The old wired LIS3DH/LIS2DH12 driver has
been removed; vibration is BLE-only.) Each block is independent; a missing
source reports `accel_N_ok=false` and the rest of its fields are null. All
band/RMS values are AC (gravity removed), in milli-g (mg). For `N` in `1`, `2`:

| Field | Type | Description |
|---|---|---|
| `accel_N_ok` | boolean | Vibration source present and read this cycle |
| `accel_N_sample_rate_hz` | integer | Output data rate used for the capture |
| `accel_N_sample_count` | integer | Samples fed into the FFT |
| `accel_N_range_g` | integer | Full-scale range (±2/4/8/16 g) |
| `accel_N_rms_mg` | number | Broadband AC RMS of the vector magnitude |
| `accel_N_peak_mg` | number | Peak deviation from the mean |
| `accel_N_band_swarm_mg` | number | 8–30 Hz energy — ~20 Hz pre-swarm signal (consumed by Insights) |
| `accel_N_band_fanning_mg` | number | 30–100 Hz fanning/ventilation energy |
| `accel_N_band_activity_mg` | number | 100–200 Hz general activity energy |

See [accelerometer.md](accelerometer.md) for the rationale and wiring.

The full payload is also stored as JSONB in `raw_json`. The request model uses
`extra="ignore"`, so **only fields declared on `MeasurementIn` are stored** —
any field the model does not know about is silently dropped, not preserved in
`raw_json`. When adding a new telemetry field (e.g. a new GATT field), declare
it on `MeasurementIn` or it will be discarded on ingest.

> `network_transport`, `cellular_ok`, and `cellular_csq` are accepted and stored
> for the future Power Module. The current ESP32 firmware is Wi-Fi only and
> reports `network_transport: "wifi"`.

#### Example Wi-Fi payload

```json
{
  "device_id": "hive_scale_dual_01",
  "claim_code": "ABCD-1234",
  "timestamp": "2026-05-01T12:00:00Z",
  "scale_1_weight_kg": 42.5,
  "scale_2_weight_kg": 38.2,
  "hive_1_temp_c": 34.1,
  "hive_2_temp_c": 33.7,
  "ambient_temp_c": 18.4,
  "ambient_humidity_percent": 61.2,
  "network_transport": "wifi",
  "rssi_dbm": -65,
  "firmware_version": "0.9.2",
  "config_version": 3,
  "sd_ok": true,
  "rtc_ok": true,
  "sht_ok": true,
  "scale_1_raw": -298450,
  "scale_2_raw": -271900
}
```

#### Example off-grid payload

```json
{
  "device_id": "hive_scale_offgrid_01",
  "claim_code": "ABCD-1234",
  "timestamp": "2026-05-01T12:00:00Z",
  "scale_1_weight_kg": 42.5,
  "scale_2_weight_kg": 38.2,
  "network_transport": "sim7080g",
  "cellular_ok": true,
  "cellular_csq": 18,
  "rssi_dbm": -77,
  "battery_voltage_v": 3.94,
  "battery_soc_percent": 73.2,
  "battery_alert": false,
  "battery_monitor_ok": true,
  "solar_monitor_ok": true,
  "solar_bus_voltage_v": 5.22,
  "solar_shunt_voltage_mv": 12.4,
  "solar_load_voltage_v": 5.232,
  "solar_current_ma": 184.0,
  "solar_power_mw": 960.0,
  "calibration_mode": false,
  "boot_count": 128,
  "time_source": "cellular"
}
```

#### Response

```json
{
  "status": "ok",
  "id": 1042,
  "measured_at": "2026-05-01T12:00:00+00:00"
}
```

### `GET /api/v1/measurements/latest`

Returns recent measurements across all devices, newest-first.

**Auth:** `X-API-Key` (master/admin key)

| Query parameter | Default | Max | Description |
|---|---:|---:|---|
| `limit` | 50 | 500 | Number of rows to return |

The response includes the core fields and the optional off-grid fields listed above.

---

## Device configuration

### `GET /api/v1/devices/{device_id}/config`

Returns the current config for a device. A default config is created if none exists.

**Auth:** `X-API-Key` (per-device key)

```json
{
  "device_id": "hive_scale_dual_01",
  "send_interval_seconds": 600,
  "scale1_offset": 0,
  "scale1_factor": -7050.0,
  "scale2_offset": 0,
  "scale2_factor": -7050.0,
  "config_version": 3,
  "tempco_enabled": false,
  "tempco_source": "ambient",
  "tempco_ref_temp_c": 20.0,
  "scale1_tempco_kg_per_c": 0.0,
  "scale2_tempco_kg_per_c": 0.0
}
```

The `tempco_*` fields drive backend load-cell temperature compensation. They are
informational for the device (which keeps sending raw weights); the correction is
applied in the backend on read. See
[temperature-compensation.md](temperature-compensation.md).

Since firmware 0.23.5 the device applies the `scale*_offset`/`scale*_factor`
fields to its hive registry **only when `config_version` differs from the last
version it applied** (stored in NVS). A calibration done locally on the
provisioning portal's `/calibrate` page therefore survives config fetches; to
override it from the backend, change the values here (any `PATCH` bumps
`config_version`, which makes the device adopt them on its next cycle).

Since firmware 0.23.7 the device also **reports a portal calibration back to the
server**. The provisioning AP is offline, so a tare/span done there (and a
"Save and reboot" that carries one) only sets a pending flag in NVS. On the first
cycle with WiFi after the reboot, the device `PATCH`es its live calibration so the
backend no longer holds only defaults: hives 1–2 in the `scale1/2_offset` +
`factor` fields, and **hives 3+ in the `hive_scales` array** (below). That
`PATCH` bumps `config_version`; the device records the returned version as its
last-applied one, so the config fetch in the same cycle does not bridge those
values straight back.

The GET response includes a `hive_scales` array carrying the stored calibration
for hives 3+ (hives 1–2 stay in the `scale1/2_*` fields):

```json
"hive_scales": [
  { "index": 5, "scale": 0, "offset": 123, "factor": -7100.0 }
]
```

A `PATCH` may send the same array to set them; each entry updates one hive
(omitted `offset`/`factor` keep their current value). These are stored per hive
in `device_configs.scale_offsets_by_hive` and, like the legacy fields, are
bridged into the firmware's hive registry when `config_version` changes.

### `PATCH /api/v1/devices/{device_id}/config`

Updates one or more config fields and increments `config_version`.

**Auth:** `X-API-Key` (per-device key)

```json
{
  "send_interval_seconds": 300,
  "scale1_factor": -7200.0
}
```

---

## Firmware OTA

### `GET /api/v1/devices/{device_id}/firmware`

Checks whether a newer active firmware release is available for the given target.

The release is resolved **owner-first**: the backend looks up the device's owner
and serves that owner's most recent active release, falling back to a global /
"official" release (one with no owner) when the owner has none. A scale therefore
only ever sees firmware its own owner uploaded, or an official build.

For the `hivescale` self-update this endpoint is also **accept-to-apply**: it
returns `update: true` only once the owner has approved that exact version for the
device (`POST /api/v1/app/devices/{device_id}/firmware/approve`). Until then it
reports no update, so publishing firmware never auto-flashes a whole fleet. The
sub-device target (`hiveinside`) is relayed explicitly via commands and is not
gated here.

**Auth:** `X-API-Key` (per-device key)

| Query parameter | Default | Description |
|---|---|---|
| `version` | `0.0.0` | Current device firmware version |
| `target` | `hivescale` | `hivescale` (the ESP32 itself) or `hiveinside`. The `hivehub` alias is also accepted and treated as `hivescale`. |

No update:

```json
{ "update": false, "update_available": false }
```

Update available:

```json
{
  "update": true,
  "update_available": true,
  "version": "0.9.3",
  "url": "https://your-domain.example.com/firmware/hivescale-0.9.3.bin",
  "size": 1276928,
  "crc32": 2843490522
}
```

> The response carries both `update` and `update_available` with the same value:
> the ESP32 reads `update`, while older clients read `update_available`.
>
> `size` (bytes) and `crc32` (unsigned CRC-32 of the image file) let the device
> flash safely even when the download arrives without a `Content-Length` header
> — a reverse proxy/CDN (e.g. Cloudflare) may deliver the binary with
> `Transfer-Encoding: chunked` — and verify the received image before rebooting.
> `0` means unknown; firmware ≤ 0.23.5 ignores both fields.

### `POST /api/v1/firmware/releases`

Registers or updates a firmware release. The binary must already exist in `FIRMWARE_DIR`. The server computes and stores the image CRC-32.

**Auth:** `X-API-Key` (master/admin key)

```json
{
  "version": "0.9.3",
  "filename": "hivescale-0.9.3.bin",
  "active": true,
  "target": "hivescale"
}
```

`target` defaults to `hivescale` and may also be `hiveinside`. (`beecounter`
is no longer a valid target: the wired I2C update relay was removed and
BeeCounter OTA over BLE/GATT is not implemented yet.) Response:

```json
{ "status": "ok", "version": "0.9.3", "target": "hivescale", "crc32": 2882343476 }
```

### `GET /firmware/{filename}`

Downloads a firmware binary. This endpoint has no API-key requirement; the URL is normally obtained from the firmware check endpoint.

### `POST /api/v1/devices/{device_id}/commands/update-hiveinside`

Queues an `update_hiveinside` command that tells the HiveHub to relay the active
HiveInside firmware image to the HiveInside sensor paired in the given slot over
BLE GATT. The image URL, version, and CRC-32 are looked up server-side and embedded
in the command payload; the HiveHub resolves the BLE MAC locally.

**Auth:** `X-API-Key` (master/admin key)

| Query parameter | Default | Description |
|---|---|---|
| `slot` | `1` | HiveInside slot: `1` → `bleSensorMac0`, `2` → `bleSensorMac1` |

Returns `404` if there is no active `hiveinside` release. Response on success:

```json
{ "id": 72, "status": "pending" }
```

> The HiveHub reports the command result **after** the relay finishes, so the
> command transitions `pending` → `claimed` → `done`/`failed` reflecting the real
> outcome. A failed relay records a specific cause in the command result message
> (e.g. `HiveInside not found in scan (asleep or out of range?)`,
> `firmware download failed (HTTP 404)`, `HiveInside rejected image (… CRC/size
> mismatch?)`). The HiveInside must be awake/advertising during the locate scan.

---

## Remote commands

Commands are queued server-side and claimed by the device on a later cycle.

### `POST /api/v1/devices/{device_id}/commands`

**Auth:** `X-API-Key` (master/admin key)

```json
{
  "command_type": "start_calibration_mode",
  "payload": {"interval_seconds": 5, "timeout_seconds": 600}
}
```

| Command type | Payload | Description |
|---|---|---|
| `calibrate_scale_1` | `{"known_weight_kg": 10.0}` | Set scale 1 calibration factor using a known weight |
| `calibrate_scale_2` | `{"known_weight_kg": 10.0}` | Set scale 2 calibration factor using a known weight |
| `start_calibration_mode` | `{"interval_seconds": 5, "timeout_seconds": 600}` | Temporarily use fast cycles for calibration |
| `stop_calibration_mode` | `{}` | Return to the normal configured interval |
| `reboot` | `{}` | Restart ESP32 |
| `reset_preferences` | `{}` | Clear stored preferences and reboot |
| `factory_reset` | `{}` | Factory reset stored preferences and reboot |
| `reset_wifi` | `{}` | Clear saved Wi-Fi credentials and reboot |
| `check_ota` / `ota_update` | `{}` | Trigger immediate OTA check/update |
| `update_hiveinside` | `{"slot": 1, "url": "...", "version": "...", "crc32": 123}` | Relay a firmware image to a HiveInside sensor over BLE GATT (normally queued via the `update-hiveinside` helper above) |
| `start_provisioning` | `{}` | Start provisioning AP |

Response:

```json
{ "status": "queued", "id": 55 }
```

### `GET /api/v1/devices/{device_id}/commands/next`

Claims the next pending command and marks it as claimed.

**Auth:** `X-API-Key` (per-device key)

No command:

```json
{ "command": false }
```

Command returned:

```json
{
  "command": true,
  "id": 55,
  "command_type": "start_calibration_mode",
  "payload": {"interval_seconds": 5, "timeout_seconds": 600}
}
```

### `POST /api/v1/devices/{device_id}/commands/{command_id}/result`

Reports command success or failure. Calibration command results can include updated offset/factor values; the server applies them to `device_configs`.

**Auth:** `X-API-Key` (per-device key)

```json
{
  "success": true,
  "message": "Calibration applied",
  "result": {
    "scale1_factor": -7050.0
  }
}
```

---

## App endpoints for HivePal

All app endpoints require both `X-HivePal-Service-Key` and an `Authorization: Bearer <hivepal-jwt>` header. The user is identified by the verified token's `sub` claim.

| Role | Permissions |
|---|---|
| `owner` | Full access, including member management and removal |
| `admin` | Read plus config/channel writes |
| `viewer` | Read-only access |

### `POST /api/v1/app/devices/claim`

Claims an unclaimed device by claim code. The device must have sent at least one measurement containing that claim code.

```json
{
  "claim_code": "ABCD-1234",
  "display_name": "Back garden scale",
  "scale_1_display_name": "Hive A",
  "scale_2_display_name": "Hive B"
}
```

### `GET /api/v1/app/devices`

Lists all devices the current user can access. Device objects include `device_id`, `display_name`, `claimed_at`, `last_seen_at`, `last_firmware_version`, `role`, and `channels`.

### `DELETE /api/v1/app/devices/{device_id}`

Removes the current user's membership. If no members remain, the device becomes claimable again.

### `GET /api/v1/app/devices/{device_id}/channels`

Returns channel display names for scale 1 and scale 2.

### `PATCH /api/v1/app/devices/{device_id}/channels`

Updates channel display names. Requires `owner` or `admin`.

```json
{
  "scale_1_display_name": "Buckfast colony",
  "scale_2_display_name": "Carnica colony"
}
```

### `GET /api/v1/app/devices/{device_id}/config`

Returns the same device config schema as the device-facing config endpoint. Any role may read.

### `PATCH /api/v1/app/devices/{device_id}/config`

Updates config fields. Requires `owner` or `admin`. Accepts the `tempco_*`
temperature-compensation fields in addition to the calibration fields.

### `POST /api/v1/app/devices/{device_id}/temp-compensation/fit`

Fits a load-cell temperature coefficient from the device's own history by
regressing a scale's raw weight against an EMA-smoothed temperature channel (the
same smoothing applied at read time). A plain fit needs
`viewer`; persisting it (`apply: true`) needs `owner`/`admin`. See
[temperature-compensation.md](temperature-compensation.md).

```json
{
  "scale": 1,
  "lookback_days": 3,
  "temp_source": "ambient",
  "calibration_mode_only": false,
  "apply": true
}
```

Returns the fit (`coeff_kg_per_c`, `ref_temp_c`, `r_squared`, sample count and
temperature span). When `apply` is true and the fit succeeds, the coefficient,
reference temperature and source are written to the config and compensation is
enabled.

### `GET /api/v1/app/devices/{device_id}/measurements`

Returns measurements for one device.

| Query parameter | Default | Max | Description |
|---|---:|---:|---|
| `limit` | 200 | 10000 | Number of rows |
| `start_at` | - | - | ISO datetime lower bound |
| `end_at` | - | - | ISO datetime upper bound |

The response includes off-grid fields when the firmware sends them, plus
`scale_1_weight_kg_compensated` / `scale_2_weight_kg_compensated` and a
`tempco_applied` flag. The compensated values apply the per-device coefficient to
an EMA-smoothed temperature (see
[temperature-compensation.md](temperature-compensation.md)). When compensation is
disabled the compensated values equal the raw weights and `tempco_applied` is
`false`.

### `GET /api/v1/app/devices/{device_id}/measurements/latest`

Returns the newest measurements for one device.

| Query parameter | Default | Max | Description |
|---|---:|---:|---|
| `limit` | 50 | 500 | Number of rows |

### `POST /api/v1/app/devices/{device_id}/measurements/import`

Bulk-imports measurements parsed from a device's SD card backup
(`measurements.ndjson` / the AP-mode `hivescale-sd-data.tar` download). Used by
the HivePal web UI's "Import SD card data" button. Requires `owner` or `admin`
on the device — devices are never auto-created from uploaded data.

The request body is the same measurement objects as `POST /api/v1/measurements`,
wrapped in a list (max 20000 per request; `device_id` is taken from the URL and
forced onto every row):

```json
{
  "measurements": [
    { "timestamp": "2025-01-15T14:30:45Z", "scale_1_weight_kg": 42.1 },
    { "timestamp": "2025-01-15T14:40:45Z", "scale_1_weight_kg": 42.3 }
  ]
}
```

Import is idempotent: `(device_id, measured_at)` is the natural key, so rows that
already exist — and rows repeated within the file — are skipped, not duplicated.

#### Response

```json
{
  "status": "ok",
  "device_id": "hive_scale_dual_01",
  "received": 2,
  "inserted": 2,
  "duplicates": 0
}
```

### `GET /api/v1/app/devices/{device_id}/members`

Lists members and roles.

### `POST /api/v1/app/devices/{device_id}/members`

Shares a device with another HivePal user ID. Requires `owner`.

```json
{
  "user_id": "user-002",
  "role": "viewer"
}
```

### `DELETE /api/v1/app/devices/{device_id}/members/{user_id}`

Revokes another user's access. Requires `owner`.

### `POST /api/v1/app/devices/{device_id}/calibration/start`

Queues a `start_calibration_mode` command. Requires `owner` or `admin`. The body is optional.

| Field | Type | Default | Constraints |
|---|---|---|---|
| `interval_seconds` | int | 5 | 1 ≤ value ≤ 3600 |
| `timeout_seconds` | int | 600 | 1 ≤ value ≤ 86400 |

```json
{ "status": "pending", "id": 42, "command_type": "start_calibration_mode", "payload": { "interval_seconds": 5, "timeout_seconds": 600 } }
```

> The backend validates the ranges above; the firmware additionally clamps the
> interval to 2–30 s and the timeout to at most 30 minutes. See
> [calibration-mode.md](calibration-mode.md).

### `POST /api/v1/app/devices/{device_id}/calibration/stop`

Queues a `stop_calibration_mode` command. Requires `owner` or `admin`. No body.

```json
{ "status": "pending", "id": 43, "command_type": "stop_calibration_mode", "payload": {} }
```

### `POST /api/v1/app/devices/{device_id}/firmware`

Uploads a firmware binary as `multipart/form-data` and registers it as a release.
Requires `owner` or `admin`. Unlike `POST /api/v1/firmware/releases` (device key,
file must already be in `FIRMWARE_DIR`), this endpoint accepts the binary itself,
writes it into `FIRMWARE_DIR`, computes its CRC-32, and upserts the release.

The release is **scoped to the device's owner** (`owner_user_id`), so only scales
owned by the same user are offered it — the upload no longer reaches the whole
fleet. Pushing a global / official build is still done with the master-key
`POST /api/v1/firmware/releases`, which leaves the release un-owned.

| Form field | Required | Description |
|---|---:|---|
| `file` | Yes | The firmware binary |
| `version` | Yes | Release version string |
| `target` | No | `hivescale` (default) or `hiveinside`. **`hivehub` is accepted as an alias for `hivescale`** and is stored as `hivescale`, so HiveHub firmware needs no special handling. (`beecounter` is no longer accepted: the wired I2C update relay was removed and BeeCounter OTA over BLE/GATT is not implemented yet.) |
| `active` | No | Whether the release is active (default `true`) |

```json
{
  "status": "ok",
  "version": "0.9.3",
  "filename": "hivescale-0.9.3.bin",
  "target": "hivescale",
  "active": true,
  "size_bytes": 1048576,
  "crc32": 2882343476
}
```

> **Uploading only registers the release; it does not start a relay or auto-flash.**
> For a `hivescale` target the owner must still approve the update per device (see
> `firmware/status` and `firmware/approve` below) before any scale installs it. For
> a `hiveinside` target, queue the OTA with the trigger endpoint
> below after the upload. Releases are unique per `(owner, target, version)`, so the
> same version number can coexist across owners and across
> `hivescale`/`hiveinside`.

### `GET /api/v1/app/devices/{device_id}/firmware/status`

Reports the device's firmware-update status for the HivePal setup panel. Any role
may read. `current_version` is what the device last reported; `latest_version` is
the newest active `hivescale` release resolved owner-first (the owner's own build,
else a global/official one). `update_available` means `latest_version` is newer
than `current_version`; `pending_approval` means an update is available but the
owner has not approved it yet, so the device will **not** auto-flash until they do.

```json
{
  "device_id": "hive_scale_dual_01",
  "target": "hivescale",
  "current_version": "0.9.2",
  "latest_version": "0.9.3",
  "latest_is_official": false,
  "approved_version": null,
  "update_available": true,
  "pending_approval": true
}
```

### `POST /api/v1/app/devices/{device_id}/firmware/approve`

Approves the latest available `hivescale` firmware so this device may install it —
the accept-to-apply step behind the HivePal panel's "Apply update" button. Records
the approved version (so the device-facing firmware check starts returning
`update: true` for this device) and queues an `ota_update` command to nudge the
scale to update on its next check-in instead of waiting for its scheduled OTA poll.
Requires `owner` or `admin`. Returns `404` if there is no release available.

```json
{ "status": "approved", "device_id": "hive_scale_dual_01", "version": "0.9.3", "command_id": 88 }
```

### `POST /api/v1/app/devices/{device_id}/commands/update-hiveinside`

Queues an `update_hiveinside` command so the HiveHub relays the active HiveInside
release to the paired sensor over BLE. This is the app-facing counterpart of the
device-key `…/commands/update-hiveinside` helper; it authenticates with the HivePal
service key + user JWT and requires `owner` or `admin` on the device.

| Query parameter | Default | Description |
|---|---|---|
| `slot` | `1` | HiveInside slot (`1` or `2`) |

Returns `404` if there is no active `hiveinside` release. Response on success:

```json
{ "status": "pending", "id": 72, "command_type": "update_hiveinside", "payload": { "slot": 1 } }
```

### `GET /api/v1/app/devices/{device_id}/insights`

Computes rule-based colony alerts over recent measurements. Any role may read.
See [insights.md](insights.md) for the detector catalogue and literature sources.

| Query parameter | Default | Constraints | Description |
|---|---|---|---|
| `lookback_days` | 14 | 1 ≤ value ≤ 90 | Days of history evaluated |

```json
{
  "device_id": "hive_scale_dual_01",
  "computed_at": "2026-05-01T12:00:00+00:00",
  "lookback_days": 14,
  "measurement_count": 1280,
  "alerts": [ { "severity": "watch", "category": "swarm", "title": "Pre-swarm watch", "...": "..." } ]
}
```

### `GET /api/v1/app/devices/{device_id}/insights/summary`

Highest-severity summary of current alerts (fixed 14-day lookback), suitable for dashboard cards.

```json
{
  "device_id": "hive_scale_dual_01",
  "computed_at": "2026-05-01T12:00:00+00:00",
  "alert_count": 3,
  "highest_severity": "warning",
  "highest_alert": { "severity": "warning", "category": "swarm", "...": "..." },
  "categories": { "swarm": 1, "foraging": 1, "brood": 1 }
}
```

### `GET /api/v1/app/devices/{device_id}/insights/history`

Persisted **history** of insight alerts for the device. Unlike `/insights`
(which recomputes the *current* state on each call and stores nothing), this
returns the stored lifecycle of every alert the backend has observed —
including ones that have since resolved — newest first. Any role may read.

Alert lifecycle is reconciled by a background thread (see
`INSIGHTS_RECONCILE_*` in [.env.example](../server/.env.example)) and
opportunistically whenever `/insights/summary` is hit. While a detector keeps
firing the same row is updated (`last_seen_at` bumped, `peak_severity`
tracked); when it stops firing the row is resolved (`resolved_at` set). A later
recurrence of the same detector starts a new row.

| Query parameter | Default | Constraints | Description |
|---|---|---|---|
| `status` | `all` | `all` \| `active` \| `resolved` | Filter by lifecycle state |
| `category` | — | swarm, queenless, robbing, … | Filter by detector category |
| `since` | — | ISO 8601 | Only alerts last seen at/after this time |
| `limit` | 100 | 1 ≤ value ≤ 500 | Max rows returned |

```json
{
  "device_id": "hive_scale_dual_01",
  "lookback_days": 14,
  "count": 2,
  "active_count": 1,
  "alerts": [
    {
      "id": 42,
      "alert_key": "swarm-watch-ch1",
      "category": "swarm",
      "channel": 1,
      "severity": "watch",
      "peak_severity": "warning",
      "title": "Pre-swarm watch (hive 1)",
      "description": "...",
      "confidence": 0.65,
      "evidence": { "...": "..." },
      "source": "project spec Phase 1; MSPB arXiv 2311.10876",
      "window_start": "2026-04-30T00:00:00+00:00",
      "window_end": "2026-05-01T12:00:00+00:00",
      "first_seen_at": "2026-04-30T08:15:00+00:00",
      "last_seen_at": "2026-05-01T12:00:00+00:00",
      "resolved_at": null,
      "status": "active",
      "update_count": 7
    }
  ]
}
```

---

## Database schema

The backend auto-creates and updates the schema on startup.

| Table | Description |
|---|---|
| `devices` | Device identity, claim status, per-device API key hash, display name, last seen, firmware version, and the owner-approved firmware version (accept-to-apply gate) |
| `device_members` | Users with `owner`, `admin`, or `viewer` role per device |
| `device_channels` | Display names for scale channel 1 and 2 |
| `device_configs` | Send interval, offsets, calibration factors, config version |
| `measurements` | Measurement records, including power/acoustic/bee-counter columns and `raw_json` |
| `firmware_releases` | Firmware versions available for OTA, with `target`, `crc32`, and `owner_user_id` (NULL = global/official; otherwise the owner the release is private to) |
| `device_commands` | Pending, claimed, done, and failed commands |
| `insight_alerts` | Persisted lifecycle of insight alerts (first/last seen, peak severity, resolution) powering the history endpoint |

The backend creates the full schema on startup and runs idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` statements, so existing deployments upgrade automatically. Columns cover power telemetry (battery/solar), cellular status, calibration mode, boot count, time source, INMP441 acoustic levels + FFT bands, per-hive HiveTraffic bee-counter counts (BLE/GATT), load-cell temperature-compensation config, per-hive vibration bands, in-hive BLE humidity/pressure, and the beehivemonitoring.com `hiveheart_*` / `hivescale_*` fields; `firmware_releases` gains `target`, `crc32`, and `owner_user_id`. The SQL files in `server/migrations/` (`001`–`011`, from `001_offgrid_telemetry.sql` through `011_firmware_owner_scoping.sql`) can also be applied manually. All fields remain available in `raw_json` for forward compatibility.

---

## Error responses

FastAPI errors are returned as JSON:

```json
{ "detail": "No unclaimed device found with that claim code" }
```

| Status | Meaning |
|---:|---|
| 400 | Bad request or invalid command payload |
| 401 | Missing or invalid API key / service key / bearer token |
| 403 | Insufficient device role |
| 404 | Resource not found |
| 500 | Server misconfiguration or unexpected backend error |
