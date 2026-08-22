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
// Firmware updates run over GATT as well: the backend queues an
// `update_beecounter` command and HiveHub streams the image into the counter's
// OTA characteristics (see gatt_ota.h and hivehub_network.cpp). That path is
// entirely separate from the measurement read below — it opens its own session
// and shares nothing but the service UUID.

#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"   // ENABLE_WIRELESS_BEECOUNTER + BEECOUNTER_GATT_* UUIDs

namespace beecnt {

// One totals-only reading from a HiveTraffic counter, taken each upload cycle.
// Field names mirror the HiveTraffic measurement JSON (docs/ble-mode.md in the
// 2026-easy-bee-counter repo), normalized across wire revisions — see
// bee_counter_wire.h, which owns the fw:2 / fw:3 branch and the decoding.
struct Snapshot {
    bool     present          = false;  // device connected and parsed this cycle
    uint8_t  fw_version       = 0;      // wire-protocol revision ("fw")
    // Image version ("ver", e.g. "0.1.0"), empty when the counter runs firmware
    // older than the field. NOT the same thing as fw_version above: that one
    // versions the wire format and does not move when the firmware does. This
    // is what the backend gates an OTA relay on and what confirms, after the
    // counter reboots, that the update actually took. Fixed buffer rather than
    // a String: Snapshot is used as a MAX_HIVES-sized stack array.
    //
    // Because it is a char[] and the array is a stack local, anything putting it
    // into a JsonDocument must copy it (writeSnapshotToHive wraps it in
    // String()): ArduinoJson links a const char* by pointer, and the document
    // outlives this struct.
    char     version[16]      = {0};
    uint8_t  status_flags     = 0;
    // 32-bit as of wire revision 3. A uint16_t here would re-impose the exact
    // ceiling the counter was widened to escape: it clamped at 65535 s
    // (18 h 12 min), so an always-on counter reported a constant and an
    // unexpected reset was undetectable. A fw:2 counter still cannot report
    // more than 65535, but that is now the device's limit, not ours.
    uint32_t uptime_s         = 0;
    uint8_t  num_gates        = 0;
    // MCP23017 port expanders on the counter answering its I2C bus, 0..3 — NOT
    // gates, of which there are num_gates (24); each healthy expander covers
    // eight. Called "gates_healthy" on the wire before revision 3, which is
    // precisely the misreading the rename fixed. LIVE health, not a boot-time
    // snapshot: it moves during a deployment as chips fail and recover.
    uint8_t  mcps_healthy     = 0;
    uint32_t total_in         = 0;      // lifetime totals — never reset by us
    uint32_t total_out        = 0;
    // Seconds of night-mode suspension still to run on the counter ("idle_s",
    // wire revision 4+); 0 while it is counting, and 0 for any counter too old
    // to report it. Non-zero says the flat interval that follows is deliberate
    // — we asked for it — rather than a failed emitter bank.
    uint32_t idle_s           = 0;
    // Which of the counter's three emitter MOSFETs are enabled ("banks", wire
    // revision 5+): bit 0 = gates 00..07, bit 1 = 10..17, bit 2 = 20..27.
    //
    // BEECOUNTER_BANK_MASK_ALL for any counter too old to report it, which is
    // the truth — a pre-v5 counter has no way to switch a bank off. A cleared
    // bit means eight gates are dark and not being counted, so their share of
    // the totals is permanently flat; without this field that is
    // indistinguishable from the FET having failed, which is the same problem
    // idle_s solves for the whole counter.
    uint8_t  bank_mask        = BEECOUNTER_BANK_MASK_ALL;
    // 32-bit as of revision 3, saturating on the device rather than wrapping.
    uint32_t glitch_count     = 0;
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
