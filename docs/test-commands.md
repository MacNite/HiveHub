# HiveHub test commands

Use these `curl` examples to verify a HiveHub backend and to simulate firmware/HivePal traffic.

Replace placeholders before running:

| Placeholder | Replace with |
|---|---|
| `HOST` | Server host or domain, for example `192.168.1.100` or `hivescale.example.com` |
| `YOUR_API_KEY` | For device endpoints: that device's **per-device key** (registered on first contact). For the admin/tooling endpoints (`measurements/latest`, `firmware/releases`, queueing commands, `update-beecounter`, `time`): the server's master `API_KEY`. |
| `YOUR_HIVEPAL_KEY` | HiveHub `HIVEPAL_SERVICE_API_KEY`, also configured in HivePal as `HIVESCALE_SERVICE_API_KEY` |
| `DEVICE_ID` | Device ID, for example `hive_scale_dual_01` |
| `JWT_TOKEN` | A HivePal user access token (JWT). HiveHub verifies it with `HIVEPAL_JWT_SECRET` and reads the user from the `sub` claim. Get one from HivePal's login/register response. |

The examples assume HTTP on port `31115` for host-side testing. Use HTTPS and omit the port when running behind a reverse proxy. Note the ESP32 firmware itself only talks to the API over HTTPS with a verified certificate.

---

## Health and time

```bash
curl http://HOST:31115/health
```

```bash
curl http://HOST:31115/api/v1/time \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Submit measurements

### Basic Wi-Fi style payload

```bash
curl -X POST http://HOST:31115/api/v1/measurements \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "device_id": "DEVICE_ID",
    "claim_code": "ABCD-1234",
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
  }'
```

### Off-grid (cellular + solar + LiPo) payload

> `network_transport`/`cellular_*` are accepted by the backend for the future
> Power Module; the current ESP32 firmware is Wi-Fi only.

```bash
curl -X POST http://HOST:31115/api/v1/measurements \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "device_id": "DEVICE_ID",
    "claim_code": "ABCD-1234",
    "scale_1_weight_kg": 42.5,
    "scale_2_weight_kg": 38.2,
    "hive_1_temp_c": 34.1,
    "hive_2_temp_c": 33.7,
    "ambient_temp_c": 18.4,
    "ambient_humidity_percent": 61.2,
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
    "time_source": "cellular",
    "firmware_version": "0.9.2",
    "config_version": 3,
    "sd_ok": true,
    "rtc_ok": true,
    "sht_ok": true,
    "scale_1_raw": -298450,
    "scale_2_raw": -271900
  }'
```

### Latest measurements

```bash
curl "http://HOST:31115/api/v1/measurements/latest?limit=10" \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Device configuration

```bash
curl http://HOST:31115/api/v1/devices/DEVICE_ID/config \
  -H "X-API-Key: YOUR_API_KEY"
```

```bash
curl -X PATCH http://HOST:31115/api/v1/devices/DEVICE_ID/config \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "send_interval_seconds": 300,
    "scale1_offset": 0,
    "scale1_factor": -7050.0,
    "scale2_offset": 0,
    "scale2_factor": -7050.0
  }'
```

---

## Commands

### Queue calibration commands

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "calibrate_scale_2", "payload": {"known_weight_kg": 20.0}}'
```

### Start and stop calibration mode

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "start_calibration_mode", "payload": {"interval_seconds": 5, "timeout_seconds": 600}}'
```

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "stop_calibration_mode", "payload": {}}'
```

### Reset / provisioning / OTA commands

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "start_provisioning", "payload": {}}'
```

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "factory_reset", "payload": {}}'
```

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"command_type": "ota_update", "payload": {}}'
```

### Simulate device command polling

```bash
curl http://HOST:31115/api/v1/devices/DEVICE_ID/commands/next \
  -H "X-API-Key: YOUR_API_KEY"
```

### Report command result

```bash
curl -X POST http://HOST:31115/api/v1/devices/DEVICE_ID/commands/55/result \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "success": true,
    "message": "Calibration applied",
    "result": {"scale2_factor": -7050.0}
  }'
```

---

## OTA firmware

Register a release. The binary must already exist in `FIRMWARE_DIR`. `target`
defaults to `hivescale` and may also be `beecounter`.

```bash
curl -X POST http://HOST:31115/api/v1/firmware/releases \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"version": "0.9.3", "filename": "hivescale-0.9.3.bin", "active": true, "target": "hivescale"}'
```

Check for an update (per target):

```bash
curl "http://HOST:31115/api/v1/devices/DEVICE_ID/firmware?version=0.9.2&target=hivescale" \
  -H "X-API-Key: YOUR_API_KEY"
```

Queue a BeeCounter OTA relay (slot 1 → I2C `0x30`, slot 2 → `0x31`). Requires an
active `beecounter` release:

```bash
curl -X POST "http://HOST:31115/api/v1/devices/DEVICE_ID/commands/update-beecounter?slot=1" \
  -H "X-API-Key: YOUR_API_KEY"
```

Upload a firmware binary from an app client (multipart) and register it in one
call. Requires `owner`/`admin` on the device:

```bash
curl -X POST http://HOST:31115/api/v1/app/devices/DEVICE_ID/firmware \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN" \
  -F "file=@hivescale-0.9.3.bin" \
  -F "version=0.9.3" \
  -F "target=hivescale" \
  -F "active=true"
```

---

## HivePal app endpoints

### Claim a device

The device must have sent at least one measurement containing the claim code.

```bash
curl -X POST http://HOST:31115/api/v1/app/devices/claim \
  -H "Content-Type: application/json" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN" \
  -d '{
    "claim_code": "ABCD-1234",
    "display_name": "Back garden scale",
    "scale_1_display_name": "Hive A",
    "scale_2_display_name": "Hive B"
  }'
```

### List devices

```bash
curl http://HOST:31115/api/v1/app/devices \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

### Get device measurements

```bash
curl "http://HOST:31115/api/v1/app/devices/DEVICE_ID/measurements?limit=100" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

```bash
curl "http://HOST:31115/api/v1/app/devices/DEVICE_ID/measurements?start_at=2026-05-01T00:00:00Z&end_at=2026-05-07T00:00:00Z" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

### Get latest measurements

```bash
curl "http://HOST:31115/api/v1/app/devices/DEVICE_ID/measurements/latest?limit=10" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

### Update channel names

```bash
curl -X PATCH http://HOST:31115/api/v1/app/devices/DEVICE_ID/channels \
  -H "Content-Type: application/json" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN" \
  -d '{
    "scale_1_display_name": "Hive A",
    "scale_2_display_name": "Hive B"
  }'
```

### Share a device with another HivePal user ID

```bash
curl -X POST http://HOST:31115/api/v1/app/devices/DEVICE_ID/members \
  -H "Content-Type: application/json" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN" \
  -d '{"user_id": "other-user-id", "role": "viewer"}'
```

### Start and stop calibration mode (app endpoints)

```bash
curl -X POST http://HOST:31115/api/v1/app/devices/DEVICE_ID/calibration/start \
  -H "Content-Type: application/json" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN" \
  -d '{"interval_seconds": 5, "timeout_seconds": 600}'
```

```bash
curl -X POST http://HOST:31115/api/v1/app/devices/DEVICE_ID/calibration/stop \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

### Insights

```bash
curl "http://HOST:31115/api/v1/app/devices/DEVICE_ID/insights?lookback_days=14" \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```

```bash
curl http://HOST:31115/api/v1/app/devices/DEVICE_ID/insights/summary \
  -H "X-HivePal-Service-Key: YOUR_HIVEPAL_KEY" \
  -H "Authorization: Bearer JWT_TOKEN"
```
