// beehive_gatt.cpp — NimBLE GATT client for HiveHeart / HiveScale. See header.
#include "beehive_gatt.h"

#if ENABLE_BEEHIVE_GATT

#include "globals.h"
#include <NimBLEDevice.h>
#include <cctype>

namespace bhgatt {

namespace {

// One notification's worth of payload, captured by the subscribe callback.
struct NotifyCapture {
  volatile bool   got = false;
  uint8_t         buf[64];
  size_t          len = 0;
};
NotifyCapture g_capture;

void onNotify(NimBLERemoteCharacteristic* /*chr*/, uint8_t* data, size_t len, bool /*isNotify*/) {
  if (g_capture.got) return;  // keep only the first notification of the cycle
  size_t n = len > sizeof(g_capture.buf) ? sizeof(g_capture.buf) : len;
  for (size_t i = 0; i < n; i++) g_capture.buf[i] = data[i];
  g_capture.len = n;
  g_capture.got = true;
}

// Connect to `mac`, subscribe to the configured notify characteristic, and wait
// up to BEEHIVE_GATT_NOTIFY_TIMEOUT_S for one notification. On success copies the
// raw payload into `outBuf`/`outLen` and returns the connection RSSI via `rssi`.
bool readNotification(const String& mac, uint8_t* outBuf, size_t cap, size_t& outLen, int& rssi) {
  if (mac.length() == 0) return false;

  g_capture.got = false;
  g_capture.len = 0;

  NimBLEAddress addr(std::string(mac.c_str()), BLE_ADDR_PUBLIC);
  NimBLEClient* client = NimBLEDevice::createClient();
  client->setConnectTimeout(BEEHIVE_GATT_CONNECT_TIMEOUT_S);

  Serial.printf("[BHGATT] connecting to %s ...\n", mac.c_str());
  if (!client->connect(addr)) {
    NimBLEDevice::deleteClient(client);
    Serial.println("[BHGATT] connect failed");
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
      while (!g_capture.got && (int32_t)(deadline - millis()) > 0) delay(20);
      if (g_capture.got) {
        outLen = g_capture.len > cap ? cap : g_capture.len;
        for (size_t i = 0; i < outLen; i++) outBuf[i] = g_capture.buf[i];
        ok = true;
      } else {
        Serial.println("[BHGATT] no notification within timeout");
      }
      chr->unsubscribe();
    }
  }

  client->disconnect();
  NimBLEDevice::deleteClient(client);
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

  const String hearts[2] = { heartMac0, heartMac1 };
  const String scales[2] = { scaleMac0, scaleMac1 };

  bool anyPaired = false;
  for (int i = 0; i < 2; i++) anyPaired |= hearts[i].length() || scales[i].length();
  if (!anyPaired) {
    Serial.println("[BHGATT] No HiveHeart/HiveScale paired; skipping");
    return;
  }

  NimBLEDevice::init("");
  uint8_t buf[64];
  size_t len = 0;
  int rssi = 0;

  for (int i = 0; i < 2; i++) {
    if (!hearts[i].length()) continue;
    if (readNotification(hearts[i], buf, sizeof(buf), len, rssi)) {
      if (decodeHeart(buf, len, out.heart[i])) {
        Serial.printf("[BHGATT] Heart%d: T=%.1f H=%.1f%% f=%.1fHz V=%.3f\n",
                      i + 1, out.heart[i].temp_c, out.heart[i].humidity_pct,
                      out.heart[i].frequency_hz, out.heart[i].battery_v);
      }
    }
  }
  for (int i = 0; i < 2; i++) {
    if (!scales[i].length()) continue;
    if (readNotification(scales[i], buf, sizeof(buf), len, rssi)) {
      if (decodeScale(buf, len, out.scale[i])) {
        Serial.printf("[BHGATT] Scale%d: W=%.2fkg T=%.1f P=%.1fhPa V=%.3f\n",
                      i + 1, out.scale[i].weight_kg, out.scale[i].temp_c,
                      out.scale[i].pressure_hpa, out.scale[i].battery_v);
      }
    }
  }

  NimBLEDevice::deinit(true);
}

void writeToJson(JsonDocument& doc, const CycleResult& r) {
  for (int i = 0; i < 2; i++) {
    const HeartReading& h = r.heart[i];
    if (!h.present) continue;
    uint8_t s = i + 1;
    String pfx = String("hiveheart_") + s + "_";
    doc[pfx + "frequency_hz"] = h.frequency_hz;
    doc[pfx + "energy"]       = h.energy;
    doc[pfx + "peak"]         = h.peak;
    doc[pfx + "battery_v"]    = h.battery_v;
    if (h.fft_present) {
      JsonArray fft = doc[pfx + "fft"].to<JsonArray>();
      for (int j = 0; j < 8; j++) fft.add(h.fft[j]);
    }
  }
  for (int i = 0; i < 2; i++) {
    const ScaleReading& sc = r.scale[i];
    if (!sc.present) continue;
    uint8_t s = i + 1;
    String pfx = String("hivescale_") + s + "_";
    doc[pfx + "weight_kg"]        = sc.weight_kg;
    doc[pfx + "raw_weight"]       = sc.raw_weight;
    doc[pfx + "temp_c"]           = sc.temp_c;
    doc[pfx + "humidity_percent"] = sc.humidity_pct;
    doc[pfx + "pressure_hpa"]     = sc.pressure_hpa;
    doc[pfx + "battery_v"]        = sc.battery_v;
  }
}

}  // namespace bhgatt

#endif  // ENABLE_BEEHIVE_GATT
