# HolyIot 25015 in-hive BLE sensor

HiveScale can read up to **two HolyIot 25015 BLE sensors** — one per hive — as
optional in-hive sensors. The ESP32 acts as a passive **BLE bridge**: no wiring
into the hive, just a battery beacon sitting on/near the comb that the logger
scans for once per upload cycle.

The 25015 (nRF54L15) carries three sensors:

| On-board chip | Measures | HiveScale field |
|---|---|---|
| SHT40 | temperature | `hive_{1,2}_temp_c` (shared with / replacing the wired DS18B20) |
| SHT40 | relative humidity | `ble_{1,2}_humidity_percent` |
| LPS22HB | barometric pressure | `ble_{1,2}_pressure_hpa` |
| LIS2DH12 | 3-axis acceleration | `accel_{1,2}_*` (reused accelerometer fields) |

It replaces the previous wired LIS3DH/LIS2DH12 accelerometer
([accelerometer.md](accelerometer.md)).

---

## Enabling and pairing

1. **Firmware flag.** Set `ENABLE_BLE_SCAN 1` in `secrets.h` (see
   `secrets.example.h` for all options). The wired in-hive microphone
   (`ENABLE_INMP441_MICS`) and DS18B20 probes (`ENABLE_DS18B20_HIVE_TEMP`) are
   independent optional features — turn them off if a build relies on the BLE
   sensor instead.
2. **Pair from the setup portal.** Open the provisioning portal (short-press the
   setup button, join the `HiveScale-Setup-XXXX` AP, browse to
   `http://192.168.4.1/`). Under **In-hive BLE sensors (HolyIot 25015)**:
   - Click **scan for nearby sensors** to list advertising BLE devices; sensors
     carrying a parseable 25015 payload are flagged.
   - Paste each sensor's MAC into **Sensor 1 MAC (hive 1)** / **Sensor 2 MAC
     (hive 2)** and **Save and reboot**.
   The MACs persist in Preferences (`ble_mac0` / `ble_mac1`).

Each cycle the firmware scans for `HOLYIOT_BLE_SCAN_SECONDS` (default 6 s),
matches advertisements to the paired MACs, and folds the readings into the
normal measurement upload before connecting WiFi.

### Hive temperature source

When `ENABLE_DS18B20_HIVE_TEMP` is on **and** the wired probe returns a valid
reading, the DS18B20 wins and the BLE temperature is the fallback. With the
DS18B20 disabled, `hive_{1,2}_temp_c` comes from the paired sensor's SHT40.

---

## ⚠️ Advertisement format is a best guess

HolyIot do **not** publish the 25015 advertisement byte layout. The parser in
`firmware/src/ble_sensor.cpp` uses a **documented best-effort layout**:

```
manufacturer-specific data (AD type 0xFF):
  off 0..1  company id (LE)            == HOLYIOT_COMPANY_ID (default 0xFFFF)
  off 2     frame/type                 (ignored)
  off 3     battery percent (uint8)
  off 4..5  temperature  int16 LE      /100  -> °C
  off 6..7  humidity     uint16 LE     /100  -> %RH
  off 8..9  pressure     uint16 LE     /10   -> hPa
  off 10..11 accel X     int16 LE            -> mg
  off 12..13 accel Y     int16 LE            -> mg
  off 14..15 accel Z     int16 LE            -> mg
```

After sniffing one real packet (e.g. nRF Connect), correct the `HOLYIOT_OFF_*` /
`*_SCALE` / `HOLYIOT_COMPANY_ID` constants — nothing else needs to change. Until
the layout is confirmed, treat the decoded values as provisional.

---

## Acceleration & the low-rate pre-swarm insight

A passive beacon emits **periodic single-shot** acceleration samples, not a
high-rate stream, so the 8–30 Hz FFT swarm band the wired sensor produced cannot
be computed. Instead:

- The firmware reports a **per-cycle AC magnitude** (gravity removed) in the
  reused `accel_{1,2}_rms_mg` / `accel_{1,2}_peak_mg` fields
  (`accel_{1,2}_band_*_mg` stay null). Raw per-axis values, battery % and link
  RSSI are kept in `raw_json` (`ble_{1,2}_accel_*_mg`,
  `ble_{1,2}_battery_percent`, `ble_{1,2}_rssi_dbm`).
- The server runs `detect_lowrate_accel_swarm` (insights.py): it trends the
  **night-time mean** of `accel_{1,2}_rms_mg` and raises a lower-confidence
  `swarm` / `watch` alert (`swarm-ble-vibration-chN`) when in-hive movement
  climbs ≥ `LOWRATE_SWARM_RISE_MULT`× over its baseline. Rising comb excitation
  over several days can precede swarming (Bencsik/Ramsey). When real FFT-band
  data exists for a hive, this detector defers to the band detector to avoid a
  duplicate alert.

This is a coarser signal than research-grade comb vibrometry — the LIS2DH12's
noise floor limits it to detecting the louder, collective movement changes — but
it adds a genuine, no-wiring pre-swarm cue alongside the weight and temperature
rules.

---

## Database

Migration `008_holyiot_ble_sensor.sql` adds the four new columns
(`ble_{1,2}_humidity_percent`, `ble_{1,2}_pressure_hpa`). `init_db()` creates the
same columns on fresh deployments, and both are idempotent. Acceleration and
temperature reuse the existing `accel_{1,2}_*` and `hive_{1,2}_temp_c` columns.
