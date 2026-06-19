// config.h — compile-time configuration: pins, timeouts, file paths and
// optional-feature defaults. Pure preprocessor + constants, no globals.
//
// secrets.h is included first so that per-device overrides (feature flags,
// pin choices, sample rates) take effect before the defaults below.
#pragma once

#include "secrets.h"

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
// The two 1-Wire DS18B20 probes (hive_1_temp_c / hive_2_temp_c) are now an
// OPTIONAL sensor: in-hive temperature can instead come from a paired HolyIot
// 25015 BLE sensor (see below). Default 1 so existing wired builds are
// unchanged; set to 0 in secrets.h on devices that rely on the BLE sensor.
#ifndef ENABLE_DS18B20_HIVE_TEMP
#define ENABLE_DS18B20_HIVE_TEMP 1
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
// When enabled, HiveScale connects to a paired HiveInside sensor as a GATT
// central *after* the passive scan locates it by MAC address. It reads the full
// JSON measurement characteristic (every FFT band, RMS, peak, mic bands) and
// then disconnects. Only devices found by MAC without recognisable advertising
// data trigger a connection attempt, so HolyIot and advertising-mode HiveInside
// sensors are unaffected. Set to 0 to use passive advertising scan exclusively.
//
// Both HiveScale (this flag) and HiveInside (BLE_MODE_GATT) must be compiled
// with matching modes; pairing itself (MAC address) is unchanged.
//
// Defaults to 1: current HiveInside firmware is GATT-ONLY (its broadcast/beacon
// advertising mode was removed), so it never emits the manufacturer-data blob
// parseHiveInside() expects. Without the GATT client a paired HiveInside is
// found by MAC during the scan but yields no measurement, so HiveScale uploads
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

// ==============================
// HIVEINSIDE FIRMWARE-OVER-BLE (OTA relay)
// ==============================
// HiveScale (the only WiFi node) relays a HiveInside firmware image to a paired
// HiveInside ESP32-C6 over GATT, the same way it relays a BeeCounter image over
// I2C (see updateBeeCounter / bee_counter_client). The backend queues an
// `update_hiveinside` command with the image URL + CRC-32; HiveScale STREAMS the
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
// PIN MAP
// ==============================
#define HX1_DOUT 16
#define HX1_SCK  17
#define HX2_DOUT 32
#define HX2_SCK  33
#define ONE_WIRE_PIN 4
#define I2C_SDA 21
#define I2C_SCL 22
#define SD_CS   5
#define SD_SCK  18
#define SD_MISO 23
#define SD_MOSI 19

// External button. Wire button between this pin and GND. Uses INPUT_PULLUP.
// Short press: start WiFi provisioning AP.
// Long press: reset Preferences and reboot.
#define SETUP_BUTTON_PIN 27
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
static const size_t CACHE_MAX_LINE_BYTES = 4096UL;
static const uint16_t CACHE_UPLOAD_MAX_LINES_PER_CYCLE = 25;
static const uint16_t CAPTIVE_DNS_PORT = 53;
static const size_t LAST_MEASUREMENT_TAIL_BYTES = CACHE_MAX_LINE_BYTES * 2;
