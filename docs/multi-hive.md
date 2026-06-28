# Multi-hive support (up to 18 hives per ESP32)

Firmware **v0.20.0** generalises HiveScale from a fixed two-hive device into a
dynamic registry of **up to 18 hives**, each with one scale source and at most
one in-hive sensor. This page covers the new hardware paths, the hive-centric provisioning
portal, the BLE budget, and the data model.

---

## Capacities at a glance

| Sensor | Max | How |
| --- | --- | --- |
| Scales | **18** | One scale source per hive: HX711 (1), HX711 (2), NAU7802 channels (main bus **or** mux — not both, see topology note), or a paired beehivemonitoring.com BLE HiveScale. Wired NAU7802-only tops out at **16** (mux); 18 wired channels needs the 2 HX711 channels too |
| Wired temperature | **18** | One DS18B20 probe per hive on the single 1-Wire bus, mapped by ROM address |
| In-hive BLE/GATT | **18** | One non-scale BLE/GATT sensor per hive. Passive beacons share one scan window; connection-based GATT reads remain cycle-capped |

`MAX_HIVES`, `MAX_SCALES`, `ENABLE_NAU7802`, `ENABLE_I2C_MUX` and
`MAX_GATT_READS_PER_CYCLE` are defined in `firmware/include/config.h` and may be
overridden in `secrets.h`.

---

## Scales: NAU7802 + TCA9548A

The **NAU7802** is a 24-bit I2C load-cell ADC at the fixed address `0x2A`. It has
**two differential inputs** (CH1/CH2), so one chip reads **two load cells**. It is
an alternative/complement to the HX711:

- **No mux:** one NAU7802 on the main bus = **2 scales**.
- **With a TCA9548A** 1-to-8 mux (`0x70`): up to **8 NAU7802 behind it = 16
  scales** — the maximum for an all-NAU7802 setup. Writing `1<<channel` to the
  mux connects exactly one downstream chip at a time; the driver disables all
  channels between hives.

> **Topology note.** The NAU7802 has **no address-select pin** — it is hardwired
> to `0x2A`, so two of them can never share one bus segment. A chip on the *main*
> bus and a chip *behind a mux channel* both answer at `0x2A` whenever that
> channel is enabled, which collides. So use **either** one main-bus chip (2
> scales, no mux) **or** put all NAU7802s behind the mux (up to **16**) — never
> both. (When a main-bus chip *is* present, the driver reads it only with the mux
> fully disabled, and the provisioning portal hides the mux channels to avoid
> phantom detections.)
>
> **Reaching the full 18 wired channels** therefore means combining the two HX711
> pin channels (which use dedicated GPIOs, not I2C, so they never collide with
> `0x2A`) with 16 muxed NAU7802 channels — i.e. **2 HX711 + 16 NAU7802 = 18** on
> the classic ESP32 board. An all-NAU7802 path to 18 would require a **second
> TCA9548A** strapped to a different address; the firmware models a single mux
> address today, so that is not yet supported.

The firmware reads NAU7802s **raw** and applies the same `offset`/`factor`
calibration as the HX711 path (`weightFromRaw`), so calibration is per scale
channel and stored in the hive registry.

### NAU7802 sleep before deep sleep

Before the ESP32 enters deep sleep, `powerDownScalesForSleep()`
(`firmware/src/storage_power.cpp`) calls `scalebus::powerDownAllForSleep()`, which
walks the registry and issues `powerDown()` to every NAU7802 (main bus + each mux
channel). Without this the ADC keeps converting and draws milliamps for the whole
sleep window.

## Wired temperature: up to 18 DS18B20

All DS18B20 probes share the single `ONE_WIRE_PIN` bus. Instead of reading by
index (old behaviour), each hive maps a probe by its **ROM address**, so a
specific probe always belongs to a specific hive regardless of bus enumeration
order. Map probes in the portal after an I2C/1-Wire scan. (Not available on the
XIAO ESP32-C6 — it has no spare pins; use BLE in-hive sensors there.)

---

## Hive-centric provisioning portal

The setup portal (AP mode → `http://192.168.4.1/`) is now organised **by hive**:

1. An **I2C scan** runs when the page loads, enumerating NAU7802 channels (main
   bus + each mux channel) and DS18B20 ROMs. Use **Scan BLE** for wireless
   sensors and **I2C scan details** to verify wiring.
2. **➕ Add hive** creates a hive card.
3. Inside each hive: choose exactly one **Scale** source from the dropdown. The
   portal offers HX711 (1), HX711 (2), detected NAU7802 channels, and **BLE
   HiveScale from Beehivemonitoring**; when the BLE HiveScale option is selected,
   a MAC-address field appears for pairing. The **In-hive sensor** section allows
   exactly one non-scale sensor per hive: **➕ Add BLE sensor** or **➕ Add
   DS18B20**.
4. **Save and reboot** writes one compact JSON blob per hive to NVS
   (`h0_cfg`..`h17_cfg` + `hive_count`).

On the first boot after upgrading from a pre-0.20 build, the old two-slot config
(scale offsets, paired BLE/GATT MACs) is **migrated automatically** into a
two-hive registry, so an existing device keeps working until you remap it.

---

## BLE budget

A passive BLE **scan** hears every nearby **beacon** in one window. The portal
now limits each hive to one non-scale in-hive BLE/GATT sensor, matching the
backend's single nested `ble` object per `hive_readings` row; up to 18 hives can
still each have one beacon without extra scan windows.

**GATT** sensors (GATT-mode HiveInside, HiveHeart, wireless HiveScale, HiveTraffic)
each need a **serial connect → read → disconnect** of seconds, so reading many of
them would keep the radio awake for minutes and defeat deep sleep. The firmware
therefore **caps GATT reads at `MAX_GATT_READS_PER_CYCLE` (default 4) per wake**;
any remaining paired GATT sensors are skipped that cycle and retried next wake.

**Recommendation:** prefer **beacon** in-hive sensors for multi-hive setups. Use
GATT sparingly, raise the send interval, or raise the cap only if you accept the
extra awake time. A BLE HiveScale selected as the **Scale** source is separate
from the one non-scale in-hive sensor, but both are persisted as BLE pairings.

---

## Data model

### Upload payload (firmware → server)

New firmware sends a `hives` array instead of the fixed `scale_1/2_*` /
`hive_1/2_*` fields:

```json
{
  "device_id": "hive_scale_18_01",
  "ambient_temp_c": 21.4,
  "hive_count": 3,
  "hives": [
    { "index": 1, "weight_kg": 41.2, "raw_weight": 8345012, "scale_source": "nau7802",
      "temp_c": 34.1, "temp_source": "ds18b20", "humidity_percent": 55.0,
      "accel": { "ok": true, "rms_mg": 12.5, "band_swarm_mg": 3.1 },
      "ble":   { "present": true, "sensor_type": "HolyIot 25015", "pressure_hpa": 1011.2, "battery_percent": 88 } },
    { "index": 2, "weight_kg": 38.0, "scale_source": "nau7802", "temp_c": 33.0,
      "bee_counter": { "ok": true, "total_in": 1200, "total_out": 1190, "interval_in": 5, "interval_out": 4 } }
  ]
}
```

Old firmware keeps sending the flat fields; the server accepts both.

### Storage (server)

The backend stores per-hive data in a normalized **`hive_readings`** table (one
row per hive per cycle; see `server/migrations/012_multi_hive.sql`). For
backward-compatibility, hives **1–2** are also mirrored onto the legacy
`measurements.scale_1/2_*` / `hive_1/2_*` columns, so the existing column-based
read, insights and temperature-compensation paths keep working unchanged.

### Read API

`GET /api/v1/measurements*` returns each measurement with:

- a **`hives`** array (all hives, from `hive_readings`, or synthesized from the
  legacy columns for historical/old-firmware rows), **and**
- synthesized flat **`scale_N_*` / `hive_N_*`** keys for every hive, so existing
  consumers (HivePal, MQTT, the insight detectors) reach all 18 hives without
  changes. Insights run for every hive index reported.

---

## Upgrade order

1. **Deploy the updated server first** (run migration `012_multi_hive.sql`, or just
   restart — `init_db()` creates the table idempotently). An old server would drop
   the `hives` array (`extra="ignore"`) and store nothing per-hive.
2. **Then flash firmware v0.20.0** and remap hives in the portal.

## Known limitations

- Connection-based **HiveHeart / wireless HiveScale / HiveTraffic** GATT sensors
  are read for **hives 1–2** in this release (beacons cover all hives). Their
  pairings for higher hives are stored but not yet polled.
- Server-side **temperature compensation** is applied to hives 1–2; hives 3–18
  use raw weight for insights. The firmware already accepts a per-hive
  `hive_scales` calibration array over remote config for all hives.
