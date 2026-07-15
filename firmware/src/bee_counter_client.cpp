// bee_counter_client.cpp — HiveTraffic bee counter over BLE/GATT (totals-only).
// See bee_counter_client.h: the wired I2C BeeCounter path no longer exists.

#include "bee_counter_client.h"

#if ENABLE_WIRELESS_BEECOUNTER
#include <NimBLEDevice.h>
#include "hive_config.h"
#endif

namespace beecnt {

void writeSnapshotToHive(JsonObject hive, const Snapshot& snap) {
    // Nested per-hive form for the hives[] array (server maps it onto the
    // hive_readings bee_counter_* columns). Only hives with a configured
    // counter get this object at all; ok:false means "paired but unreachable
    // this cycle". Totals-only: the backend differences consecutive totals
    // into intervals, so interval_*/per_gate_* fields are never emitted.
    JsonObject bc = hive["bee_counter"].to<JsonObject>();
    bc["ok"] = snap.present;
    if (!snap.present) return;

    bc["protocol_version"] = snap.fw_version;   // key kept for server compat
    bc["status_flags"]     = snap.status_flags;
    bc["uptime_s"]         = snap.uptime_s;
    bc["num_gates"]        = snap.num_gates;
    bc["gates_healthy"]    = snap.gates_healthy;
    bc["total_in"]         = snap.total_in;
    bc["total_out"]        = snap.total_out;
    bc["glitch_count"]     = snap.glitch_count;
}

// ---------------------------------------------------------------------------
// HiveTraffic GATT-client read
// ---------------------------------------------------------------------------
#if ENABLE_WIRELESS_BEECOUNTER

namespace {

// Parse the HiveTraffic measurement JSON (see 2026-easy-bee-counter
// docs/ble-mode.md) into a Snapshot. Returns false on malformed JSON. Fields
// absent in the document keep their Snapshot defaults.
bool parseTrafficJson(const char* json, size_t len, Snapshot& out) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json, len);
    if (err) {
        Serial.printf("[TRAFFIC] JSON parse failed: %s\n", err.c_str());
        return false;
    }
    out.present       = true;
    out.fw_version    = doc["fw"]            | 0;
    out.status_flags  = doc["status"]        | 0;
    out.uptime_s      = doc["uptime_s"]      | 0;
    out.num_gates     = doc["num_gates"]     | 0;
    out.gates_healthy = doc["gates_healthy"] | 0;
    out.total_in      = doc["total_in"]      | 0u;
    out.total_out     = doc["total_out"]     | 0u;
    out.glitch_count  = doc["glitches"]      | 0;
    return true;
}

// Connect to `mac`, read the measurement characteristic once, parse it into
// `out`. Tries public then random address type (HiveTraffic advertises with the
// ESP32's default random static address, but seeded MACs may be either).
bool readTrafficSlot(const String& mac, Snapshot& out) {
    if (mac.length() == 0) return false;

    NimBLEClient* client = NimBLEDevice::createClient();
    client->setConnectTimeout((uint32_t)BEECOUNTER_GATT_CONNECT_TIMEOUT_S * 1000UL);

    const uint8_t addrTypes[2] = { BLE_ADDR_PUBLIC, BLE_ADDR_RANDOM };
    bool connected = false;
    for (int t = 0; t < 2 && !connected; t++) {
        NimBLEAddress addr(std::string(mac.c_str()), addrTypes[t]);
        Serial.printf("[TRAFFIC] connecting to %s (addr type %u) ...\n",
                      mac.c_str(), addrTypes[t]);
        if (client->connect(addr)) connected = true;
    }
    if (!connected) {
        Serial.printf("[TRAFFIC] connect failed for %s\n", mac.c_str());
        NimBLEDevice::deleteClient(client);
        return false;
    }

    bool ok = false;
    NimBLERemoteService* svc = client->getService(NimBLEUUID(BEECOUNTER_GATT_SERVICE_UUID));
    if (!svc) {
        Serial.println("[TRAFFIC] service not found");
    } else {
        NimBLERemoteCharacteristic* chr =
            svc->getCharacteristic(NimBLEUUID(BEECOUNTER_GATT_CHAR_UUID));
        if (!chr || !chr->canRead()) {
            Serial.println("[TRAFFIC] measurement characteristic unreadable");
        } else {
            std::string v = chr->readValue();
            if (v.empty()) {
                Serial.println("[TRAFFIC] empty characteristic read");
            } else {
                ok = parseTrafficJson(v.c_str(), v.size(), out);
                if (ok) {
                    Serial.printf("[TRAFFIC] %s: in=%lu out=%lu uptime=%us status=0x%02X\n",
                                  mac.c_str(), (unsigned long)out.total_in,
                                  (unsigned long)out.total_out,
                                  out.uptime_s, out.status_flags);
                }
            }
        }
    }

    // HiveTraffic stays connectable; close the link deterministically and wait
    // for it to drop before freeing the client so nothing is left for the
    // deinit() in bleRunCycleRegistry() to trip over.
    if (client->isConnected()) client->disconnect();
    uint32_t deadline = millis() + BEECOUNTER_GATT_DISCONNECT_TIMEOUT_MS;
    while (client->isConnected() && (int32_t)(deadline - millis()) > 0) delay(20);
    NimBLEDevice::deleteClient(client);
    return ok;
}

}  // namespace

void bleRunCycleRegistry(Snapshot* out, uint8_t cap) {
    for (uint8_t h = 0; h < cap; h++) out[h] = Snapshot{};

    // Nothing to do unless at least one hive has a paired HiveTraffic counter.
    bool anyPaired = false;
    for (uint8_t h = 0; h < hivecfg::gHiveCount && h < cap; h++) {
        const hivecfg::Hive& hive = hivecfg::gHives[h];
        for (uint8_t b = 0; b < hive.bleCount; b++)
            if (hive.ble[b].type == "beecounter" && hive.ble[b].mac.length()) anyPaired = true;
    }
    if (!anyPaired) return;

    NimBLEDevice::init("");
    uint8_t readAttempts = 0;
    for (uint8_t h = 0; h < hivecfg::gHiveCount && h < cap; h++) {
        const hivecfg::Hive& hive = hivecfg::gHives[h];
        for (uint8_t b = 0; b < hive.bleCount; b++) {
            const hivecfg::BlePairing& p = hive.ble[b];
            if (p.type != "beecounter" || p.mac.length() == 0) continue;

            if (readAttempts >= MAX_GATT_READS_PER_CYCLE) {
                Serial.printf("[TRAFFIC] GATT read budget exhausted (%u); skipping hive %u %s\n",
                              (unsigned)MAX_GATT_READS_PER_CYCLE, hive.index, p.mac.c_str());
                break;
            }
            readAttempts++;
            (void)readTrafficSlot(p.mac, out[h]);
            break;  // at most one HiveTraffic counter per hive
        }
    }
    // deinit(false), not (true): on the ESP32-C6 deinit(true) panics in
    // ~NimBLEScan() once a scan has run this boot, because the scan singleton is
    // deleted after nimble_port_deinit() zeroes npl_funcs. See the detailed note
    // in ble_sensor.cpp (scanPairedSensorsMulti). Controller is still freed.
    NimBLEDevice::deinit(false);
}

#endif  // ENABLE_WIRELESS_BEECOUNTER

}  // namespace beecnt
