// config.h — compile-time configuration: pins, timeouts, file paths and
// optional-feature defaults. Pure preprocessor + constants, no globals.
//
// secrets.h is included first so that per-device overrides (feature flags,
// pin choices, sample rates) take effect before the defaults below.
#pragma once

// This header is included before <Arduino.h> (globals.h pulls config.h in first
// so feature flags resolve before the conditional driver includes). It therefore
// cannot rely on Arduino transitively providing the fixed-width / size types it
// uses for the constants at the bottom of this file — include them explicitly.
#include <stdint.h>
#include <stddef.h>

// Per-device secrets live in secrets.h (gitignored). Fall back to the tracked
// example so a fresh clone / CI still compiles with placeholder values — real
// builds must provide their own secrets.h. (__has_include is C++17, which this
// project already targets; the defined() guard keeps older toolchains safe.)
#if defined(__has_include)
#  if __has_include("secrets.h")
#    include "secrets.h"
#  else
#    include "secrets.example.h"
#  endif
#else
#  include "secrets.h"
#endif

// ==============================
// XIAO ESP32-C6 — BOARD-SPECIFIC SENSOR SET
// ==============================
// The C6 variant breaks out only the 11 front-header pins (D0–D10). Two wired
// peripherals from the classic board do not fit and are force-compiled out for
// this target — overriding whatever a secrets.h carried over from an ESP32 build
// (or the default of 1) may set:
//   - HX711: each amp needs a dedicated DOUT/SCK pin pair (two pairs = 4 pins).
//     The C6 reads load cells through I2C NAU7802 channels (D4/D5) instead, so
//     no HX711 driver is compiled in and no scale1/scale2 objects exist.
//   - INMP441 I2S microphone: no spare I2S-capable pins.
//
// A single DS18B20 1-Wire bus IS supported on the C6. With HX711 removed, D1 is
// free, so the V0.4 breakout wires the DS18B20 there (ONE_WIRE_PIN in the C6 pin
// map below) and ENABLE_DS18B20_HIVE_TEMP defaults ON for this board. It stays
// optional: in-hive temperature can still come from a paired BLE sensor instead.
//
// Two guards for the same condition:
//   HIVEHUB_BOARD_XIAO_C6  — set by [env:xiao_esp32c6] build_flags; available
//                               before any header is included (plain -D flag).
//   CONFIG_IDF_TARGET_ESP32C6 — from sdkconfig.h via Arduino.h; only valid in
//                               files that include Arduino.h first.
// Using both ensures this block fires even though config.h is included before
// Arduino.h (e.g. as the first thing in globals.h).
#if defined(HIVEHUB_BOARD_XIAO_C6) || defined(CONFIG_IDF_TARGET_ESP32C6)
// No HX711 on the C6 — force the driver out regardless of any secrets.h value.
#undef ENABLE_HX711
#define ENABLE_HX711 0
#undef ENABLE_INMP441_MICS
#define ENABLE_INMP441_MICS 0
#endif

// ==============================
// BOARD / ARCHITECTURE LABEL (OTA cross-flash guard)
// ==============================
// Reported to the backend during the OTA check (?board=...). The 30-pin ESP32 is
// an Xtensa LX6 and the XIAO ESP32-C6 is RISC-V — their firmware images are NOT
// interchangeable, and flashing the wrong one will not boot. The server only
// serves a hivescale release whose `board` matches this label, so a device is
// never offered an image built for the other architecture. Keep these strings in
// sync with firmware/rename_firmware.py (BOARD_LABELS) and the server's
// firmware_releases.board values ("esp32" / "esp32-c6").
#if defined(HIVEHUB_BOARD_XIAO_C6) || defined(CONFIG_IDF_TARGET_ESP32C6)
#define HIVEHUB_BOARD_LABEL "esp32-c6"
#else
#define HIVEHUB_BOARD_LABEL "esp32"
#endif

#ifndef CLAIM_CODE
#define CLAIM_CODE ""
#endif

#ifndef CLAIM_CODE_REVISION
#define CLAIM_CODE_REVISION 1
#endif

#ifndef FORCE_RESEED
#define FORCE_RESEED false
#endif

// ==============================
// OPTIONAL OFF-GRID FEATURES
// ==============================
// Keep all optional hardware compiled out by default. Enable per device in
// secrets.h with 0/1 values.
#ifndef ENABLE_INA219_SOLAR
#define ENABLE_INA219_SOLAR 0
#endif

#ifndef ENABLE_MAX17048_BATTERY
#define ENABLE_MAX17048_BATTERY 0
#endif

#ifndef INA219_I2C_ADDRESS
#define INA219_I2C_ADDRESS 0x40
#endif

#ifndef MAX17048_ALERT_PERCENT
#define MAX17048_ALERT_PERCENT 20
#endif

// ==============================
// DS18B20 WIRED IN-HIVE TEMPERATURE (optional)
// ==============================
// The 1-Wire DS18B20 probes are an OPTIONAL sensor: in-hive temperature can
// instead come from a paired in-hive BLE sensor (see below). Default OFF on the
// classic board; default ON for the XIAO C6, whose V0.4 breakout always wires a
// DS18B20 to D1. Override either way in secrets.h.
#ifndef ENABLE_DS18B20_HIVE_TEMP
#  if defined(HIVEHUB_BOARD_XIAO_C6) || defined(CONFIG_IDF_TARGET_ESP32C6)
#    define ENABLE_DS18B20_HIVE_TEMP 1
#  else
#    define ENABLE_DS18B20_HIVE_TEMP 0
#  endif
#endif

// ==============================
// MULTI-HIVE CAPACITY (up to 18 hives per ESP32)
// ==============================
// HiveHub historically served exactly two hives (two HX711 load cells, two
// DS18B20 probes, two BLE slots). v0.20.0 generalises this to a dynamic registry
// of up to MAX_HIVES hives, each carrying one scale source and at most one
// non-scale in-hive sensor, configured from the provisioning portal and stored as a per-hive JSON blob in
// NVS (see firmware/src/hive_config.cpp).
//
//   - Scales: HX711 (legacy pins) and/or NAU7802 I2C channels via the
//     NAU7802 + TCA9548A path below. See the wired-channel topology note on
//     MAX_SCALES — all-NAU7802 tops out at 16; 18 needs the 2 HX711 channels.
//   - Wired temperature: up to MAX_HIVES DS18B20 on the single ONE_WIRE_PIN bus,
//     addressed by ROM (not by index) so each probe maps to a specific hive.
//   - In-hive sensors: one non-scale BLE/GATT sensor OR one DS18B20 per hive.
//     BLE HiveScale selected as a scale source is stored separately from that
//     in-hive sensor; serial GATT reads are still capped per cycle (see below).
#ifndef MAX_HIVES
#define MAX_HIVES 18
#endif
// Upper bound on physically attached WIRED load-cell channels.
//
// IMPORTANT — how to actually reach 18 (the NAU7802 has NO address-select pin;
// it is hardwired to 0x2A, so two NAU7802s cannot share one bus segment):
//   - All-NAU7802 via ONE TCA9548A: max 16 (8 chips × 2 channels, mux-only). A
//     NAU7802 on the main bus shares 0x2A with every muxed chip and stays on the
//     bus while a mux channel is enabled, so a direct chip and the mux CANNOT be
//     mixed. Use EITHER one main-bus chip (2 scales, no mux) OR the mux (≤16).
//   - 18 wired channels: classic ESP32 board only — 2 HX711 (dedicated pins, no
//     I2C address, so no 0x2A collision) + 16 muxed NAU7802 = 18.
//   - 18 all-NAU7802 would need a SECOND TCA9548A at a different address; the
//     firmware models a single mux address today, so that is not yet supported.
#ifndef MAX_SCALES
#define MAX_SCALES 18
#endif

// ==============================
// HX711 LOAD-CELL AMPLIFIER (legacy, 2× dedicated pin pairs)
// ==============================
// The classic board reads two load cells through two HX711 amps on dedicated
// pins (HX1_*/HX2_* in the pin map). Set to 0 to compile the HX711 driver out
// entirely — no <HX711.h> include, no scale1/scale2 objects, no HX711 option in
// the provisioning portal. The XIAO ESP32-C6 has no room for the amps and reads
// load cells via I2C NAU7802 instead, so ENABLE_HX711 is force-disabled for that
// target up in the board-specific block near the top of this file.
#ifndef ENABLE_HX711
#define ENABLE_HX711 1
#endif

// ==============================
// NAU7802 24-bit I2C LOAD-CELL ADC (alternative to HX711)
// ==============================
// The NAU7802 (Nuvoton) is a 24-bit bridge ADC on I2C at a FIXED address 0x2A. It
// has TWO differential input channels (CH1/CH2) multiplexed onto one ADC, so a
// single NAU7802 reads two load cells. We read it RAW (getReading()) and apply our
// own offset/factor, mirroring the HX711 path (weightFromRaw), so no per-channel
// calibration state has to survive a mux switch. Before deep sleep every NAU7802
// is put into power-down (PU_CTRL PUD/PUA cleared) — see powerDownScalesForSleep().
#ifndef ENABLE_NAU7802
#define ENABLE_NAU7802 1
#endif
#ifndef NAU7802_I2C_ADDRESS
#define NAU7802_I2C_ADDRESS 0x2A
#endif
// Samples averaged per NAU7802 channel read (matches the HX711 default of 15).
#ifndef NAU7802_SAMPLES
#define NAU7802_SAMPLES 15
#endif

// ==============================
// TCA9548A 1-to-8 I2C MULTIPLEXER (fan out 8 more NAU7802s)
// ==============================
// Because every NAU7802 lives at 0x2A (no address-select pin), more than one can
// only share the bus behind a TCA9548A mux. The mux exposes 8 downstream channels
// (0–7); writing (1<<channel) to its control register connects exactly one. With
// one NAU7802 per mux channel that is 8×2 = 16 scales — the maximum for an
// all-NAU7802 setup. A NAU7802 directly on the main bus CANNOT be added on top:
// it shares 0x2A and stays on the bus while a mux channel is enabled, so its
// reads would collide with the muxed chip's. To reach 18 wired channels, use the
// 2 HX711 pin channels (no I2C address) alongside the 16 muxed NAU7802s. Only ONE
// mux channel may be enabled at a time; the driver disables all channels (write
// 0x00) between hives, so a main-bus chip is only ever read with the mux closed.
#ifndef ENABLE_I2C_MUX
#define ENABLE_I2C_MUX 1
#endif
#ifndef TCA9548A_I2C_ADDRESS
#define TCA9548A_I2C_ADDRESS 0x70
#endif

// ==============================
// BLE READ BUDGET (protect deep sleep — see also the BLE sections below)
// ==============================
// A passive scan catches every nearby BEACON (HolyIot 25015 / RuuviTag /
// advertising HiveInside) in a single window, so any number of beacon in-hive
// sensors costs the same one scan and deep sleep stays effective. GATT sensors
// (HiveHeart, GATT-mode HiveInside, wireless HiveScale, HiveTraffic) instead need
// a SERIAL connect→read→disconnect of seconds each, so reading many of them would
// keep the radio awake for minutes and defeat deep sleep. Cap the number of GATT
// reads attempted per wake cycle; remaining paired GATT sensors are skipped this
// cycle (and logged). Beacons are never capped.
#ifndef MAX_GATT_READS_PER_CYCLE
#define MAX_GATT_READS_PER_CYCLE 4
#endif

// ==============================
// INMP441 STEREO MICS (defaults)
// ==============================
// The wired in-hive microphone is optional and compiled out by default
// (ENABLE_INMP441_MICS 0). Enable per device in secrets.h.
#ifndef ENABLE_INMP441_MICS
#define ENABLE_INMP441_MICS 0
#endif

#ifndef INMP441_BCLK_PIN
#define INMP441_BCLK_PIN 14
#endif

#ifndef INMP441_WS_PIN
#define INMP441_WS_PIN 13
#endif

#ifndef INMP441_SD_PIN
#define INMP441_SD_PIN 34
#endif

#ifndef INMP441_SAMPLE_RATE
#define INMP441_SAMPLE_RATE 16000
#endif

#ifndef INMP441_SAMPLE_FRAMES
#define INMP441_SAMPLE_FRAMES 8000
#endif

// Use I2S port 0. Port 0 has access to the most peripherals on the ESP32.
#ifndef INMP441_I2S_PORT
#define INMP441_I2S_PORT I2S_NUM_0
#endif

// ==============================
// HOLYIOT 25015 IN-HIVE BLE SENSOR (optional)
// ==============================
// Replaces the previous wired LIS3DH/LIS2DH12 accelerometer. The HolyIot 25015
// is an nRF54L15 BLE beacon carrying an SHT40 (temp/humidity), an LPS22HB
// (barometric pressure) and a LIS2DH12 (3-axis acceleration). The ESP32 acts as
// a passive BLE bridge: during each wake cycle it runs a short scan, parses the
// beacon's advertisement and folds the readings into the normal measurement
// upload. Up to two sensors can be paired (slot 1 -> hive 1, slot 2 -> hive 2)
// from the provisioning portal; their MAC addresses live in Preferences.
//
// IMPORTANT — advertisement byte layout is a documented BEST GUESS.
// HolyIot do not publish the 25015 advertisement format. The offsets in
// firmware/src/ble_sensor.cpp (HOLYIOT_OFF_* constants) are an editable
// best-effort layout; after sniffing one real packet (nRF Connect etc.) adjust
// those constants — no other code needs to change.
#ifndef ENABLE_BLE_SCAN
#define ENABLE_BLE_SCAN 1
#endif

// How many seconds to scan for the paired beacons each cycle. The 25015
// typically advertises every 0.5–2 s, so a few seconds reliably catches it
// while keeping the extra awake time (and battery cost) small.
#ifndef HOLYIOT_BLE_SCAN_SECONDS
#define HOLYIOT_BLE_SCAN_SECONDS 6
#endif

// Active scan also pulls the scan-response payload (device name). Costs a little
// more power but improves identification during portal pairing.
#ifndef HOLYIOT_BLE_ACTIVE_SCAN
#define HOLYIOT_BLE_ACTIVE_SCAN 1
#endif

// 16-bit BLE company identifier in the manufacturer-specific AD structure.
// 0xFFFF is the "no registered company" value many generic beacons ship with;
// override in secrets.h once the real ID is known from a packet capture.
#ifndef HOLYIOT_COMPANY_ID
#define HOLYIOT_COMPANY_ID 0xFFFF
#endif

// ==============================
// RUUVITAG IN-HIVE BLE SENSOR (optional, shares the same bridge)
// ==============================
// The RuuviTag (Ruuvi Innovations) is a four-in-one BLE beacon — temperature,
// humidity, pressure and 3-axis acceleration — auto-detected on the SAME passive
// scan bridge as the HolyIot 25015. It is told apart by its registered Ruuvi
// company id (0x0499) and decoded by firmware/include/ruuvi_decode.h (Data
// Format 5 / RAWv2, with legacy Format 3 support). Its readings fold into the
// existing ble_{slot}_* and accel_{slot}_* fields, so no extra enable flag or
// server column is required. Override only if Ruuvi ever changes the id.
#ifndef RUUVI_COMPANY_ID
#define RUUVI_COMPANY_ID 0x0499
#endif

// ==============================
// HIVEINSIDE ESP32-C6 IN-HIVE BLE SENSOR (optional, shares the same bridge)
// ==============================
// The HiveInside ESP32-C6 prototype advertises through the SAME passive scan
// bridge as the HolyIot 25015 (ENABLE_BLE_SCAN turns the bridge on). Its
// manufacturer-specific payload is auto-detected by a distinct company id plus a
// magic byte, so the two formats coexist with no extra enable flag. Unlike the
// HolyIot beacon it also carries vibration AND acoustic FFT bands, which the
// bridge folds into the existing accel_{slot}_band_* and mic_{left,right}_band_*
// measurement fields (slot 1 -> mic_left, slot 2 -> mic_right).
#ifndef HIVEINSIDE_COMPANY_ID
#define HIVEINSIDE_COMPANY_ID 0x02E5   // Espressif's Bluetooth SIG company id
#endif

// ==============================
// HIVEINSIDE GATT CLIENT (optional, requires HiveInside BLE_MODE=BLE_MODE_GATT)
// ==============================
// When enabled, HiveHub connects to a paired HiveInside sensor as a GATT
// central *after* the passive scan locates it by MAC address. It reads the full
// JSON measurement characteristic (every FFT band, RMS, peak, mic bands) and
// then disconnects. Only devices found by MAC without recognisable advertising
// data trigger a connection attempt, so HolyIot and advertising-mode HiveInside
// sensors are unaffected. Set to 0 to use passive advertising scan exclusively.
//
// Both HiveHub (this flag) and HiveInside (BLE_MODE_GATT) must be compiled
// with matching modes; pairing itself (MAC address) is unchanged.
//
// Defaults to 1: current HiveInside firmware is GATT-ONLY (its broadcast/beacon
// advertising mode was removed), so it never emits the manufacturer-data blob
// parseHiveInside() expects. Without the GATT client a paired HiveInside is
// found by MAC during the scan but yields no measurement, so HiveHub uploads
// nothing for it. HolyIot beacons are unaffected — they carry advertising data,
// so the post-scan GATT step is skipped for them.
#ifndef HIVEINSIDE_USE_GATT
#define HIVEINSIDE_USE_GATT 1
#endif

// Timeout for each GATT connection attempt in seconds (NimBLE unit).
// The BLE stack gives up and the slot stays not-present after this window.
#ifndef HIVEINSIDE_GATT_CONNECT_TIMEOUT_S
#define HIVEINSIDE_GATT_CONNECT_TIMEOUT_S 5
#endif

// Seconds before HiveHub's next scan that HiveInside should already be awake
// and listening. Written to the wake-sync characteristic each cycle as
// (sendInterval - lead). HiveHub holds a fixed boot-to-boot cadence (see
// enterDeepSleepUntilNextCycle in storage_power.cpp), so this lead need only
// cover the small in-cycle scan offset plus one interval of HiveInside RC-timer
// drift in the "HiveInside wakes late" direction; the "wakes early" direction is
// absorbed by HiveInside's SYNC_LISTEN_MS. Balanced default: 60 s lead paired
// with HiveInside SYNC_LISTEN_MS = 150 s. Raise both together for more drift
// tolerance at the cost of HiveInside awake time.
#ifndef HIVEINSIDE_SYNC_LEAD_S
#define HIVEINSIDE_SYNC_LEAD_S 60
#endif

// ==============================
// HIVEINSIDE FIRMWARE-OVER-BLE (OTA relay)
// ==============================
// HiveHub (the only WiFi node) relays a HiveInside firmware image to a paired
// HiveInside ESP32-C6 over GATT, the same way it relays a BeeCounter image over
// I2C (see updateBeeCounter / bee_counter_client). The backend queues an
// `update_hiveinside` command with the image URL + CRC-32; HiveHub STREAMS the
// HTTPS download straight into the HiveInside OTA characteristics (it never
// buffers the whole >1 MB image — the WROOM has no PSRAM), and the HiveInside
// device verifies the end-to-end CRC before swapping its OTA slot.
//
// This needs only a GATT *client* connection, so it is independent of
// HIVEINSIDE_USE_GATT (which selects how normal measurements are read). Set to 0
// to compile the relay out.
#ifndef HIVEINSIDE_OTA_ENABLED
#define HIVEINSIDE_OTA_ENABLED 1
#endif

// Largest DATA chunk (bytes) written per GATT write. Capped to stay inside the
// default NimBLE ATT MTU envelope; the relay further clamps this to the value
// actually negotiated with the device (MTU − 3).
#ifndef HIVEINSIDE_OTA_CHUNK_MAX
#define HIVEINSIDE_OTA_CHUNK_MAX 244
#endif

// True when the GATT-client scaffolding (address-type capture during the scan)
// must be compiled in: either normal GATT reads or the OTA relay needs it.
#define HIVEINSIDE_GATT_CLIENT (HIVEINSIDE_USE_GATT || HIVEINSIDE_OTA_ENABLED)

// ==============================
// BLE vs WIRED SENSOR ARBITRATION (collision avoidance)
// ==============================
// When a paired in-hive BLE sensor reports a capability, the wired sensor that
// measures the SAME in-hive quantity is skipped that cycle, so each upload
// carries one authoritative value per field instead of duplicate/conflicting
// readings. Arbitration is per slot: hive 1 follows the slot-1 BLE sensor,
// hive 2 the slot-2 sensor.
//
// The SHT40 is deliberately NOT arbitrated: it is an AMBIENT (outside-hive)
// temp/humidity sensor, and the BLE in-hive humidity lands in its own
// ble_{slot}_humidity_percent field — a different measurement that never
// collides — so the SHT40 always stays on.
#ifndef BLE_OVERRIDES_WIRED
#define BLE_OVERRIDES_WIRED 1
#endif
#ifndef BLE_OVERRIDE_DS18B20
#define BLE_OVERRIDE_DS18B20 BLE_OVERRIDES_WIRED   // in-hive temperature
#endif
#ifndef BLE_OVERRIDE_MICS
#define BLE_OVERRIDE_MICS BLE_OVERRIDES_WIRED       // in-hive acoustics (INMP441)
#endif
#ifndef BLE_OVERRIDE_ACCEL
#define BLE_OVERRIDE_ACCEL BLE_OVERRIDES_WIRED      // in-hive vibration (LIS3DH)
#endif

// ==============================
// WIRELESS SENSOR CATALOG (configurator)
// ==============================
// The secrets.h configurator can describe up to six wireless BLE sensors across
// three categories: in-hive (max 2), scale (max 2) and bee counter (max 2). The
// in-hive bridge (ENABLE_BLE_SCAN above) is the only category the current
// firmware reads; the two flags below — plus the per-slot WSCALE_* / WBEECNT_*
// and INHIVE_* TYPE / PROTOCOL / GATT-UUID macros the configurator writes — are
// captured for a future firmware build and are otherwise unused today.
#ifndef ENABLE_WIRELESS_SCALE
#define ENABLE_WIRELESS_SCALE 0
#endif
#ifndef ENABLE_WIRELESS_BEECOUNTER
#define ENABLE_WIRELESS_BEECOUNTER 0
#endif

// ------------------------------------------------------------------
// HiveTraffic (wireless entrance bee counter, BLE/GATT)
// ------------------------------------------------------------------
// When ENABLE_WIRELESS_BEECOUNTER is set, the firmware acts as a GATT client:
// once per upload cycle it connects to each paired HiveTraffic MAC
// (counter_mac{0,1}, paired in the portal or seeded via WBEECNT_n_MAC), reads
// its JSON measurement characteristic and folds the lifetime IN/OUT totals into
// the same bee_counter_{slot}_* fields the wired I2C BeeCounter uses. The wire
// format is totals-only: the backend differences consecutive totals into
// per-interval counts (see 2026-easy-bee-counter/docs/ble-mode.md), so no
// CMD_LATCH reset is written. A slot with a paired MAC uses BLE; a slot without
// one falls back to the wired I2C BeeCounter. All HiveTraffic devices share one
// service/characteristic UUID, so a single pair of macros covers both slots.
#ifndef BEECOUNTER_GATT_SERVICE_UUID
#define BEECOUNTER_GATT_SERVICE_UUID "8e8b0101-7a1c-4b9e-9a2f-1d6e0b9c1a01"
#endif
#ifndef BEECOUNTER_GATT_CHAR_UUID
#define BEECOUNTER_GATT_CHAR_UUID    "8e8b0102-7a1c-4b9e-9a2f-1d6e0b9c1a01"
#endif
// Seconds to wait for the GATT connection, then for the characteristic read.
#ifndef BEECOUNTER_GATT_CONNECT_TIMEOUT_S
#define BEECOUNTER_GATT_CONNECT_TIMEOUT_S 12
#endif
#ifndef BEECOUNTER_GATT_DISCONNECT_TIMEOUT_MS
#define BEECOUNTER_GATT_DISCONNECT_TIMEOUT_MS 2000
#endif

// ==============================
// BEEHIVEMONITORING.COM GATT SENSORS (HiveHeart / HiveScale)
// ==============================
// HiveHeart (in-hive) and HiveScale (weight) are read over GATT: the firmware
// connects to a paired MAC, subscribes to one notify characteristic, takes the
// pushed notification and disconnects (see firmware/src/beehive_gatt.cpp). Both
// products share the same service + characteristic UUID; only the payload (and
// the configured slot type) differ. Enable per device in secrets.h, pair the
// MACs in the provisioning portal (or seed INHIVE_n_MAC / WSCALE_n_MAC here).
#ifndef ENABLE_BEEHIVE_GATT
#define ENABLE_BEEHIVE_GATT 0
#endif

#ifndef BEEHIVE_GATT_SERVICE_UUID
#define BEEHIVE_GATT_SERVICE_UUID "0d01c3b8-eff2-44bc-9260-3256eb957268"
#endif
#ifndef BEEHIVE_GATT_CHAR_UUID
#define BEEHIVE_GATT_CHAR_UUID    "513849eb-913d-4f80-8c44-3f0685533d6e"
#endif

// Seconds to wait for the GATT connection, and then for the one notification.
// These devices can be slow to accept a connection (~12 s seen in captures), so
// keep the connect window generous.
#ifndef BEEHIVE_GATT_CONNECT_TIMEOUT_S
#define BEEHIVE_GATT_CONNECT_TIMEOUT_S 20
#endif
#ifndef BEEHIVE_GATT_NOTIFY_TIMEOUT_S
#define BEEHIVE_GATT_NOTIFY_TIMEOUT_S 5
#endif

// Milliseconds to wait, after the read, for the link to actually close before
// freeing the client. These devices drop the link themselves but not always
// promptly; deleting a still-connected client defers the free and leaves it for
// NimBLEDevice::deinit() to terminate after the host stack is disabled — that is
// the source of "ble_gap_terminate failed: rc=30" (BLE_HS_EDISABLED). Normal
// BLE teardown completes well within this budget.
#ifndef BEEHIVE_GATT_DISCONNECT_TIMEOUT_MS
#define BEEHIVE_GATT_DISCONNECT_TIMEOUT_MS 1000
#endif

// ==============================
// PIN MAP — 30-pin ESP32 DevKit (original board)
// ==============================
#if !defined(HIVEHUB_BOARD_XIAO_C6) && !defined(CONFIG_IDF_TARGET_ESP32C6)
#define HX1_DOUT     16
#define HX1_SCK      17
#define HX2_DOUT     32
#define HX2_SCK      33
#define ONE_WIRE_PIN  4
#define I2C_SDA      21
#define I2C_SCL      22
#define SD_CS         5
#define SD_SCK       18
#define SD_MISO      23
#define SD_MOSI      19
// External button. Wire button between this pin and GND. Uses INPUT_PULLUP.
// Short press: start WiFi provisioning AP.
// Long press: reset Preferences and reboot.
// GPIO27 is RTC-capable so it can wake the ESP32 from deep sleep via EXT0.
#define SETUP_BUTTON_PIN 27
#endif // !(HIVEHUB_BOARD_XIAO_C6 || CONFIG_IDF_TARGET_ESP32C6)

// ==============================
// PIN MAP — XIAO ESP32-C6 (compact RISC-V variant)
// ==============================
// Only the 11 front-header pins D0–D10 are used. There is no HX711 on this board
// (ENABLE_HX711 forced 0 above) — scales are I2C NAU7802 channels on D4/D5 — so
// D0/D1 are free. The V0.4 breakout uses D1 for a single DS18B20 1-Wire bus
// (D0 is unused). The INMP441 mic is unsupported; pair BLE in-hive sensors for
// acoustics/vibration. Deep-sleep button wake uses
// esp_deep_sleep_enable_gpio_wakeup() (no RTC GPIO subsystem on C6); see
// storage_power.cpp for the platform-specific guard.
#if defined(HIVEHUB_BOARD_XIAO_C6) || defined(CONFIG_IDF_TARGET_ESP32C6)
#define ONE_WIRE_PIN     1   // D1 — DS18B20 in-hive temperature bus (4.7k pull-up on-board)
#define I2C_SDA         22   // D4 (XIAO SDA label)
#define I2C_SCL         23   // D5 (XIAO SCL label)
#define SD_CS           21   // D3
#define SD_SCK          19   // D8 (XIAO SCK label)
#define SD_MISO         20   // D9 (XIAO MISO label)
#define SD_MOSI         18   // D10 (XIAO MOSI label)
// D2 is the setup button; button-to-GND, INPUT_PULLUP.
#define SETUP_BUTTON_PIN 2   // D2
#endif // HIVEHUB_BOARD_XIAO_C6 || CONFIG_IDF_TARGET_ESP32C6

// ==============================
// XIAO ESP32-C6 ANTENNA SELECTION
// ==============================
// The XIAO ESP32-C6 has both a built-in ceramic patch antenna and a u.FL
// connector for an external antenna. Selection uses the on-board FM8625H RF
// switch, driven by TWO internal-trace GPIOs (not broken out on the headers):
//   GPIO3  (RF_SWITCH_EN)  — must be driven LOW to ENABLE the RF switch. This
//                            is required for EITHER antenna; leaving it HIGH
//                            disables the switch and cripples the radio.
//   GPIO14 (RF_ANT_SELECT) — LOW  → built-in ceramic antenna (default)
//                            HIGH → external u.FL antenna
// To use an external antenna, add to secrets.h:
//   #define XIAO_C6_USE_EXTERNAL_ANTENNA 1
#if defined(HIVEHUB_BOARD_XIAO_C6) || defined(CONFIG_IDF_TARGET_ESP32C6)
#ifndef XIAO_C6_USE_EXTERNAL_ANTENNA
#define XIAO_C6_USE_EXTERNAL_ANTENNA 0
#endif
// GPIO3: RF switch enable (active-low). Driven LOW at boot to power the switch.
#ifndef XIAO_C6_RF_SWITCH_EN_GPIO
#define XIAO_C6_RF_SWITCH_EN_GPIO 3
#endif
// GPIO14: antenna select (LOW = internal ceramic, HIGH = external u.FL).
#ifndef XIAO_C6_ANTENNA_SELECT_GPIO
#define XIAO_C6_ANTENNA_SELECT_GPIO 14
#endif
#endif // HIVEHUB_BOARD_XIAO_C6 || CONFIG_IDF_TARGET_ESP32C6

// External button shared constants (both board variants)
static const unsigned long BUTTON_DEBOUNCE_MS = 50;
static const unsigned long BUTTON_LONG_PRESS_MS = 10000;

static const int MAX_WIFI_NETWORKS = 3;
static const unsigned long WIFI_CONNECT_TIMEOUT_MS = 15000;
static const unsigned long PROVISIONING_TIMEOUT_MS = 10UL * 60UL * 1000UL;
static const unsigned long OTA_CHECK_INTERVAL_MS = 6UL * 60UL * 60UL * 1000UL;
static const unsigned long COMMAND_CHECK_INTERVAL_MS = 5UL * 60UL * 1000UL;
static const unsigned long CALIBRATION_MODE_DEFAULT_INTERVAL_MS = 5UL * 1000UL;
static const unsigned long CALIBRATION_MODE_MIN_INTERVAL_MS = 2UL * 1000UL;
static const unsigned long CALIBRATION_MODE_MAX_INTERVAL_MS = 30UL * 1000UL;
static const unsigned long CALIBRATION_MODE_DEFAULT_TIMEOUT_MS = 10UL * 60UL * 1000UL;
static const unsigned long CALIBRATION_MODE_MAX_TIMEOUT_MS = 30UL * 60UL * 1000UL;

// Power saving behavior. With deep sleep enabled, the ESP32 wakes for one
// measurement/upload cycle, then sleeps until the next send interval.
static const bool DEEP_SLEEP_ENABLED = true;
static const bool WAKE_BUTTON_FROM_DEEP_SLEEP = true;
static const unsigned long MIN_DEEP_SLEEP_MS = 30UL * 1000UL;
static const uint64_t US_PER_MS = 1000ULL;

static const char* CACHE_FILE = "/cache.ndjson";
static const char* TEMP_FILE = "/cache.tmp";
static const char* CACHE_BAD_FILE = "/cache_bad.ndjson";
static const char* BACKUP_FILE = "/measurements.ndjson";

// SD behavior:
// - BACKUP_FILE is append-only and is never deleted by the firmware.
// - CACHE_FILE is ONLY the retry queue for rows that still need backend upload.
//   Successful live uploads are not written to the cache file.
static const bool SD_KEEP_PERSISTENT_BACKUP = true;
static const size_t BACKUP_WARN_SIZE_BYTES = 50UL * 1024UL * 1024UL;
static const size_t CACHE_MAX_BYTES = 512UL * 1024UL;
// One measurement = one NDJSON line. A fully-populated multi-hive upload (up to
// 18 hives, each with nested ble/accel/hiveheart/hivescale objects) is far larger
// than the old two-hive line, so this cap was raised from 4 KB to keep such lines
// from being refused by the SD retry cache / persistent backup (data loss while
// offline) or dropped from the last-measurement panel. The live HTTPS upload is
// not bounded by this; it only gates on-SD storage and the cached "last reading".
static const size_t CACHE_MAX_LINE_BYTES = 16384UL;
static const uint16_t CACHE_UPLOAD_MAX_LINES_PER_CYCLE = 25;
static const uint16_t CAPTIVE_DNS_PORT = 53;
static const size_t LAST_MEASUREMENT_TAIL_BYTES = CACHE_MAX_LINE_BYTES * 2;
