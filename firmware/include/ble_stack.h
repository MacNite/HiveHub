// ble_stack.h — one owner for the NimBLE stack's lifetime.
//
// The hub brings NimBLE up and down several times per wake cycle: the
// measurement scan, the HiveHeart/HiveScale GATT reads, the HiveTraffic reads,
// the portal's discovery scan, and the firmware relay each did their own
// init/deinit pair. That is four copies of the same subtle teardown rule, and a
// field crash showed the rule was not merely subtle but wrong.
//
// ── What actually goes wrong ────────────────────────────────────────────────
//
// `NimBLEScan` owns a scan-response timer, `ble_npl_callout m_srTimer`, and
// initialises it **once in its constructor** — bound to the porting layer that
// existed at that moment. It is torn down only in the destructor.
//
// `NimBLEDevice::deinit(clearAll)` calls `nimble_port_deinit()` FIRST and only
// then, if `clearAll`, deletes the singletons. So `deinit(true)` destroys
// `NimBLEScan` after the porting layer is gone and faults inside
// `~NimBLEScan()`. That is why every call site here passes `false`.
//
// But `deinit(false)` keeps the singleton alive with a callout that belonged to
// the porting layer just destroyed. Bring NimBLE back up in the same boot, run
// another scan, and the first `ble_npl_callout_stop(&m_srTimer)` — which every
// scan reaches, via `stop()` or the `BLE_GAP_EVENT_DISC_COMPLETE` handler —
// hands a freed handle to `esp_timer_stop()`:
//
//     Guru Meditation Error: Core 0 panic'ed (Load access fault)
//     MEPC 0x408090c6 = esp_timer_stop at esp_timer.c:257  (timer->flags)
//     MTVAL = the stale handle, non-null but long gone
//
// The old note in ble_sensor.cpp assumed the retained singleton's "callout
// survives a port re-init". It does not, and that assumption is what crashed a
// hub mid-relay: the measurement scan ran (port lifetime 1), the port was torn
// down for the WiFi upload, and the relay's locate scan ran in lifetime 2.
//
// ── The rule this module enforces ──────────────────────────────────────────
//
// A scan is only safe in the port lifetime the `NimBLEScan` singleton was
// constructed in. Since the library exposes no way to delete that singleton
// while the port is still alive, the invariant cannot be repaired after the
// fact — it can only be respected. So: count port lifetimes, remember which one
// scanned, and let callers ask before scanning instead of finding out by
// crashing.
//
// Connection-only work (GATT client reads, the OTA relay) never touches
// NimBLEScan and stays safe in any lifetime.
#pragma once

#include <Arduino.h>

#include "config.h"

// Every consumer is itself behind one of these flags, so the guard is their
// union: ble_sensor.cpp (ENABLE_BLE_SCAN), bee_counter_client.cpp
// (ENABLE_WIRELESS_BEECOUNTER), beehive_gatt.cpp (ENABLE_BEEHIVE_GATT) and
// gatt_ota.cpp (GATT_OTA_ENABLED).
#if ENABLE_BLE_SCAN || ENABLE_WIRELESS_BEECOUNTER || ENABLE_BEEHIVE_GATT || \
    GATT_OTA_ENABLED

namespace blestack {

// Bring the stack up if it is not already, starting a new port lifetime when it
// had been released. Idempotent.
void acquire();

// Release the stack, freeing the BT controller so WiFi has the radio.
// Always `deinit(false)` — see the header comment for why `true` is unusable.
void release();

// True when a scan may be started in the current port lifetime. False once the
// port has been torn down and re-initialised after a scan, because the retained
// NimBLEScan singleton's callout belongs to the previous lifetime.
bool scanAllowed();

// Called by the scan helpers just before starting one, so this module knows
// which port lifetime owns the singleton.
void noteScanStarted();

// Current port lifetime, counting from 1 at the first acquire(). Diagnostics.
uint32_t generation();

// Port lifetime that owns the NimBLEScan singleton, or 0 if nothing has
// scanned yet this boot. Diagnostics.
uint32_t scanGeneration();

}  // namespace blestack

#endif
