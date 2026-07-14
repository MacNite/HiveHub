# Multi-hive support (up to 16 hives per ESP32)

Firmware **v0.20.0** generalises HiveHub from a fixed two-hive device into a
dynamic registry of **up to 16 hives**, each with one scale source and at most
one in-hive sensor. This page covers the new hardware paths, the hive-centric provisioning
portal, the BLE budget, and the data model.

---

## Capacities at a glance

| Sensor | Max | How |
| --- | --- | --- |
| Scales | **16** | One scale source per hive: NAU7802 channels (main bus **or** mux — not both, see topology note), HX711 (1) / HX711 (2) on the legacy 30-pin board, or a paired beehivemonitoring.com BLE HiveScale. All-NAU7802 wired reaches **16** (8 chips behind the TCA9548A mux) |
| Wired temperature | **16** | One DS18B20 probe per hive on the single 1-Wire bus, mapped by ROM address |
| In-hive BLE/GATT | **16** | One non-scale BLE/GATT sensor per hive. Passive beacons share one scan window; connection-based GATT reads remain cycle-capped |

> **Registry footnote:** the firmware's registry (`MAX_HIVES`) technically holds
> **18** hives — 16 muxed NAU7802 channels plus the 2 HX711 pin channels of the
> obsolete 30-pin board. Since the legacy board is no longer recommended, the
> supported and advertised maximum is **16**.

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
> On the obsolete 30-pin board the two HX711 pin channels (dedicated GPIOs, not
> I2C, so no `0x2A` collision) could technically be combined with the 16 muxed
> NAU7802 channels for 18 wired channels — but the legacy board is no longer
> recommended, so **16 is the supported maximum**.

The firmware reads NAU7802s **raw** and applies the same `offset`/`factor`
calibration as the HX711 path (`weightFromRaw`), so calibration is per scale
channel and stored in the hive registry.

### NAU7802 sleep before deep sleep

Before the ESP32 enters deep sleep, `powerDownScalesForSleep()`
(`firmware/src/storage_power.cpp`) calls `scalebus::powerDownAllForSleep()`, which
walks the registry and issues `powerDown()` to every NAU7802 (main bus + each mux
channel). Without this the ADC keeps converting and draws milliamps for the whole
sleep window.

## Wired temperature: up to 16 DS18B20

All DS18B20 probes share the single `ONE_WIRE_PIN` bus. Instead of reading by
index (old behaviour), each hive maps a probe by its **ROM address**, so a
specific probe always belongs to a specific hive regardless of bus enumeration
order. Map probes in the portal after an I2C/1-Wire scan. (On the XIAO ESP32-C6
the 1-Wire bus lives on **D1** — the V0.4 Scale Module PCB wires a DS18B20
header there.)

---

## Hive-centric provisioning portal

The setup portal (AP mode → `http://192.168.4.1/`) is now organised **by hive**:

1. An **I2C scan** runs when the page loads, enumerating NAU7802 channels (main
   bus + each mux channel) and DS18B20 ROMs; see **I2C scan details** to verify
   wiring. A **BLE scan** runs once when the portal starts, so every MAC field
   comes with a dropdown of the discovered devices (name + MAC + detected sensor
   type + RSSI). Picking a device fills the MAC field (and pre-selects the
   sensor type when it was recognised); the field stays editable for sensors
   that are currently powered off. The **🔄 Rescan BLE sensors** button on the
   setup page refreshes the dropdowns in place **without losing unsaved hive
   entries** (unlike navigating to the standalone `/ble/scan` page).
2. **➕ Add hive** creates a hive card.
3. Inside each hive: choose exactly one **Scale** source from the dropdown. The
   portal offers HX711 (1), HX711 (2), detected NAU7802 channels, and **BLE
   HiveScale from Beehivemonitoring**; when the BLE HiveScale option is selected,
   a MAC-address field (with the same device dropdown) appears for pairing. The
   **In-hive sensor** section allows exactly one non-scale sensor per hive:
   **➕ Add BLE sensor** or **➕ Add DS18B20**.
4. **Save and reboot** writes one compact JSON blob per hive to NVS
   (`h0_cfg`..`h17_cfg` + `hive_count`).

On the first boot after upgrading from a pre-0.20 build, the old two-slot config
(scale offsets, paired BLE/GATT MACs) is **migrated automatically** into a
two-hive registry, so an existing device keeps working until you remap it.

---

## Pre-seeding the registry from secrets.h (optional)

The portal is the recommended way to set this up, but a brand-new device can
also ship already knowing every hive on **first boot**, before it has ever
visited the portal: set `HIVE_COUNT` (1..16) and one `HIVE_<n>_JSON` macro per
hive in `secrets.h`, in exactly the JSON shape `hiveToJson()` /
`hiveFromJson()` use (`firmware/src/hive_config.cpp`). The
[website config tool](../website/configurator.html) builds these for you from
a per-hive form (scale source, DS18B20 ROM or one wireless sensor) — see
`firmware/include/secrets.example.h` for the full macro reference and a
worked example. Once a device saves anything from the on-device portal, these
macros are never consulted again; leaving `HIVE_COUNT` at its default (`0`)
keeps the pre-0.20 two-slot migration behavior described above.

---

## BLE budget

A passive BLE **scan** hears every nearby **beacon** in one window. The portal
now limits each hive to one non-scale in-hive BLE/GATT sensor, matching the
backend's single nested `ble` object per `hive_readings` row; up to 16 hives can
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
  consumers (HivePal, MQTT, the insight detectors) reach every hive without
  changes. Insights run for every hive index reported.

---

## Upgrade order

1. **Deploy the updated server first** (run migration `012_multi_hive.sql`, or just
   restart — `init_db()` creates the table idempotently). An old server would drop
   the `hives` array (`extra="ignore"`) and store nothing per-hive.
2. **Then flash firmware v0.20.0** and remap hives in the portal.

## Known limitations

- Connection-based **HiveHeart / wireless HiveScale** GATT sensors and
  **HiveTraffic** wireless bee counters are all read for **any hive**
  (`beehive_gatt.cpp` and `bee_counter_client.cpp::bleRunCycleRegistry` each
  walk the whole registry), same as the passive beacons. Only the **wired I2C
  BeeCounter** stays limited to hives 1–2, since it has just two fixed I2C
  addresses (`0x30` / `0x31`).
- Server-side **temperature compensation** is applied to hives 1–2; hives 3+
  use raw weight for insights.
- **Scale calibration** is synced for all hives (firmware 0.23.7+): hives 1–2 via
  the legacy `scale1/2_offset`+`factor` config fields and hives 3+ via the
  `hive_scales` array (stored server-side in `device_configs.scale_offsets_by_hive`).
  A tare/span done offline on the provisioning portal is reported back to the
  backend on the next check-in and bridged into the registry over remote config.
