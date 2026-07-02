# HiveTraffic — wireless entrance bee counter (BLE/GATT)

HiveTraffic is the wireless variant of the entrance bee counter (the
`2026-easy-bee-counter` / *HiveTraffic* board). Instead of the wired I2C link,
HiveHub reads it over Bluetooth LE: once per upload cycle it acts as a **GATT
client**, connects to each paired HiveTraffic MAC, reads one JSON measurement
characteristic and folds the counts into the same `bee_counter_{slot}_*` fields
the wired counter uses.

It is a drop-in alternative to the I2C counter: **a slot with a paired
HiveTraffic MAC is read over BLE; a slot without one falls back to the wired I2C
BeeCounter.** Both can coexist (e.g. counter 1 wired, counter 2 wireless).

## Enabling

Build with `ENABLE_WIRELESS_BEECOUNTER=1` (the
[configurator](../website/configurator.html) emits this when you add a
*HiveTraffic* wireless sensor), then pair each counter's MAC:

* in the **provisioning portal** — add a wireless sensor, pick *HiveTraffic*, map
  it to counter 1 or 2, and enter/copy its MAC; or
* seed it in `secrets.h` via `WBEECNT_1_MAC` / `WBEECNT_2_MAC`.

MACs are stored under the portal's `counter_mac{0,1}` keys.

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

HiveHub reads it, fills a totals-only `beecnt::Snapshot`, and disconnects. No
`CMD_LATCH` reset is written.

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
  intervals are filled, so the wired I2C path — which reports a real device
  interval — is left untouched.

Because the BLE path reports no per-interval or per-gate detail, those columns
arrive `NULL` for HiveTraffic readings; the derived interval is authoritative.

## Relationship to the other BLE subsystems

HiveHub already connects out over GATT for HiveInside and the
beehivemonitoring.com sensors (see `beehivemonitoring-gatt.md`). HiveTraffic
reuses the same connect-by-MAC pattern (`bee_counter_client.cpp::bleRunCycle`,
modelled on `beehive_gatt.cpp`), bringing the NimBLE stack up once per cycle for
the wireless counter slots and tearing it down afterwards.
