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
#include "config.h"   // ENABLE_WIRELESS_BEECOUNTER + BEECOUNTER_GATT_* UUIDs

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

// ---- OTA-over-I2C (BeeCounter PROTOCOL_VERSION >= 2) ----------------------
// Keep in sync with i2c_slave_protocol.h on the BeeCounter side.
constexpr uint8_t REG_OTA_BEGIN  = 0x90;   // write 8 bytes: size(4 BE)+crc32(4 BE)
constexpr uint8_t REG_OTA_DATA   = 0x91;   // write offset(4 BE)+data
constexpr uint8_t REG_OTA_END    = 0x92;   // write 0 bytes -> finalize
constexpr uint8_t REG_OTA_ABORT  = 0x93;   // write 0 bytes -> cancel
constexpr uint8_t REG_OTA_STATUS = 0x94;   // read 6 bytes: state(1)+recv(4)+err(1)

constexpr uint8_t OTA_CHUNK_MAX  = 64;     // data bytes per REG_OTA_DATA frame

constexpr uint8_t OTA_STATE_IDLE      = 0x00;
constexpr uint8_t OTA_STATE_RECEIVING = 0x01;
constexpr uint8_t OTA_STATE_DONE      = 0x02;
constexpr uint8_t OTA_STATE_ERR_BEGIN = 0x10;   // first error code; >= is fatal
constexpr uint8_t OTA_STATE_ERR_SEQ   = 0x11;
constexpr uint8_t OTA_STATE_ERR_WRITE = 0x12;
constexpr uint8_t OTA_STATE_ERR_CRC   = 0x13;
constexpr uint8_t OTA_STATE_ERR_SIZE  = 0x14;
constexpr uint8_t OTA_STATE_ERR_END   = 0x15;

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

    // True when this snapshot came from a totals-only source (the HiveTraffic
    // BLE path), which reports lifetime totals but no per-interval or per-gate
    // detail. writeSnapshotToJson then omits the interval_* and per_gate_*
    // fields so the backend differences the totals into intervals instead of
    // storing a misleading zero.
    bool     totals_only      = false;
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

// Per-hive form for the hives[] array: writes a nested "bee_counter" object.
void writeSnapshotToHive(JsonObject hive, const Snapshot& snap);

#if ENABLE_WIRELESS_BEECOUNTER
// HiveTraffic (wireless bee counter) GATT-client read, registry-driven. Brings
// the BLE stack up once, then for every hive in hivecfg::gHives[] that carries a
// "beecounter" pairing connects to its MAC, reads the JSON measurement
// characteristic (BEECOUNTER_GATT_*), parses the lifetime totals into a
// totals-only Snapshot stored at out[h] (the same array position as gHives[h]),
// then tears the stack down — the same lifecycle and any-hive model as
// bhgatt::runCycle. A hive without a pairing (or with an empty MAC) leaves
// out[h] !present. At most MAX_GATT_READS_PER_CYCLE devices are read per cycle;
// `cap` is the length of `out` (pass MAX_HIVES). No CMD_LATCH is written: the
// wire format is totals-only and the backend differences the totals.
void bleRunCycleRegistry(Snapshot* out, uint8_t cap);
#endif

// CRC-32 (IEEE 802.3, poly 0xEDB88320) over a buffer, finalized. Matches the
// BeeCounter and the backend (zlib.crc32). Exposed so the relay/download
// layer can compute or verify an image checksum.
uint32_t crc32_buf(const uint8_t* data, size_t len);

// Push a firmware image (already in RAM, `len` bytes, with precomputed
// `imageCrc32`) to the BeeCounter at `address` over I2C. Temporarily raises
// the bus to 400 kHz, restores 100 kHz afterward. Returns true only if the
// BeeCounter reports OTA_STATE_DONE. The BeeCounter pauses bee counting and
// reboots into the new image on success.
bool pushFirmwareToBeeCounter(uint8_t address, const uint8_t* image, size_t len,
                              uint32_t imageCrc32);

}  // namespace beecnt