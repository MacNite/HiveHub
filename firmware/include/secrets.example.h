#ifndef SECRETS_H
#define SECRETS_H

// ==============================
// DEVICE IDENTITY
// ==============================
// Unique per device (can be overwritten later via backend provisioning)
#define DEVICE_ID        "hive-001"

// Per-device API key - can be created via CLI:   openssl rand -hex 32
#define API_KEY          "your-api-key-here"

// Human-entered pairing code. The firmware seeds this into Preferences and sends it
// with every measurement so the backend can auto-create an unclaimed device.
// Change CLAIM_CODE_REVISION when you want an OTA firmware update to overwrite
// the claim_code stored in Preferences.
#define CLAIM_CODE       "ABCD-1234"
#define CLAIM_CODE_REVISION 1

// ==============================
// BACKEND CONFIG
// ==============================
// Base URL of your backend (no trailing slash required)
#define API_BASE_URL     "https://your-backend-domain.com"

// ==============================
// WIFI FALLBACK CREDENTIALS
// ==============================
// These are ONLY used on first boot (seed into Preferences)
// or as fallback if Preferences are empty

// --- WiFi Set 1 ---
#define WIFI1_SSID       "your-wifi-ssid-1"
#define WIFI1_PASS       "your-wifi-password-1"

// --- WiFi Set 2 ---
//#define WIFI2_SSID       "your-wifi-ssid-2"
//#define WIFI2_PASS       "your-wifi-password-2"

// --- WiFi Set 3 ---
//#define WIFI3_SSID       "your-wifi-ssid-3"
//#define WIFI3_PASS       "your-wifi-password-3"

// ==============================
// OPTIONAL OFF-GRID MODULES
// ==============================
// Keep these as numeric 0/1 values because the firmware uses preprocessor #if.
// They are per-device build configuration rather than secrets, but this project
// already uses secrets.h as the local, untracked per-device config file.
#define ENABLE_INA219_SOLAR      0
#define ENABLE_MAX17048_BATTERY  0

// INA219 solar monitor. Default address is 0x40 on most breakout boards.
#define INA219_I2C_ADDRESS       0x40

// MAX17048 LiPo fuel gauge alert threshold, in percent.
#define MAX17048_ALERT_PERCENT   20

// ==============================
// INMP441 STEREO MICROPHONES
// ==============================
// Two INMP441 I2S MEMS microphones sharing a single I2S bus.
// Wire L/R on one mic to GND (left channel) and L/R on the other mic to 3.3V
// (right channel). Both mics share BCLK, WS (LRCLK) and SD (data) lines.
//
// Default pinout (free on this board):
//   GPIO 14 -> BCLK (SCK on the mic boards)
//   GPIO 13 -> WS   (LRCLK / WS on the mic boards)
//   GPIO 34 -> SD   (data out from both mics, ESP32 input-only pin)
//
// VDD on each mic -> 3.3V, GND -> GND.
//
// The wired in-hive microphone is OPTIONAL and OFF by default. Set to 1 on
// builds that fit two INMP441 mics.
#define ENABLE_INMP441_MICS      0

#define INMP441_BCLK_PIN         14
#define INMP441_WS_PIN           13
#define INMP441_SD_PIN           34

// Sample rate in Hz. 16 kHz is plenty for hive sounds (fundamental ~200 Hz,
// harmonics up to a few kHz) and keeps the buffer small.
#define INMP441_SAMPLE_RATE      16000

// Number of stereo frames captured per measurement cycle.
// 8000 frames at 16 kHz = ~500 ms of audio.
#define INMP441_SAMPLE_FRAMES    8000

// ==============================
// DS18B20 WIRED IN-HIVE TEMPERATURE (optional)
// ==============================
// The two 1-Wire DS18B20 probes (hive_1_temp_c / hive_2_temp_c) are optional and
// OFF by default. Set to 1 on builds that fit the wired probes; otherwise in-hive
// temperature comes from a paired in-hive BLE sensor instead (see below). When
// both are present the wired probe wins and the BLE temperature is the fallback.
#define ENABLE_DS18B20_HIVE_TEMP 0

// ==============================
// HOLYIOT 25015 IN-HIVE BLE SENSOR (optional)
// ==============================
// Replaces the wired LIS3DH/LIS2DH12 accelerometer. The HolyIot 25015 is an
// nRF54L15 BLE beacon with an SHT40 (temp/humidity), LPS22HB (pressure) and
// LIS2DH12 (acceleration). The ESP32 scans for it passively each cycle and folds
// the readings into the upload — no wiring, just battery beacons in the hive.
//
// Pair up to two sensors (slot 1 -> hive 1, slot 2 -> hive 2) from the
// provisioning portal: open the setup page, use "scan for nearby sensors", and
// paste each MAC into a slot. The MACs persist in Preferences.
//
// NOTE: the advertisement byte layout in firmware/src/ble_sensor.cpp is a
// documented best guess (HolyIot publish no spec). After sniffing one real
// packet, correct the HOLYIOT_OFF_* / *_SCALE constants there.
#define ENABLE_BLE_SCAN       1

// Seconds to scan for the paired beacons each cycle (they advertise ~0.5–2 s).
#define HOLYIOT_BLE_SCAN_SECONDS 6

// Active scan also fetches the device name (handy when pairing); costs a little
// more power. Set to 0 for passive-only scanning.
#define HOLYIOT_BLE_ACTIVE_SCAN  1

// 16-bit BLE company id in the manufacturer-specific advertisement. 0xFFFF is a
// common generic default; override once the real id is known from a capture.
#define HOLYIOT_COMPANY_ID       0xFFFF

// RuuviTag four-in-one beacons (temp/humidity/pressure/accel) ride the SAME scan
// bridge and are auto-detected by Ruuvi's registered company id below — no extra
// enable flag. Pair a RuuviTag exactly like a HolyIot: paste its MAC into a slot.
#define RUUVI_COMPANY_ID         0x0499

// ==============================
// WIRELESS SENSOR CATALOG (optional)
// ==============================
// The secrets.h configurator (website/configurator.html) can describe up to six
// wireless BLE sensors across three categories — at most 2 in-hive sensors, 2
// scales and 2 bee counters. In-hive sensors are scanned by the bridge above
// (ENABLE_BLE_SCAN); HiveTraffic bee counters are read over GATT.
//
// Supported in-hive types: HolyIot 25015 (beacon), RuuviTag 4-in-1 (beacon),
// HiveInside ESP32-C6 (beacon or GATT), HiveHeart (GATT, beehivemonitoring.com).
// Supported scale: HiveScale (GATT, beehivemonitoring.com) via ENABLE_BEEHIVE_GATT.
// Supported bee counter: HiveTraffic (GATT). The separate ENABLE_WIRELESS_SCALE
// flag below is captured for a future build and not consumed yet.
//
// Each in-hive slot (INHIVE_1 -> hive 1, INHIVE_2 -> hive 2) records the chosen
// sensor type, its transport and, for GATT devices, the service / characteristic
// UUIDs. Example (HiveHeart on hive 1, HolyIot beacon on hive 2):
//#define HIVEINSIDE_USE_GATT          1
//#define INHIVE_1_TYPE                "hiveheart"
//#define INHIVE_1_PROTOCOL            "gatt"
//#define INHIVE_1_GATT_SERVICE_UUID   "0d01c3b8-eff2-44bc-9260-3256eb957268"
//#define INHIVE_1_GATT_CHAR_UUID      "513849eb-913d-4f80-8c44-3f0685533d6e"
//#define INHIVE_2_TYPE                "holyiot"
//#define INHIVE_2_PROTOCOL            "beacon"

// Wireless scale category (placeholder — not consumed yet).
#define ENABLE_WIRELESS_SCALE        0

// HiveTraffic wireless entrance bee counter (GATT). Set to 1 and pair each
// counter's MAC in the provisioning portal, or seed WBEECNT_n_MAC below. A
// slot with a paired MAC is read over BLE; a slot without one falls back to the
// wired I2C BeeCounter. The firmware uses the shared BEECOUNTER_GATT_* UUIDs.
#define ENABLE_WIRELESS_BEECOUNTER   0
//#define WBEECNT_1_TYPE               "beecounter"
//#define WBEECNT_1_PROTOCOL           "gatt"
//#define WBEECNT_1_MAC                "AA:BB:CC:DD:EE:FF"   // HiveTraffic counter 1
//#define WBEECNT_2_MAC                "AA:BB:CC:DD:EE:00"   // HiveTraffic counter 2

// ==============================
// Use external Antenna on XIOA ESP32 C6
// ==============================
// 0: internal Antenna
// 1: external Antenna

#define XIAO_C6_USE_EXTERNAL_ANTENNA 0

// ==============================
// OPTIONAL FLAGS
// ==============================

// If true, always reseed Preferences from secrets on boot (DANGEROUS)
#define FORCE_RESEED     false

// If true, enable extra serial debug logs
#define DEBUG_MODE       true

#endif // SECRETS_H