# HiveHub off-grid power telemetry

Off-grid builds add **power telemetry** to the normal HiveHub measurement
cycle: LiPo fuel-gauge monitoring with a **MAX17048** (the module used on the
ESP32-C6 Scale Module V0.4). It is an optional I2C module and disabled by
default. Enable it per device in `firmware/include/secrets.h`.

> **Connectivity has moved.** Earlier firmware supported a SIM7080G LTE/NB-IoT
> modem for cellular transport. That has been removed — the ESP32 firmware is now
> **Wi-Fi only**. LTE/NB-IoT, solar charging, and battery management are handled
> by a separate **Power Module** that connects to the Scale Module over
> I2C/ESP-NOW. The backend still accepts `network_transport`, `cellular_ok`, and
> `cellular_csq` so that Power Module can report cellular status later; on-device
> firmware reports `network_transport: "wifi"`.

---

## Feature flags

Use numeric `0` / `1` values because the firmware uses preprocessor `#if` checks.

```cpp
#define ENABLE_MAX17048_BATTERY  1
```

| Flag | Default | Effect |
|---|---|---|
| `ENABLE_MAX17048_BATTERY` | `0` | Compiles in MAX17048 support and adds LiPo telemetry fields |

The matching library (`sparkfun/SparkFun MAX1704x Fuel Gauge Arduino Library`)
is listed in `firmware/platformio.ini`.

> **Legacy: INA219 solar telemetry.** Earlier builds could add an INA219 for
> solar/load voltage-current-power telemetry (`ENABLE_INA219_SOLAR`,
> `solar_*` measurement fields). The flag and the backend columns still exist
> for old devices, but the INA219 is no longer part of the recommended setup
> or the BOM — the MAX17048 covers battery state, which is what matters for
> an unattended deployment.

---

## LiPo telemetry with MAX17048

Enable:

```cpp
#define ENABLE_MAX17048_BATTERY 1
#define MAX17048_ALERT_PERCENT  20
```

Wiring uses the shared I2C bus plus the battery sense connection required by the breakout board.

Measurement fields:

| Field | Description |
|---|---|
| `battery_monitor_ok` | MAX17048 detected and readable |
| `battery_voltage_v` | Battery voltage in volts |
| `battery_soc_percent` | Battery state-of-charge percentage |
| `battery_alert` | Alert flag from the fuel gauge |

The backend also returns `battery_voltage` for backwards compatibility, mapped from `battery_voltage_v` when available.

---

## Backend storage and API behavior

The backend stores power telemetry in dedicated columns as well as in `raw_json`.

The startup schema and `server/migrations/001_offgrid_telemetry.sql` include columns for:

- battery state-of-charge, voltage, monitor status, and alert
- solar voltage/current/power values
- transport and cellular status (reserved for the Power Module)
- calibration mode state
- boot count
- time source

These fields are returned by `/api/v1/measurements/latest` and the HivePal app measurement endpoints.

---

## Power-saving behavior

Normal operation is one wake cycle:

1. Wake from deep sleep or reset.
2. Power up sensors and scale ADCs.
3. Measure weights, temperatures, acoustic levels, and optional power telemetry.
4. Connect over Wi-Fi.
5. Upload the measurement and retry cached rows.
6. Poll config and commands.
7. Power down the scale ADCs and SD where supported.
8. Enter deep sleep until the next send interval.

The MAX17048 sits on the shared I2C bus and is read in step 3 when its flag is enabled.

---

## Practical notes

- Add bulk capacitance close to any LiPo/solar input; load peaks can brown out weak supplies.
- Keep load-cell analog wiring away from switching regulators.
- Set the MAX17048 low-battery alert (`MAX17048_ALERT_PERCENT`) to match your battery and duty cycle.
- Use the SD card cache as protection against temporary Wi-Fi outages, not as a replacement for regular connectivity checks.
- For cellular/solar charging in the field, use the separate Power Module rather than wiring a modem directly to the Scale Module.
