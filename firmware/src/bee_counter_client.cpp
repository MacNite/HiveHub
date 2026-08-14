// bee_counter_client.cpp — HiveTraffic bee counter over BLE/GATT (totals-only).
// See bee_counter_client.h: the wired I2C BeeCounter path no longer exists.

#include "bee_counter_client.h"

#if ENABLE_WIRELESS_BEECOUNTER
#include <string.h>       // strlcpy for the counter's reported version
#include <NimBLEDevice.h>
#include "ble_stack.h"
#include "bee_counter_wire.h"   // the fw:2 / fw:3 tolerant document decoder
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
    // Image version, when the counter reports one. Omitted rather than sent
    // empty so the server can tell "counter too old to report a version" from
    // "counter reported an empty string" — the OTA version gate treats an
    // unknown version as "never block", which is only correct for the former.
    if (snap.version[0]) bc["version"] = snap.version;
    bc["status_flags"]     = snap.status_flags;
    bc["uptime_s"]         = snap.uptime_s;
    bc["num_gates"]        = snap.num_gates;
    // Always emitted under the revision-3 name, whichever name the counter used
    // on the wire: the value has the same meaning in both revisions (MCP23017
    // expanders answering, 0..3), and normalizing here means the server and
    // anything reading history sees one key rather than having to branch on
    // protocol_version too. Readings taken before this change carry the old
    // "gates_healthy" key in hive_readings.raw_json, with the same meaning.
    bc["mcps_healthy"]     = snap.mcps_healthy;
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
// docs/ble-mode.md) into a Snapshot. Returns false on malformed JSON or a
// document carrying no "fw". Fields absent in the document keep their Snapshot
// defaults.
//
// The decoding — including the fw:2 / fw:3 branch and the saturating integer
// reads — lives in bee_counter_wire.h so it can be tested on a host compiler
// against captured documents of both revisions
// (test-data/test_bee_counter_wire.cpp). This wrapper is only the copy into
// Snapshot.
bool parseTrafficJson(const char* json, size_t len, Snapshot& out) {
    wire::Measurement m;
    if (!wire::parseMeasurement(json, len, m)) {
        Serial.println("[TRAFFIC] measurement document rejected (malformed or not a measurement)");
        return false;
    }
    out.present       = true;
    out.fw_version    = m.protocol_version;
    strlcpy(out.version, m.version, sizeof(out.version));
    out.status_flags  = m.status_flags;
    out.uptime_s      = m.uptime_s;
    out.num_gates     = m.num_gates;
    out.mcps_healthy  = m.mcps_healthy;
    out.total_in      = m.total_in;
    out.total_out     = m.total_out;
    out.glitch_count  = m.glitch_count;
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
                    // The wire revision is logged too: a counter still on fw=2
                    // is one the OTA relay has not reached yet, and that is the
                    // difference between "old firmware" and "broken link".
                    Serial.printf("[TRAFFIC] %s: fw=%s wire=v%u in=%lu out=%lu "
                                  "uptime=%lus mcps=%u/3 status=0x%02X\n",
                                  mac.c_str(),
                                  out.version[0] ? out.version : "?",
                                  (unsigned)out.fw_version,
                                  (unsigned long)out.total_in,
                                  (unsigned long)out.total_out,
                                  (unsigned long)out.uptime_s,
                                  (unsigned)out.mcps_healthy,
                                  out.status_flags);
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

    blestack::acquire();
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
    // Counter reads only ever connect, so they are safe in any port lifetime.
    // See ble_stack.h for the teardown rule. Controller is still freed.
    blestack::release();
}

#endif  // ENABLE_WIRELESS_BEECOUNTER

}  // namespace beecnt
