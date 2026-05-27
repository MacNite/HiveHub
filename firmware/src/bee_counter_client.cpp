// bee_counter_client.cpp — see bee_counter_client.h for protocol notes.

#include "bee_counter_client.h"

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

    // The C6 BeeCounter switches between master/slave modes; it can NACK
    // us if we hit its master window. README recommends one retry after
    // 50ms — that's enough to land in the next slave window.
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
    doc[p + "interval_in"]      = snap.interval_in;
    doc[p + "interval_out"]     = snap.interval_out;
    doc[p + "glitch_count"]     = snap.glitch_count;
    doc[p + "busy_retries"]     = snap.busy_retries;
    doc[p + "read_attempts"]    = snap.read_attempts;
    doc[p + "latch_succeeded"]  = snap.latch_succeeded;

    // Per-gate arrays go in raw_json only — they don't deserve top-level
    // columns. Storing 24+24 bytes per hive as a JSON array keeps the
    // schema slim while preserving forensic data for the rare debug.
    JsonArray gin = doc[p + "per_gate_in"].to<JsonArray>();
    for (uint8_t i = 0; i < PER_GATE_ARRAY_LEN; i++) gin.add(snap.per_gate_in[i]);

    JsonArray gout = doc[p + "per_gate_out"].to<JsonArray>();
    for (uint8_t i = 0; i < PER_GATE_ARRAY_LEN; i++) gout.add(snap.per_gate_out[i]);
}

}  // namespace beecnt