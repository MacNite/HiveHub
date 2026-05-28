// ============================================================================
// pins.h — Easy Bee Counter 2026 hardware map
// ============================================================================
// All physical pin numbers and I2C addresses were derived from the KiCad
// schematic (easy-bee-counter-2026.kicad_sch) and the netlist export
// (easy-bee-counter-2026.net). Update this file if the PCB is revised.
// ============================================================================
//
// ESP32-C6 mini silk-label -> GPIO mapping
// ----------------------------------------
// The board exposes silk labels D0..D10 + TX_D6/RX_D7 that correspond to:
//   D0  -> GPIO0
//   D1  -> GPIO1
//   D2  -> GPIO2     <-- SDA_HiveScale (2nd I2C, slave to HiveScale)   [NEW]
//   D3  -> GPIO3     <-- SCL_HiveScale (2nd I2C, slave to HiveScale)   [NEW]
//   D4  -> GPIO4     <-- /SDA   (I2C data, MCP23017 master bus)
//   D5  -> GPIO5     <-- /SDC   (I2C clock, schematic spells it "SDC")
//   D6  -> GPIO6     <-- TX (UART0)            [unused on this board]
//   D7  -> GPIO7     <-- RX (UART0)            [unused on this board]
//   D8  -> GPIO8     <-- /GPIO4 net -> Q1 gate (LED_BANK_1 enable, gates 00..13)
//   D9  -> GPIO9     <-- /GPIO5 net -> Q2 gate (LED_BANK_2 enable, gates 14..27)
//   D10 -> GPIO10                                [unused on this board]
//
// The schematic net labels "/GPIO4" and "/GPIO5" do NOT mean physical GPIO4/5 —
// they were just net names. Physically they connect to U5 pins 9 and 10 which
// are silk-labelled D8 and D9, i.e. GPIO8 and GPIO9. Confusing but real.
//
// I2C bus layout (DUAL-BUS, 2026-revision)
// ----------------------------------------
// The ESP32-C6 has TWO independent I2C controllers. We now use both, which
// removes the old master/slave time-multiplexing entirely:
//
//   Bus 0 (Wire)  — MASTER ONLY, GPIO4 (SDA) / GPIO5 (SCL)
//                   Talks to the 3x MCP23017 port expanders. On-board
//                   pull-ups R4/R5 (4.7k each). The external J1 SDA/SCL
//                   traces to the HiveScale have been cut off this net.
//
//   Bus 1 (Wire1) — SLAVE ONLY, GPIO2 (SDA_HiveScale) / GPIO3 (SCL_HiveScale)
//                   Permanently listens at i2c_addr::BEECOUNTER_SLAVE for the
//                   HiveScale. Pull-ups for this bus come from the HiveScale
//                   side I2C network (no extra on-board pull-ups required).
//
// Because each role now owns its own controller, the HiveScale can poll us
// at any instant with no risk of a NACK/stretch collision. There is no
// "slave window", no retry requirement, and REG_BUSY_RETRIES is always 0.
//
// MCP23017 addresses (set by A0/A1/A2 strap pins, base 0x20):
//   U2 -> A0=0 A1=0 A2=0 -> 0x20  (gates 00..07)
//   U3 -> A0=1 A1=0 A2=0 -> 0x21  (gates 10..17)
//   U4 -> A0=0 A1=1 A2=0 -> 0x22  (gates 20..27)
//
// MCP23017 GPIO assignment per chip:
//   GPA0..GPA7 -> Inner sensor of gate N0..N7 (data line of QRE1113 pin 3)
//   GPB0..GPB7 -> Outer sensor of gate N0..N7 (data line of QRE1113 pin 3)
//
// In the Adafruit_MCP23X17 library, digitalRead(0..7) -> port A,
// digitalRead(8..15) -> port B, so:
//   inner sensor of gate K -> mcp.digitalRead(K)        // 0..7
//   outer sensor of gate K -> mcp.digitalRead(K + 8)    // 8..15
//
// QRE1113 wiring on this board:
//   - Each phototransistor collector goes to its MCP pin, with a 100k
//     pull-up to +3.3V (RN1/RN3/RN5/RN6/RN8/RN9).
//   - IR LEDs on a gate are tied to LED_BANK_1 or LED_BANK_2 anodes, with
//     PD1/PD2 ballast resistors in series and Q1/Q2 (IRLB8721 N-FETs) on the
//     low side. Driving the FET gate HIGH turns the IR LEDs ON, which causes
//     reflected light to drop the phototransistor collector voltage.
//   - Therefore the *logical* convention used in this firmware is:
//        sensor reads LOW  -> bee body in the beam (beam reflected/blocked)
//        sensor reads HIGH -> beam clear OR IR LEDs off
//
// Gate numbering
// --------------
// The PCB has 24 active gates physically. The naming has gaps (08, 09, 18, 19,
// 28, 29 are skipped) to keep the per-chip pin -> gate map clean. We expose
// them as a logical 0..23 dense index internally but keep the original
// "GATE_NM" tags in debug logs and protocol responses for traceability.
//
// ============================================================================

#pragma once

#include <Arduino.h>
#include <stdint.h>

// ---------------------------------------------------------------------------
// ESP32-C6 GPIO pin assignments (physical GPIO numbers)
// ---------------------------------------------------------------------------
namespace pins {

// Bus 0 (Wire) — master to the MCP23017s.
constexpr int I2C_SDA           = 4;   // U5 D4 / silk "D4" -> /SDA net
constexpr int I2C_SCL           = 5;   // U5 D5 / silk "D5" -> /SDC net

// Bus 1 (Wire1) — slave to the HiveScale.                        [NEW]
constexpr int I2C_HIVE_SDA      = 2;   // U5 D2 / silk "D2" -> SDA_HiveScale
constexpr int I2C_HIVE_SCL      = 3;   // U5 D3 / silk "D3" -> SCL_HiveScale

constexpr int IR_LED_BANK_1_EN  = 8;   // U5 D8 / silk "D8" -> Q1 gate -> gates 00..13
constexpr int IR_LED_BANK_2_EN  = 9;   // U5 D9 / silk "D9" -> Q2 gate -> gates 14..27

}  // namespace pins

// ---------------------------------------------------------------------------
// I2C device addresses
// ---------------------------------------------------------------------------
#ifndef BEECOUNTER_I2C_ADDRESS
#define BEECOUNTER_I2C_ADDRESS 0x30   // default; override with -DBEECOUNTER_I2C_ADDRESS=0x31 for hive 2
#endif

namespace i2c_addr {
// MCP23017 expanders (we are MASTER when talking to these, on Wire / bus 0)
constexpr uint8_t MCP_GATES_00_07 = 0x20;   // U2
constexpr uint8_t MCP_GATES_10_17 = 0x21;   // U3
constexpr uint8_t MCP_GATES_20_27 = 0x22;   // U4

// Our own slave address (we are SLAVE to the HiveScale on this address, on
// Wire1 / bus 1). 0x30 is unused by any device on this board and not in the
// reserved range.
// For dual-hive setups, flash the hive-2 unit with -DBEECOUNTER_I2C_ADDRESS=0x31.
constexpr uint8_t BEECOUNTER_SLAVE = BEECOUNTER_I2C_ADDRESS;
}  // namespace i2c_addr

// ---------------------------------------------------------------------------
// Gate topology
// ---------------------------------------------------------------------------
namespace gates {

// Number of physical gates that are wired up on the PCB.
constexpr uint8_t NUM_GATES = 24;

// Each gate has an Inner sensor (toward the hive interior) and an
// Outer sensor (toward the outside world). Both are read from the same
// MCP23017 chip; the inner is on port A, the outer is on port B.
struct GateLocation {
    uint8_t mcp_address;   // I2C address of the MCP23017
    uint8_t inner_pin;     // 0..7  (GPA0..GPA7)
    uint8_t outer_pin;     // 8..15 (GPB0..GPB7)
    uint8_t led_bank;      // 1 or 2 (which MOSFET-controlled LED rail)
    const char* tag;       // original schematic name, e.g. "GATE_03"
};

// The 24 physical gates, indexed 0..23. The "tag" field carries the
// original schematic name so debug logs match the PCB silk and the netlist.
constexpr GateLocation TABLE[NUM_GATES] = {
    // U2 (0x20): gates 00..07, all on LED_BANK_1
    { i2c_addr::MCP_GATES_00_07, 0,  8, 1, "GATE_00" },
    { i2c_addr::MCP_GATES_00_07, 1,  9, 1, "GATE_01" },
    { i2c_addr::MCP_GATES_00_07, 2, 10, 1, "GATE_02" },
    { i2c_addr::MCP_GATES_00_07, 3, 11, 1, "GATE_03" },
    { i2c_addr::MCP_GATES_00_07, 4, 12, 1, "GATE_04" },
    { i2c_addr::MCP_GATES_00_07, 5, 13, 1, "GATE_05" },
    { i2c_addr::MCP_GATES_00_07, 6, 14, 1, "GATE_06" },
    { i2c_addr::MCP_GATES_00_07, 7, 15, 1, "GATE_07" },
    // U3 (0x21): gates 10..17.
    //   gates 10..13 are on LED_BANK_1, gates 14..17 are on LED_BANK_2
    { i2c_addr::MCP_GATES_10_17, 0,  8, 1, "GATE_10" },
    { i2c_addr::MCP_GATES_10_17, 1,  9, 1, "GATE_11" },
    { i2c_addr::MCP_GATES_10_17, 2, 10, 1, "GATE_12" },
    { i2c_addr::MCP_GATES_10_17, 3, 11, 1, "GATE_13" },
    { i2c_addr::MCP_GATES_10_17, 4, 12, 2, "GATE_14" },
    { i2c_addr::MCP_GATES_10_17, 5, 13, 2, "GATE_15" },
    { i2c_addr::MCP_GATES_10_17, 6, 14, 2, "GATE_16" },
    { i2c_addr::MCP_GATES_10_17, 7, 15, 2, "GATE_17" },
    // U4 (0x22): gates 20..27, all on LED_BANK_2
    { i2c_addr::MCP_GATES_20_27, 0,  8, 2, "GATE_20" },
    { i2c_addr::MCP_GATES_20_27, 1,  9, 2, "GATE_21" },
    { i2c_addr::MCP_GATES_20_27, 2, 10, 2, "GATE_22" },
    { i2c_addr::MCP_GATES_20_27, 3, 11, 2, "GATE_23" },
    { i2c_addr::MCP_GATES_20_27, 4, 12, 2, "GATE_24" },
    { i2c_addr::MCP_GATES_20_27, 5, 13, 2, "GATE_25" },
    { i2c_addr::MCP_GATES_20_27, 6, 14, 2, "GATE_26" },
    { i2c_addr::MCP_GATES_20_27, 7, 15, 2, "GATE_27" },
};

}  // namespace gates