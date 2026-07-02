# HiveHub Calibration Mode — Backend & Firmware

## Overview

Calibration mode is a temporary operating state that disables deep sleep on the ESP32 and switches the device to a much shorter measurement interval (default: 5 seconds). This allows the scale's raw ADC readings to update rapidly in near-real time, making it practical to capture an empty-scale baseline and a known-weight reading without waiting minutes between cycles. Once calibration is complete, the device returns to normal battery-saving deep sleep behaviour automatically.

---

## Firmware

The firmware is split across several units: the calibration constants live in
`firmware/include/config.h`; `startCalibrationMode()` / `stopCalibrationMode()` /
`calibrationModeExpired()` are in `firmware/src/portal.cpp`; `enterDeepSleep()` is
in `firmware/src/storage_power.cpp`; the `loop()` interval/timeout handling is in
`firmware/src/main.cpp`; and command parsing (`checkCommands()`) is in
`firmware/src/hivescale_network.cpp`.

### Constants (`config.h`)

| Constant | Default | Description |
|---|---|---|
| `CALIBRATION_MODE_DEFAULT_INTERVAL_MS` | 5,000 ms | Measurement interval while calibration mode is active |
| `CALIBRATION_MODE_MIN_INTERVAL_MS` | 2,000 ms | Minimum allowed interval (clamped if the command sends a lower value) |
| `CALIBRATION_MODE_MAX_INTERVAL_MS` | 30,000 ms | Maximum allowed interval |
| `CALIBRATION_MODE_DEFAULT_TIMEOUT_MS` | 10 min | How long calibration mode stays active if not stopped explicitly |
| `CALIBRATION_MODE_MAX_TIMEOUT_MS` | 30 min | Upper bound on timeout (clamped if the command sends a higher value) |

### State Variables

```cpp
bool calibrationModeActive = false;
unsigned long calibrationModeStartedMs = 0;
unsigned long calibrationModeIntervalMs = CALIBRATION_MODE_DEFAULT_INTERVAL_MS;
unsigned long calibrationModeTimeoutMs  = CALIBRATION_MODE_DEFAULT_TIMEOUT_MS;
```

### `startCalibrationMode(intervalSeconds, timeoutSeconds)`

Activates calibration mode. Both parameters are clamped to their min/max bounds before being stored. Sets `calibrationModeActive = true` and records `calibrationModeStartedMs = millis()`. Prints the effective interval and timeout to Serial.

```cpp
void startCalibrationMode(unsigned long intervalSeconds, unsigned long timeoutSeconds);
```

### `stopCalibrationMode(reason)`

Clears the `calibrationModeActive` flag and logs the reason string to Serial. Called on an explicit `stop_calibration_mode` command or automatically when the timeout expires.

```cpp
void stopCalibrationMode(const String& reason);
```

### `calibrationModeExpired()`

Returns `true` if calibration mode is active and `millis() - calibrationModeStartedMs >= calibrationModeTimeoutMs`. Checked in `loop()` before every cycle.

### Deep-sleep interaction

Deep sleep is **blocked** while calibration mode is active. The guard in `enterDeepSleep()` returns early with a log message if `calibrationModeActive` is `true`:

```cpp
if (calibrationModeActive) {
    Serial.println("[SLEEP] Calibration mode active; staying awake");
    return;
}
```

In `loop()`, the active interval switches between the normal `sendIntervalMs` and `calibrationModeIntervalMs` depending on the flag:

```cpp
unsigned long activeIntervalMs = calibrationModeActive
    ? calibrationModeIntervalMs
    : sendIntervalMs;
```

Command polling also accelerates to match the calibration interval so new commands (e.g. `stop_calibration_mode`) are picked up quickly.

### Measurement JSON

Every measurement includes a `calibration_mode` boolean field so the backend and frontend can distinguish calibration readings from normal ones:

```json
{
  "calibration_mode": true,
  "scale_1_raw": 123456,
  "scale_2_raw": 789012,
  ...
}
```

### Command handling (`checkCommands()`)

The firmware polls `GET /api/v1/devices/{device_id}/commands/next` at the end of every cycle. Two command types are relevant to calibration:

**`start_calibration_mode`**

Payload fields:

| Field | Type | Default | Bounds |
|---|---|---|---|
| `interval_seconds` | int | 5 | 2 – 30 |
| `timeout_seconds` | int | 600 | — (0 = default; > 1800 clamped to 1800) |

```cpp
} else if (type == "start_calibration_mode") {
    unsigned long intervalSeconds = payload["interval_seconds"] | CALIBRATION_MODE_DEFAULT_INTERVAL_MS / 1000UL;
    unsigned long timeoutSeconds  = payload["timeout_seconds"]  | CALIBRATION_MODE_DEFAULT_TIMEOUT_MS  / 1000UL;
    startCalibrationMode(intervalSeconds, timeoutSeconds);
    postCommandResult(commandId, true, "Calibration mode started");
}
```

**`stop_calibration_mode`**

No payload. Calls `stopCalibrationMode("command received")` and posts a success result.

### Automatic timeout

If the stop command never arrives, `calibrationModeExpired()` returns `true` in `loop()`, which calls `stopCalibrationMode("timeout reached")` and then calls `enterDeepSleep(sendIntervalMs)` to resume normal operation.

---

## Backend API (`server/`)

### Data model

The `MeasurementIn` Pydantic model and the `measurements` PostgreSQL table both include a `calibration_mode BOOLEAN` column. This lets historical data be queried or filtered by whether a reading was taken during calibration.

```python
class MeasurementIn(BaseModel):
    ...
    calibration_mode: Optional[bool] = None
```

The column is added with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db()` so existing deployments upgrade automatically on startup.

### Calibration Mode endpoints (App API)

Both endpoints require the `X-HivePal-Service-Key` header and an `Authorization: Bearer <hivepal-jwt>` header (verified with `HIVEPAL_JWT_SECRET`). The calling user, identified by the token's `sub` claim, must have the `owner` or `admin` role on the device.

#### `POST /api/v1/app/devices/{device_id}/calibration/start`

Queues a `start_calibration_mode` command for the device. The optional request body controls interval and timeout.

Request body (`AppCalibrationModeStartIn`, optional — defaults are used if omitted):

| Field | Type | Default | Backend constraint |
|---|---|---|---|
| `interval_seconds` | int | 5 | 1 ≤ value ≤ 3600 |
| `timeout_seconds` | int | 600 | 1 ≤ value ≤ 86400 |

> The backend only enforces the wide bounds above. The **firmware** applies the
> real safety limits: it clamps the interval to **2–30 s** and the timeout to at
> most **30 minutes** (`CALIBRATION_MODE_*` constants in `config.h`).

Response:

```json
{
  "status": "pending",
  "id": 42,
  "command_type": "start_calibration_mode",
  "payload": { "interval_seconds": 5, "timeout_seconds": 600 }
}
```

#### `POST /api/v1/app/devices/{device_id}/calibration/stop`

Queues a `stop_calibration_mode` command. No request body.

Response:

```json
{
  "status": "pending",
  "id": 43,
  "command_type": "stop_calibration_mode",
  "payload": {}
}
```

### General command infrastructure

Both calibration commands use the shared `create_command()` helper, which inserts a row into `device_commands` with `status = 'pending'`. The firmware claims commands one at a time via `GET /api/v1/devices/{device_id}/commands/next` (uses `FOR UPDATE SKIP LOCKED` to be safe against concurrent callers) and posts the result back via `POST /api/v1/devices/{device_id}/commands/{command_id}/result`.

Because the device only polls for commands at the end of a normal measurement cycle, there is an inherent delay between queuing the command and the device acting on it. The delay equals at most one normal send interval (default 10 minutes). The frontend reflects this with a "Queued" badge until the device confirms calibration mode is active by sending a measurement with `calibration_mode = true`.

### Pydantic model for command dispatch

`DeviceCommandIn` includes both calibration command types in its `Literal` union to enforce valid command names at the API boundary:

```python
class DeviceCommandIn(BaseModel):
    command_type: Literal[
        ...,
        "start_calibration_mode",
        "stop_calibration_mode",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
```

---

## Full flow summary

```
HivePal app                Backend                    ESP32 firmware
───────────────────────────────────────────────────────────────────
POST /calibration/start ──► INSERT device_commands
                                                   ◄── GET /commands/next
                           UPDATE status='claimed'
                                                       startCalibrationMode()
                                                       deep sleep blocked
                                                   ──► POST /measurements  (calibration_mode=true)
                                                   ──► POST /commands/.../result
                            UPDATE status='done'
    [polling: refetch
     measurements every
     5 s while active]
                            ...fast readings arrive...

POST /calibration/stop  ──► INSERT device_commands
                                                   ◄── GET /commands/next
                           UPDATE status='claimed'
                                                       stopCalibrationMode()
                                                       resume deep sleep
                                                   ──► POST /commands/.../result
                            UPDATE status='done'
```