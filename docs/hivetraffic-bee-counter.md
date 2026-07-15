# HiveTraffic — wireless entrance bee counter (BLE/GATT)

HiveTraffic is the entrance bee counter (the `2026-easy-bee-counter` /
*HiveTraffic* board) read over Bluetooth LE. **BLE/GATT is the only supported
BeeCounter transport** — the old wired I2C BeeCounter path (slave addresses
fixed slave addresses, register polling, a latch/reset command, firmware
updates over the wire) has
been removed from the firmware, server and portal entirely. There is no wired
fallback: a hive without a paired (and reachable) HiveTraffic counter simply
reports no bee-counter data (`bee_counter.ok=false` when paired but
unreachable; nothing at all when unpaired).

Once per upload cycle HiveHub acts as a **GATT client**: it connects to each
paired HiveTraffic MAC, reads one JSON measurement characteristic and folds the
counts into the `bee_counter_{slot}_*` fields.

**Any hive can have a HiveTraffic counter.** The counter is resolved from the
dynamic hive registry (`bee_counter_client.cpp::bleRunCycleRegistry` walks
`hivecfg::gHives[]` for `beecounter` pairings), exactly like the HiveHeart /
HiveScale GATT client, so it works on any hive up to `MAX_HIVES`.
Connection-based reads share the `MAX_GATT_READS_PER_CYCLE` per-cycle budget.

## Firmware updates

There is currently **no remote BeeCounter firmware-update path**:

* BeeCounter firmware update over I2C has been **removed** (together with the
  whole wired path). The firmware explicitly rejects the obsolete
  `update_beecounter` command should an old server still queue one.
* BeeCounter firmware update over **GATT is planned but not implemented yet**.
* Until GATT OTA exists, BeeCounter firmware is updated locally (USB) on the
  counter itself.

## Enabling

Build with `ENABLE_WIRELESS_BEECOUNTER=1` (the
[configurator](../website/configurator.html) emits this when you add a
*HiveTraffic* wireless sensor), then pair each counter's MAC:

* in the **provisioning portal** — add an in-hive sensor to any hive, pick
  *HiveTraffic counter*, and enter/copy its MAC; or
* seed it in `secrets.h` via a `HIVE_i_JSON` blob's `bl` entry
  (`{"t":"beecounter","m":"AA:BB:CC:DD:EE:FF"}`) for any hive, or via the legacy
  `WBEECNT_1_MAC` / `WBEECNT_2_MAC` macros for hives 1–2.

Portal pairings live in the hive registry; the legacy `WBEECNT_n_MAC` seeds and
`counter_mac{0,1}` keys are migrated into the registry on first boot.

## GATT contract

All HiveTraffic devices share one service/characteristic (overridable via
`BEECOUNTER_GATT_SERVICE_UUID` / `BEECOUNTER_GATT_CHAR_UUID`):

| | UUID |
| --- | --- |
| Service | `8e8b0101-7a1c-4b9e-9a2f-1d6e0b9c1a01` |
| Measurement characteristic (READ) | `8e8b0102-7a1c-4b9e-9a2f-1d6e0b9c1a01` |

The characteristic returns a compact JSON document — **totals only**:

```json
{ "fw":2, "uptime_s":1234, "status":15, "num_gates":24,
  "gates_healthy":3, "total_in":100, "total_out":95, "glitches":2 }
```

HiveHub reads it, fills a totals-only `beecnt::Snapshot`, and disconnects.
The wire format is totals-only by design: no latch/reset command exists over
BLE, so a missed connection can never lose counts.

## Intervals are differenced server-side

The wire format carries only the **monotonic lifetime totals**. The backend
derives each interval as `total_now − total_prev` between consecutive readings,
so a missed connection loses nothing and a counter reboot (totals going
backwards) is handled cleanly. See `2026-easy-bee-counter/docs/ble-mode.md` for
the device side and the rationale.

This differencing happens in two places, with identical semantics:

* The insight engine (`server/insights.py::_extract_counter_series`), which
  feeds the swarm/foraging detectors and the insight cards.
* The measurement read APIs (`server/measurements.py::difference_bee_counter_intervals`,
  applied by `serialize_measurements`), which backfill the `NULL`
  `interval_in`/`interval_out` columns from the totals before returning rows.
  Without this, display clients that chart the interval fields directly (HivePal's
  bee-counter panel) would read every BLE row as zero traffic. Only `NULL`
  intervals are filled, so historical rows from the removed wired path — which
  reported a real device interval — are left untouched.

Because the BLE path reports no per-interval or per-gate detail, those columns
arrive `NULL` for HiveTraffic readings; the derived interval is authoritative.

## Relationship to the other BLE subsystems

HiveHub already connects out over GATT for HiveInside and the
beehivemonitoring.com sensors (see `beehivemonitoring-gatt.md`). HiveTraffic
reuses the same connect-by-MAC pattern (`bee_counter_client.cpp::bleRunCycleRegistry`,
modelled on `beehive_gatt.cpp`), bringing the NimBLE stack up once per cycle for
the paired counters and tearing it down afterwards.
