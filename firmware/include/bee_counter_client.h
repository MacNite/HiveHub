// bee_counter_client.h — HiveTraffic entrance bee counter, BLE/GATT ONLY.
//
// The wired I2C BeeCounter path (its fixed slave addresses, register map,
// latch/reset command, snapshot retries, and firmware-over-the-wire updates)
// was removed: BLE/GATT is the only supported BeeCounter transport. Once per
// upload cycle the firmware connects
// to each paired HiveTraffic MAC, reads one JSON measurement characteristic —
// LIFETIME totals only — and disconnects. The backend differences consecutive
// totals into per-interval counts, so no latch/reset command exists.
//
// Firmware updates: BeeCounter OTA will eventually run over GATT, but it is
// NOT implemented yet. There is currently no remote BeeCounter firmware-update
// path; the obsolete update_beecounter server command is rejected explicitly
// in checkCommands() (hivehub_network.cpp).

#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"   // ENABLE_WIRELESS_BEECOUNTER + BEECOUNTER_GATT_* UUIDs

namespace beecnt {

// One totals-only reading from a HiveTraffic counter, taken each upload cycle.
// Field names mirror the HiveTraffic measurement JSON (docs/ble-mode.md in the
// 2026-easy-bee-counter repo).
struct Snapshot {
    bool     present          = false;  // device connected and parsed this cycle
    uint8_t  fw_version       = 0;      // counter firmware revision ("fw")
    uint8_t  status_flags     = 0;
    uint16_t uptime_s         = 0;
    uint8_t  num_gates        = 0;
    uint8_t  gates_healthy    = 0;
    uint32_t total_in         = 0;      // lifetime totals — never reset by us
    uint32_t total_out        = 0;
    uint16_t glitch_count     = 0;
};

// Per-hive form for the hives[] array: writes a nested "bee_counter" object.
// Interval/per-gate fields are never written — the wire format is totals-only
// and the backend derives intervals by differencing.
void writeSnapshotToHive(JsonObject hive, const Snapshot& snap);

#if ENABLE_WIRELESS_BEECOUNTER
// Registry-driven GATT read: brings the BLE stack up once, then for every hive
// in hivecfg::gHives[] that carries a "beecounter" pairing connects to its MAC,
// reads the JSON measurement characteristic (BEECOUNTER_GATT_*), parses the
// lifetime totals into out[h] (the same array position as gHives[h]), then
// tears the stack down — the same lifecycle and any-hive model as
// bhgatt::runCycle. A hive without a pairing (or with an empty MAC) leaves
// out[h] !present; an unreachable paired counter does too. At most
// MAX_GATT_READS_PER_CYCLE devices are read per cycle; `cap` is the length of
// `out` (pass MAX_HIVES).
void bleRunCycleRegistry(Snapshot* out, uint8_t cap);
#endif

}  // namespace beecnt
