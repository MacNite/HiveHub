// bee_counter_client.cpp — see bee_counter_client.h for protocol notes.

#include "bee_counter_client.h"

#if ENABLE_WIRELESS_BEECOUNTER
#include <NimBLEDevice.h>
#include "config.h"
#endif

namespace beecnt {

namespace {

constexpr uint32_t READ_RETRY_DELAY_MS = 50;

// Big-endian decode helpers — the slave writes counters MSB-first.
inline uint16_t readU16BE(const uint8_t* b) {
    return (uint16_t(b[0]) << 8) | uint16_t(b[1]);
}

inline uint32_t readU32BE(const uint8_t* b) {
    return (uint32_t(b[0]) << 24) | (uint32_t(b[1]) << 16) |
           (uint32_t(b[2]) << 8)  |  uint32_t(b[3]);
}

// Set register pointer, then read `n` bytes into `buf`.
// Returns true on a clean transaction (no NACK, no short read).
bool readRegister(uint8_t addr, uint8_t reg, uint8_t* buf, size_t n) {
    Wire.beginTransmission(addr);
    Wire.write(reg);
    // endTransmission returns 0 on success.
    if (Wire.endTransmission() != 0) return false;

    size_t got = Wire.requestFrom((int)addr, (int)n);
    if (got != n) {
        // Drain whatever did arrive.
        while (Wire.available()) (void)Wire.read();
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        if (!Wire.available()) return false;
        buf[i] = (uint8_t)Wire.read();
    }
    return true;
}

bool writeCommand(uint8_t addr, uint8_t cmd) {
    Wire.beginTransmission(addr);
    Wire.write(REG_CTRL);
    Wire.write(cmd);
    return Wire.endTransmission() == 0;
}

// Probe whether a slave is on the bus by attempting a 0-byte write.
bool slavePresent(uint8_t addr) {
    Wire.beginTransmission(addr);
    return Wire.endTransmission() == 0;
}

// Read all interesting registers into `out`. Returns true if every read
// succeeded. Does NOT send LATCH — caller does that on success.
bool readAllRegisters(uint8_t addr, Snapshot& out) {
    uint8_t buf[24];

    if (!readRegister(addr, REG_PROTOCOL_VERSION, buf, 1)) return false;
    out.protocol_version = buf[0];

    if (!readRegister(addr, REG_STATUS, buf, 1)) return false;
    out.status_flags = buf[0];

    if (!readRegister(addr, REG_UPTIME_S, buf, 2)) return false;
    out.uptime_s = readU16BE(buf);

    if (!readRegister(addr, REG_NUM_GATES, buf, 1)) return false;
    out.num_gates = buf[0];

    if (!readRegister(addr, REG_GATES_HEALTHY, buf, 1)) return false;
    out.gates_healthy = buf[0];

    if (!readRegister(addr, REG_TOTAL_IN, buf, 4)) return false;
    out.total_in = readU32BE(buf);

    if (!readRegister(addr, REG_TOTAL_OUT, buf, 4)) return false;
    out.total_out = readU32BE(buf);

    if (!readRegister(addr, REG_INTERVAL_IN, buf, 4)) return false;
    out.interval_in = readU32BE(buf);

    if (!readRegister(addr, REG_INTERVAL_OUT, buf, 4)) return false;
    out.interval_out = readU32BE(buf);

    if (!readRegister(addr, REG_GLITCH_COUNT, buf, 2)) return false;
    out.glitch_count = readU16BE(buf);

    if (!readRegister(addr, REG_BUSY_RETRIES, buf, 2)) return false;
    out.busy_retries = readU16BE(buf);

    if (!readRegister(addr, REG_PER_GATE_IN, out.per_gate_in,
                      PER_GATE_ARRAY_LEN)) return false;
    if (!readRegister(addr, REG_PER_GATE_OUT, out.per_gate_out,
                      PER_GATE_ARRAY_LEN)) return false;

    return true;
}

}  // namespace

bool pollSlot(uint8_t address, Snapshot& out) {
    out = Snapshot{};

    if (!slavePresent(address)) {
        return false;
    }

    out.present = true;

    // First attempt.
    out.read_attempts = 1;
    bool ok = readAllRegisters(address, out);

    // The BeeCounter now has a dedicated slave bus (its GPIO2/GPIO3), so it
    // can answer at any instant and a master/slave-window collision can no
    // longer happen. We still retry once after a short delay as cheap
    // insurance against ordinary bus noise or a transient wakeup race.
    if (!ok) {
        delay(READ_RETRY_DELAY_MS);
        out.read_attempts = 2;
        ok = readAllRegisters(address, out);
    }

    if (!ok) {
        Serial.printf("[BEE] slot 0x%02X: read FAILED after %u attempts\n",
                      address, out.read_attempts);
        return false;
    }

    // Send LATCH only after a fully successful read; otherwise this
    // interval's counts would be silently discarded.
    if (writeCommand(address, CMD_LATCH)) {
        out.latch_succeeded = true;
    } else {
        Serial.printf("[BEE] slot 0x%02X: LATCH write failed\n", address);
    }

    Serial.printf("[BEE] slot 0x%02X: in=%lu out=%lu (total %lu/%lu) "
                  "status=0x%02X glitches=%u attempts=%u latch=%s\n",
                  address,
                  (unsigned long)out.interval_in,
                  (unsigned long)out.interval_out,
                  (unsigned long)out.total_in,
                  (unsigned long)out.total_out,
                  out.status_flags,
                  out.glitch_count,
                  out.read_attempts,
                  out.latch_succeeded ? "ok" : "FAIL");

    return true;
}

void writeSnapshotToJson(JsonDocument& doc, uint8_t slot, const Snapshot& snap) {
    String p = "bee_counter_" + String((int)slot) + "_";

    doc[p + "ok"] = snap.present;

    if (!snap.present) return;

    doc[p + "protocol_version"] = snap.protocol_version;
    doc[p + "status_flags"]     = snap.status_flags;
    doc[p + "uptime_s"]         = snap.uptime_s;
    doc[p + "num_gates"]        = snap.num_gates;
    doc[p + "gates_healthy"]    = snap.gates_healthy;
    doc[p + "total_in"]         = snap.total_in;
    doc[p + "total_out"]        = snap.total_out;
    doc[p + "glitch_count"]     = snap.glitch_count;
    doc[p + "read_attempts"]    = snap.read_attempts;

    // Totals-only sources (the HiveTraffic BLE path) report no per-interval or
    // per-gate detail. Omit those fields (leaving them NULL) so the backend
    // differences the lifetime totals into intervals instead of recording a
    // misleading zero. The wired I2C path fills them as before.
    if (snap.totals_only) return;

    doc[p + "interval_in"]      = snap.interval_in;
    doc[p + "interval_out"]     = snap.interval_out;
    doc[p + "busy_retries"]     = snap.busy_retries;
    doc[p + "latch_succeeded"]  = snap.latch_succeeded;

    // Per-gate arrays go in raw_json only — they don't deserve top-level
    // columns. Storing 24+24 bytes per hive as a JSON array keeps the
    // schema slim while preserving forensic data for the rare debug.
    JsonArray gin = doc[p + "per_gate_in"].to<JsonArray>();
    for (uint8_t i = 0; i < PER_GATE_ARRAY_LEN; i++) gin.add(snap.per_gate_in[i]);

    JsonArray gout = doc[p + "per_gate_out"].to<JsonArray>();
    for (uint8_t i = 0; i < PER_GATE_ARRAY_LEN; i++) gout.add(snap.per_gate_out[i]);
}

// ---------------------------------------------------------------------------
// OTA-over-I2C relay (master side)
// ---------------------------------------------------------------------------

uint32_t crc32_buf(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t b = 0; b < 8; b++)
            crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320u : (crc >> 1);
    }
    return crc ^ 0xFFFFFFFFu;
}

namespace {

bool otaReadStatus(uint8_t addr, uint8_t& state, uint32_t& received, uint8_t& err) {
    Wire.beginTransmission(addr);
    Wire.write(REG_OTA_STATUS);
    if (Wire.endTransmission() != 0) return false;
    size_t got = Wire.requestFrom((int)addr, 6);
    if (got != 6) { while (Wire.available()) (void)Wire.read(); return false; }
    uint8_t b[6];
    for (int i = 0; i < 6; i++) b[i] = (uint8_t)Wire.read();
    state    = b[0];
    received = (uint32_t(b[1]) << 24) | (uint32_t(b[2]) << 16) |
               (uint32_t(b[3]) << 8)  |  uint32_t(b[4]);
    err      = b[5];
    return true;
}

bool otaWriteBegin(uint8_t addr, uint32_t size, uint32_t crc) {
    Wire.beginTransmission(addr);
    Wire.write(REG_OTA_BEGIN);
    uint8_t p[8] = {
        (uint8_t)(size >> 24), (uint8_t)(size >> 16), (uint8_t)(size >> 8), (uint8_t)size,
        (uint8_t)(crc  >> 24), (uint8_t)(crc  >> 16), (uint8_t)(crc  >> 8), (uint8_t)crc,
    };
    Wire.write(p, 8);
    return Wire.endTransmission() == 0;
}

bool otaWriteData(uint8_t addr, uint32_t offset, const uint8_t* data, size_t n) {
    Wire.beginTransmission(addr);
    Wire.write(REG_OTA_DATA);
    uint8_t off[4] = {
        (uint8_t)(offset >> 24), (uint8_t)(offset >> 16),
        (uint8_t)(offset >> 8),  (uint8_t)offset,
    };
    Wire.write(off, 4);
    Wire.write(data, n);
    return Wire.endTransmission() == 0;
}

bool otaWriteBareReg(uint8_t addr, uint8_t reg) {
    Wire.beginTransmission(addr);
    Wire.write(reg);
    return Wire.endTransmission() == 0;
}

}  // namespace

bool pushFirmwareToBeeCounter(uint8_t address, const uint8_t* image, size_t len,
                              uint32_t imageCrc32) {
    Serial.printf("[BEE-OTA] Pushing %u bytes to 0x%02X (crc=0x%08X)\n",
                  (unsigned)len, address, (unsigned)imageCrc32);

    if (!slavePresent(address)) {
        Serial.println("[BEE-OTA] slave not present, aborting");
        return false;
    }

    Wire.setClock(400000);   // fast transfer; restored before every return path

    if (!otaWriteBegin(address, (uint32_t)len, imageCrc32)) {
        Serial.println("[BEE-OTA] BEGIN frame failed");
        Wire.setClock(100000);
        return false;
    }
    delay(50);   // allow Update.begin()/partition erase on the slave

    uint8_t  state; uint32_t recv; uint8_t err;
    if (!otaReadStatus(address, state, recv, err) || state != OTA_STATE_RECEIVING) {
        Serial.printf("[BEE-OTA] not RECEIVING after BEGIN (state=0x%02X err=%u)\n",
                      state, err);
        Wire.setClock(100000);
        return false;
    }

    const size_t CHUNK = OTA_CHUNK_MAX;
    size_t  offset  = 0;
    uint8_t retries = 0;

    while (offset < len) {
        size_t n = (len - offset < CHUNK) ? (len - offset) : CHUNK;

        if (!otaWriteData(address, (uint32_t)offset, image + offset, n)) {
            if (++retries > 5) { Wire.setClock(100000); return false; }
            delay(10);
            continue;   // re-send same offset; slave's seq check rejects dups
        }
        if (!otaReadStatus(address, state, recv, err)) {
            if (++retries > 5) { Wire.setClock(100000); return false; }
            delay(5);
            continue;
        }
        if (state >= OTA_STATE_ERR_BEGIN) {   // any error state is fatal
            Serial.printf("[BEE-OTA] slave error 0x%02X (err=%u) at %u\n",
                          state, err, (unsigned)offset);
            Wire.setClock(100000);
            return false;
        }
        if (recv != offset + n) {             // slave didn't advance; re-send
            if (++retries > 5) { Wire.setClock(100000); return false; }
            continue;
        }

        offset += n;
        retries = 0;
        if ((offset & 0x3FFF) == 0)
            Serial.printf("[BEE-OTA] %u / %u bytes\n", (unsigned)offset, (unsigned)len);
    }

    if (!otaWriteBareReg(address, REG_OTA_END)) {
        Serial.println("[BEE-OTA] END frame failed");
        Wire.setClock(100000);
        return false;
    }
    delay(100);   // slave verifies CRC + Update.end()

    bool gotStatus = otaReadStatus(address, state, recv, err);
    Wire.setClock(100000);   // restore regardless of outcome

    if (!gotStatus) {
        Serial.println("[BEE-OTA] status read after END failed");
        return false;
    }
    if (state == OTA_STATE_DONE) {
        Serial.println("[BEE-OTA] SUCCESS — BeeCounter rebooting into new firmware");
        return true;
    }
    Serial.printf("[BEE-OTA] FAILED final state=0x%02X err=%u recv=%u\n",
                  state, err, (unsigned)recv);
    return false;
}

// ---------------------------------------------------------------------------
// HiveTraffic (wireless bee counter) GATT-client read
// ---------------------------------------------------------------------------
#if ENABLE_WIRELESS_BEECOUNTER

namespace {

// Parse the HiveTraffic measurement JSON (see 2026-easy-bee-counter
// docs/ble-mode.md) into a totals-only Snapshot. Returns false on malformed
// JSON. Fields absent in the document keep their Snapshot defaults.
bool parseTrafficJson(const char* json, size_t len, Snapshot& out) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json, len);
    if (err) {
        Serial.printf("[TRAFFIC] JSON parse failed: %s\n", err.c_str());
        return false;
    }
    out.present          = true;
    out.totals_only      = true;
    out.protocol_version = doc["fw"]            | 0;
    out.status_flags     = doc["status"]        | 0;
    out.uptime_s         = doc["uptime_s"]      | 0;
    out.num_gates        = doc["num_gates"]     | 0;
    out.gates_healthy    = doc["gates_healthy"] | 0;
    out.total_in         = doc["total_in"]      | 0u;
    out.total_out        = doc["total_out"]     | 0u;
    out.glitch_count     = doc["glitches"]      | 0;
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
    // deinit() in bleRunCycle() to trip over.
    if (client->isConnected()) client->disconnect();
    uint32_t deadline = millis() + BEECOUNTER_GATT_DISCONNECT_TIMEOUT_MS;
    while (client->isConnected() && (int32_t)(deadline - millis()) > 0) delay(20);
    NimBLEDevice::deleteClient(client);
    return ok;
}

}  // namespace

void bleRunCycle(const String& mac0, const String& mac1,
                 Snapshot& slot1, Snapshot& slot2) {
    slot1 = Snapshot{};
    slot2 = Snapshot{};
    if (mac0.length() == 0 && mac1.length() == 0) return;

    NimBLEDevice::init("");
    if (mac0.length()) (void)readTrafficSlot(mac0, slot1);
    if (mac1.length()) (void)readTrafficSlot(mac1, slot2);
    NimBLEDevice::deinit(true);
}

#endif  // ENABLE_WIRELESS_BEECOUNTER

}  // namespace beecnt