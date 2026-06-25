// beehive_gatt.cpp — NimBLE GATT client for HiveHeart / HiveScale. See header.
#include "beehive_gatt.h"

#if ENABLE_BEEHIVE_GATT

#include "hive_config.h"
#include <NimBLEDevice.h>
#include <cctype>

namespace bhgatt {

namespace {

// One notification's worth of payload, captured by the subscribe callback.
// The callback runs on the NimBLE host task while runCycle() polls from the
// main loop, so every access to the shared buffer is guarded by a spinlock to
// give the reader a consistent snapshot (and a memory barrier) rather than
// relying on `volatile` alone.
struct NotifyCapture {
  bool     got = false;
  uint8_t  buf[64];
  size_t   len = 0;
};
NotifyCapture g_capture;
portMUX_TYPE  g_captureMux = portMUX_INITIALIZER_UNLOCKED;

void onNotify(NimBLERemoteCharacteristic* /*chr*/, uint8_t* data, size_t len, bool /*isNotify*/) {
  portENTER_CRITICAL(&g_captureMux);
  if (!g_capture.got) {  // keep only the first notification of the cycle
    size_t n = len > sizeof(g_capture.buf) ? sizeof(g_capture.buf) : len;
    for (size_t i = 0; i < n; i++) g_capture.buf[i] = data[i];
    g_capture.len = n;
    g_capture.got = true;
  }
  portEXIT_CRITICAL(&g_captureMux);
}

// Cleanly close and free a GATT client.
//
// NimBLEDevice::deleteClient() only frees a client *synchronously* once it is
// disconnected; called on a still-connected client it merely flags
// deleteOnDisconnect and leaves the client in NimBLE's internal list. That
// leftover link is then terminated by runCycle()'s NimBLEDevice::deinit() — but
// by then the host stack is already disabled, so ble_gap_terminate() returns
// BLE_HS_EDISABLED and the library logs "ble_gap_terminate failed: rc=30".
//
// So rather than a fixed delay and hope, we drive the teardown to completion:
// ask the link to terminate (idempotent — the library tolerates ENOTCONN /
// EALREADY if the peer already dropped it) and then wait for the NimBLE host
// task to process the disconnect event (which clears the connection handle, so
// isConnected() flips false) before deleting. The client object stays alive
// while we poll because we have not set deleteOnDisconnect ourselves.
void closeClient(NimBLEClient* client) {
  if (!client) return;
  if (client->isConnected()) client->disconnect();

  uint32_t deadline = millis() + BEEHIVE_GATT_DISCONNECT_TIMEOUT_MS;
  while (client->isConnected() && (int32_t)(deadline - millis()) > 0) delay(20);

  if (client->isConnected()) {
    // Peer is holding the link open past our budget. Free it anyway — NimBLE
    // finishes the teardown on the eventual disconnect event. Logged, not
    // hidden, so a genuinely stuck peer is still visible in the serial log.
    Serial.println("[BHGATT] link still up after disconnect timeout; freeing client");
  }
  NimBLEDevice::deleteClient(client);
}

// Connect to `mac`, subscribe to the configured notify characteristic, and wait
// up to BEEHIVE_GATT_NOTIFY_TIMEOUT_S for one notification. On success copies the
// raw payload into `outBuf`/`outLen` and returns the connection RSSI via `rssi`.
bool readNotification(const String& mac, uint8_t* outBuf, size_t cap, size_t& outLen, int& rssi) {
  if (mac.length() == 0) return false;

  portENTER_CRITICAL(&g_captureMux);
  g_capture.got = false;
  g_capture.len = 0;
  portEXIT_CRITICAL(&g_captureMux);

  NimBLEClient* client = NimBLEDevice::createClient();
  // NimBLE 2.x setConnectTimeout() is in milliseconds (1.x used seconds).
  client->setConnectTimeout((uint32_t)BEEHIVE_GATT_CONNECT_TIMEOUT_S * 1000UL);

  // beehivemonitoring devices may advertise with a public OR a random/static
  // address. We don't pre-scan the beehive path, so try public first and fall
  // back to random rather than failing outright on the address type alone.
  const uint8_t addrTypes[2] = { BLE_ADDR_PUBLIC, BLE_ADDR_RANDOM };
  bool connected = false;
  for (int t = 0; t < 2 && !connected; t++) {
    NimBLEAddress addr(std::string(mac.c_str()), addrTypes[t]);
    Serial.printf("[BHGATT] connecting to %s (addr type %u) ...\n", mac.c_str(), addrTypes[t]);
    if (client->connect(addr)) {
      connected = true;
    } else {
      Serial.printf("[BHGATT] connect failed (addr type %u)\n", addrTypes[t]);
    }
  }
  if (!connected) {
    NimBLEDevice::deleteClient(client);
    return false;
  }
  rssi = client->getRssi();

  bool ok = false;
  NimBLERemoteService* svc = client->getService(NimBLEUUID(BEEHIVE_GATT_SERVICE_UUID));
  if (!svc) {
    Serial.println("[BHGATT] service not found");
  } else {
    NimBLERemoteCharacteristic* chr = svc->getCharacteristic(NimBLEUUID(BEEHIVE_GATT_CHAR_UUID));
    if (!chr || !chr->canNotify()) {
      Serial.println("[BHGATT] notify characteristic unavailable");
    } else if (!chr->subscribe(true, onNotify)) {
      Serial.println("[BHGATT] subscribe failed");
    } else {
      uint32_t deadline = millis() + (uint32_t)BEEHIVE_GATT_NOTIFY_TIMEOUT_S * 1000UL;
      bool got = false;
      while (!got && (int32_t)(deadline - millis()) > 0) {
        portENTER_CRITICAL(&g_captureMux);
        got = g_capture.got;
        portEXIT_CRITICAL(&g_captureMux);
        if (!got) delay(20);
      }
      if (got) {
        // Copy the captured payload out under the lock for a consistent snapshot.
        portENTER_CRITICAL(&g_captureMux);
        outLen = g_capture.len > cap ? cap : g_capture.len;
        for (size_t i = 0; i < outLen; i++) outBuf[i] = g_capture.buf[i];
        portEXIT_CRITICAL(&g_captureMux);
        ok = true;
      } else {
        Serial.println("[BHGATT] no notification within timeout");
      }
      chr->unsubscribe();
    }
  }

  // These sensors push one notification then drop the link themselves. Close
  // the link deterministically and wait for it to actually go down before
  // freeing the client, so nothing is left for runCycle()'s deinit() to trip
  // over (see closeClient() for why that produced "ble_gap_terminate rc=30").
  closeClient(client);
  return ok;
}

}  // namespace

String normalizeMac(const String& raw) {
  String hex;
  for (size_t i = 0; i < raw.length(); i++) {
    char c = raw[i];
    if (isxdigit((int)c)) hex += (char)toupper((int)c);
  }
  if (hex.length() != 12) return String("");
  String out;
  for (int i = 0; i < 12; i += 2) {
    if (i) out += ':';
    out += hex.substring(i, i + 2);
  }
  return out;
}

void runCycle(CycleResult& out) {
  out = CycleResult{};

  bool anyPaired = false;
  for (uint8_t h = 0; h < hivecfg::gHiveCount && h < MAX_HIVES; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t b = 0; b < hive.bleCount; b++) {
      const hivecfg::BlePairing& p = hive.ble[b];
      if ((p.type == "hiveheart" || p.type == "hivescale") && p.mac.length()) {
        anyPaired = true;
      }
    }
  }
  if (!anyPaired) {
    Serial.println("[BHGATT] No HiveHeart/HiveScale paired; skipping");
    return;
  }

  NimBLEDevice::init("");
  uint8_t buf[64];
  size_t len = 0;
  int rssi = 0;
  uint8_t readAttempts = 0;

  for (uint8_t h = 0; h < hivecfg::gHiveCount && h < MAX_HIVES; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t b = 0; b < hive.bleCount; b++) {
      const hivecfg::BlePairing& p = hive.ble[b];
      const bool isHeart = p.type == "hiveheart";
      const bool isScale = p.type == "hivescale";
      if ((!isHeart && !isScale) || p.mac.length() == 0) continue;

      if (readAttempts >= MAX_GATT_READS_PER_CYCLE) {
        Serial.printf("[BHGATT] GATT read budget exhausted (%u); skipping hive %u %s %s\n",
                      (unsigned)MAX_GATT_READS_PER_CYCLE, hive.index,
                      p.type.c_str(), p.mac.c_str());
        continue;
      }
      readAttempts++;

      if (!readNotification(p.mac, buf, sizeof(buf), len, rssi)) continue;

      if (isHeart) {
        if (decodeHeart(buf, len, out.heart[h])) {
          out.heart[h].rssi_dbm = rssi;
          Serial.printf("[BHGATT] Hive%u Heart: T=%.1f H=%.1f%% f=%.1fHz V=%.3f RSSI=%d\n",
                        hive.index, out.heart[h].temp_c, out.heart[h].humidity_pct,
                        out.heart[h].frequency_hz, out.heart[h].battery_v, rssi);
        } else {
          Serial.printf("[BHGATT] Hive%u Heart decode failed (%u B)\n",
                        hive.index, (unsigned)len);
        }
      } else if (isScale) {
        if (decodeScale(buf, len, out.scale[h])) {
          out.scale[h].rssi_dbm = rssi;
          Serial.printf("[BHGATT] Hive%u Scale: W=%.2fkg T=%.1f P=%.1fhPa V=%.3f RSSI=%d\n",
                        hive.index, out.scale[h].weight_kg, out.scale[h].temp_c,
                        out.scale[h].pressure_hpa, out.scale[h].battery_v, rssi);
        } else {
          Serial.printf("[BHGATT] Hive%u Scale decode failed (%u B)\n",
                        hive.index, (unsigned)len);
        }
      }
    }
  }

  NimBLEDevice::deinit(true);
}

void writeToJson(JsonDocument& doc, const CycleResult& r) {
  for (uint8_t h = 0; h < hivecfg::gHiveCount && h < MAX_HIVES; h++) {
    const HeartReading& heart = r.heart[h];
    if (!heart.present) continue;
    uint8_t s = hivecfg::gHives[h].index;
    if (s == 0) continue;
    String pfx = String("hiveheart_") + s + "_";
    // temp/humidity are also fed into hives[] by sensors.cpp, but only when no
    // higher-priority wired/beacon source already filled those. Publishing the
    // raw HiveHeart values here keeps the GATT reading independently visible in
    // the serial JSON and in backends that accept the flat compatibility fields.
    doc[pfx + "temp_c"]           = heart.temp_c;
    doc[pfx + "humidity_percent"] = heart.humidity_pct;
    doc[pfx + "frequency_hz"]     = heart.frequency_hz;
    doc[pfx + "energy"]           = heart.energy;
    doc[pfx + "peak"]             = heart.peak;
    doc[pfx + "battery_v"]        = heart.battery_v;
    doc[pfx + "rssi_dbm"]         = heart.rssi_dbm;
    if (heart.fft_present) {
      JsonArray fft = doc[pfx + "fft"].to<JsonArray>();
      for (int j = 0; j < 8; j++) fft.add(heart.fft[j]);
    }
  }
  for (uint8_t h = 0; h < hivecfg::gHiveCount && h < MAX_HIVES; h++) {
    const ScaleReading& sc = r.scale[h];
    if (!sc.present) continue;
    uint8_t s = hivecfg::gHives[h].index;
    if (s == 0) continue;
    String pfx = String("hivescale_") + s + "_";
    doc[pfx + "weight_kg"]        = sc.weight_kg;
    doc[pfx + "raw_weight"]       = sc.raw_weight;
    doc[pfx + "temp_c"]           = sc.temp_c;
    doc[pfx + "humidity_percent"] = sc.humidity_pct;
    doc[pfx + "pressure_hpa"]     = sc.pressure_hpa;
    doc[pfx + "battery_v"]        = sc.battery_v;
    doc[pfx + "rssi_dbm"]         = sc.rssi_dbm;
  }
}

}  // namespace bhgatt

#endif  // ENABLE_BEEHIVE_GATT
