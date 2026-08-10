// ble_stack.cpp — see ble_stack.h for the teardown rule this enforces.
#include "ble_stack.h"

#if ENABLE_BLE_SCAN || ENABLE_WIRELESS_BEECOUNTER || ENABLE_BEEHIVE_GATT || \
    GATT_OTA_ENABLED

#include <NimBLEDevice.h>

namespace blestack {
namespace {
bool     s_up = false;
uint32_t s_generation = 0;
// Port lifetime in which a scan last ran. 0 = no scan yet this boot.
uint32_t s_scanGeneration = 0;
}  // namespace

void acquire() {
  if (!s_up) {
    s_generation++;
    s_up = true;
    if (s_generation > 1) {
      Serial.printf("[BLE] stack up (port lifetime %u)\n",
                    (unsigned)s_generation);
    }
  }
  // NimBLEDevice::init() is itself idempotent; calling it unconditionally keeps
  // acquire() correct even if something outside this module brought the stack
  // up first.
  NimBLEDevice::init("");
}

void release() {
  if (!s_up) return;
  // deinit(false), never (true): deinit() runs nimble_port_deinit() before it
  // deletes the singletons, so deinit(true) destroys NimBLEScan against a
  // porting layer that is already gone and faults inside ~NimBLEScan(). The
  // controller is fully freed either way, which is what the WiFi upload needs.
  NimBLEDevice::deinit(false);
  s_up = false;
}

bool scanAllowed() {
  // Never scanned: any lifetime is fine, and this one will own the singleton.
  // Scanned before: only the lifetime that constructed the singleton is safe.
  return s_scanGeneration == 0 || s_scanGeneration == s_generation;
}

void noteScanStarted() {
  s_scanGeneration = s_generation;
}

uint32_t generation() {
  return s_generation;
}

uint32_t scanGeneration() {
  return s_scanGeneration;
}

}  // namespace blestack

#endif
