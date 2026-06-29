# RuuviTag in-hive BLE sensor

HiveHub can read up to **two RuuviTag sensors** — one per hive — as optional
in-hive sensors, exactly like the [HolyIot 25015](holyiot-ble-sensor.md). The
RuuviTag is a four-in-one BLE beacon (temperature, humidity, pressure and 3-axis
acceleration), so it folds into the same passive **BLE bridge**: no wiring, just
a battery beacon in/near the hive that the logger scans for once per upload
cycle.

| On-board sensor | Measures | HiveHub field |
|---|---|---|
| Temperature | temperature | `hive_{1,2}_temp_c` (shared with / replacing the wired DS18B20) |
| Humidity | relative humidity | `ble_{1,2}_humidity_percent` |
| Pressure | barometric pressure | `ble_{1,2}_pressure_hpa` |
| Accelerometer | 3-axis acceleration | `accel_{1,2}_*` (reused accelerometer fields) |

Because the RuuviTag reports the same measurement set as the HolyIot beacon, it
reuses **all the same columns** — there is no new migration and no new server
field. The acceleration is reported as a per-cycle AC magnitude
(`accel_{1,2}_rms_mg` / `accel_{1,2}_peak_mg`) and feeds the same low-rate
pre-swarm detector (`detect_lowrate_accel_swarm`) described in the HolyIot doc.

---

## Enabling and pairing

1. **Firmware flag.** The RuuviTag rides the existing `ENABLE_BLE_SCAN` bridge —
   no extra flag. It is auto-detected by Ruuvi's registered Bluetooth company id
   (`RUUVI_COMPANY_ID`, `0x0499`).
2. **Pair from the setup portal.** Open the provisioning portal (short-press the
   setup button, join the `HiveHub-Setup-XXXX` AP, browse to
   `http://192.168.4.1/`). Under **Wireless sensors**:
   - Click **scan for wireless sensors**; a RuuviTag broadcasting a parseable
     payload is flagged as `RuuviTag` in the type column.
   - Click **➕ Add wireless sensor**, pick **RuuviTag** (an in-hive type),
     choose **Hive 1** or **Hive 2** in the **Maps to** dropdown, paste the
     sensor's MAC, then **Save and reboot**. The **Maps to** choice decides the
     hive, so either hive can be wireless independently.
   The MACs persist in Preferences (`ble_mac0` / `ble_mac1`), shared with the
   HolyIot/HiveInside in-hive slots — a slot holds one in-hive sensor of any
   supported type.

Each cycle the firmware scans for `HOLYIOT_BLE_SCAN_SECONDS` (default 6 s),
matches advertisements to the paired MACs, decodes the RuuviTag payload and
folds the readings into the normal measurement upload before connecting WiFi.

### Hive temperature source

Same precedence as the HolyIot: when `ENABLE_DS18B20_HIVE_TEMP` is on **and** the
wired probe returns a valid reading, the DS18B20 wins and the RuuviTag
temperature is the fallback. With the DS18B20 disabled, `hive_{1,2}_temp_c` comes
from the paired RuuviTag.

---

## Advertisement format

The RuuviTag broadcasts everything in **one** manufacturer-specific AD under the
Ruuvi company id `0x0499`. Two on-air formats are decoded
(`firmware/include/ruuvi_decode.h`), selected by the format byte at offset 2
(first byte after the 2-byte company id):

### Data Format 5 (RAWv2) — current default, 24-byte payload, big-endian

| Field | Offset in manufacturer data | Decode |
|---|---|---|
| Temperature | d[3..4] int16 | × 0.005 → °C (`0x8000` = invalid) |
| Humidity | d[5..6] uint16 | × 0.0025 → %RH (`0xFFFF` = invalid) |
| Pressure | d[7..8] uint16 | (+ 50000) Pa → hPa (`0xFFFF` = invalid) |
| Accel X/Y/Z | d[9..10], d[11..12], d[13..14] int16 | mg (`0x8000` = invalid) |
| Power info | d[15..16] uint16 | bits 15..5 battery (mV − 1600); bits 4..0 TX power (2·n − 40 dBm) |
| Movement counter | d[17] uint8 | kept in `raw_json` |
| Sequence number | d[18..19] uint16 | kept in `raw_json` |

### Data Format 3 (RAWv1) — legacy, 14-byte payload

Humidity (uint8 ×0.5), temperature (integer byte with sign bit + fraction byte
/100), pressure (uint16 +50000), accel X/Y/Z (int16 mg) and battery (uint16 mV).

Both layouts are validated against Ruuvi's own published test vectors in
`test-data/test_ruuvi_decode.cpp`, which builds with plain `g++` (no
Arduino/NimBLE):

```sh
g++ -std=c++17 -I firmware/include test-data/test_ruuvi_decode.cpp -o /tmp/t && /tmp/t
```

### Battery

The RuuviTag reports a coin-cell **voltage** (the HolyIot reported a percent). It
is mapped to the shared `ble_{1,2}_battery_percent` field with a clamped linear
estimate over the CR2477's usable window (~3.0 V fresh → 100 %, ~2.0 V → 0 %).
This is a coarse fuel gauge, adequate for "battery getting low" alerts.

---

## Database

None required. The RuuviTag reuses the columns added for the HolyIot 25015
(`ble_{1,2}_humidity_percent`, `ble_{1,2}_pressure_hpa`,
migration `008_holyiot_ble_sensor.sql`), the accelerometer columns
(`accel_{1,2}_*`) and `hive_{1,2}_temp_c`. The `ble_{1,2}_sensor_type` value in
`raw_json` reads `RuuviTag` so the source is identifiable.
