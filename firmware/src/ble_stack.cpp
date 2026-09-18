// ble_stack.cpp — see ble_stack.h for the teardown rule this enforces.
#include "ble_stack.h"

#if ENABLE_BLE_SCAN || ENABLE_WIRELESS_BEECOUNTER || ENABLE_BEEHIVE_GATT || \
    GATT_OTA_ENABLED

#include <NimBLEDevice.h>
#include <esp_bt.h>

namespace blestack {
namespace {
bool     s_up = false;
uint32_t s_generation = 0;
// Port lifetime in which a scan last ran. 0 = no scan yet this boot.
uint32_t s_scanGeneration = 0;

// Give back what a FAILED NimBLEDevice::init() left behind.
//
// init() brings the ESP-IDF controller up — esp_bt_controller_init() then
// esp_bt_controller_enable() — before it reaches the NimBLE host, so a failure
// at the host layer returns false with the controller still holding its
// allocation. Nothing unwinds it on its own: NimBLEDevice::deinit() guards on
// the library's own m_initialized flag, which a failed init never set, so it is
// a no-op here; and release() below never runs because s_up was never set
// either. The memory is stranded until the next reset.
//
// A field log from a 30-pin hub shows exactly what that costs. The audio relay
// asked for the radio with WiFi and a TLS session resident, got
//
//     E NimBLEDevice: esp_nimble_hci_init() failed; err=257   (ESP_ERR_NO_MEM)
//
// and the cycle went on: free heap 91 kB at the start of the attempt, 71 kB at
// the end of the cycle with everything else torn down, and a minimum free heap
// for the cycle of 1456 bytes. The failed recording was the harmless half of
// that. The command-result POST and the OTA check that follow it each open a
// fresh TLS session, and they ran on ~1.4 kB of headroom.
//
// Deliberately NOT esp_bt_controller_mem_release(): that hands the BLE memory
// back permanently and the next cycle's measurement scan would find no radio.
// Disable + deinit returns the controller to IDLE, where a later acquire() —
// this cycle's or the next one's — can try again.
void unwindFailedInit() {
  if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_ENABLED) {
    esp_bt_controller_disable();
  }
  if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_INITED) {
    esp_bt_controller_deinit();
  }
}
}  // namespace

bool acquire() {
  // NimBLEDevice::init() is itself idempotent; calling it unconditionally keeps
  // acquire() correct even if something outside this module brought the stack
  // up first. It returns false when esp_bt_controller_init() or _enable()
  // could not get the memory they need — which is a real possibility on the
  // classic ESP32, whose BT controller wants tens of kilobytes and which has
  // far less DRAM than the C6 to begin with. The audio relay in particular asks
  // for the stack with WiFi already connected — which is why it now asks BEFORE
  // opening its TLS session rather than after (gatt_audio::acquireRadio), so
  // the one allocation here that cannot shrink is not also the last in line.
  //
  // Returning here rather than pressing on is the point: none of NimBLE's host
  // API is usable after a failed init, and calling getScan() or createClient()
  // against an uninitialised host is a fault, not an error code.
  if (!NimBLEDevice::init("")) {
    Serial.println("[BLE] stack failed to start (no memory for the controller?)");
    // s_up false means no lifetime of ours is running, so there is nothing live
    // to pull out from under: whatever init() got as far as allocating is
    // unreachable and ours to give back. (A stack that WAS already up makes
    // init() return true, so this branch cannot be reached with s_up set --
    // the guard is there so a future caller cannot make it so.)
    if (!s_up) unwindFailedInit();
    return false;
  }
  if (!s_up) {
    s_generation++;
    s_up = true;
    if (s_generation > 1) {
      Serial.printf("[BLE] stack up (port lifetime %u)\n",
                    (unsigned)s_generation);
    }
  }
  return true;
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

bool scanWouldBeAllowed() {
  // Nothing has scanned yet: whichever lifetime scans first will own the
  // singleton, so a scan is safe either way.
  if (s_scanGeneration == 0) return true;
  // Something has scanned. Only the lifetime that is still up and owns the
  // singleton may scan again — if the stack is currently released, the next
  // acquire() starts a new lifetime and the answer becomes no.
  return s_up && s_scanGeneration == s_generation;
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
