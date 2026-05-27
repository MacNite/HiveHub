// bee_counter_client.h — polls BeeCounter slaves over I2C.
//
// Keep these constants in sync with
//   2026-easy-bee-counter/Firmware/include/i2c_slave_protocol.h
// Protocol version is reported back from the slave so any mismatch will
// surface in the upload payload.

#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <ArduinoJson.h>

namespace beecnt {

// Slave addresses we will probe each cycle. NUM_SLOTS sized to two
// physical hives in the dual setup; widen later if needed.
constexpr uint8_t SLAVE_ADDR_SLOT_1 = 0x30;
constexpr uint8_t SLAVE_ADDR_SLOT_2 = 0x31;
constexpr uint8_t NUM_SLOTS         = 2;

// Register addresses (subset we actually read).
constexpr uint8_t REG_PROTOCOL_VERSION = 0x00;
constexpr uint8_t REG_STATUS           = 0x01;
constexpr uint8_t REG_UPTIME_S         = 0x02;  // 2 bytes BE
constexpr uint8_t REG_NUM_GATES        = 0x04;
constexpr uint8_t REG_GATES_HEALTHY    = 0x05;
constexpr uint8_t REG_TOTAL_IN         = 0x10;  // 4 bytes BE
constexpr uint8_t REG_TOTAL_OUT        = 0x14;  // 4 bytes BE
constexpr uint8_t REG_INTERVAL_IN      = 0x18;  // 4 bytes BE
constexpr uint8_t REG_INTERVAL_OUT     = 0x1C;  // 4 bytes BE
constexpr uint8_t REG_GLITCH_COUNT     = 0x20;  // 2 bytes BE
constexpr uint8_t REG_BUSY_RETRIES     = 0x22;  // 2 bytes BE
constexpr uint8_t REG_PER_GATE_IN      = 0x30;  // 24 bytes
constexpr uint8_t REG_PER_GATE_OUT     = 0x48;  // 24 bytes
constexpr uint8_t REG_CTRL             = 0x80;  // write-only

constexpr uint8_t CMD_LATCH         = 0x01;
constexpr uint8_t CMD_CLEAR_FAULTS  = 0x04;

constexpr uint8_t PER_GATE_ARRAY_LEN = 24;

// Status bits — we forward as-is to the backend but it's convenient to
// have the names locally for debug prints.
constexpr uint8_t STATUS_READY              = 0x01;
constexpr uint8_t STATUS_MCP_U2_OK          = 0x02;
constexpr uint8_t STATUS_MCP_U3_OK          = 0x04;
constexpr uint8_t STATUS_MCP_U4_OK          = 0x08;
constexpr uint8_t STATUS_IR_LEDS_ON         = 0x10;
constexpr uint8_t STATUS_SENSOR_FAULT_FLAG  = 0x20;
constexpr uint8_t STATUS_OVERFLOW_FLAG      = 0x40;

// One snapshot we read each upload cycle.
struct Snapshot {
    bool     present          = false;  // slave acked on this cycle
    bool     latch_succeeded  = false;  // CMD_LATCH was written cleanly
    uint8_t  protocol_version = 0;
    uint8_t  status_flags     = 0;
    uint16_t uptime_s         = 0;
    uint8_t  num_gates        = 0;
    uint8_t  gates_healthy    = 0;
    uint32_t total_in         = 0;
    uint32_t total_out        = 0;
    uint32_t interval_in      = 0;
    uint32_t interval_out     = 0;
    uint16_t glitch_count     = 0;
    uint16_t busy_retries     = 0;
    uint8_t  per_gate_in[PER_GATE_ARRAY_LEN]  = {0};
    uint8_t  per_gate_out[PER_GATE_ARRAY_LEN] = {0};
    uint8_t  read_attempts    = 0;       // 1 = first try succeeded, 2 = retried
};

// Read every register listed above from one slave, then LATCH on
// success. Returns true if the slave was present and was fully read.
// On any I2C failure during the read sequence, retries the whole
// sequence ONCE after a 50ms delay — per the BeeCounter README. Does
// not send CMD_LATCH unless every read succeeded.
bool pollSlot(uint8_t address, Snapshot& out);

// Serialize a snapshot into the parent measurement JSON document
// under a per-slot key prefix (e.g. "bee_counter_1_").
void writeSnapshotToJson(JsonDocument& doc, uint8_t slot, const Snapshot& snap);

}  // namespace beecnt