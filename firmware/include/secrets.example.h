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

// Keep HTTPS/TLS verification enabled by default. Set to 1 ONLY when this
// device connects to a trusted LAN server over plain http://. HTTPS URLs remain
// supported and certificate-verified when this is enabled.
// #define ALLOW_INSECURE_HTTP 1

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
#define ENABLE_MAX17048_BATTERY  0

// MAX17048 LiPo fuel gauge alert threshold, in percent.
#define MAX17048_ALERT_PERCENT   20

// ==============================
// POWER / DEEP SLEEP
// ==============================
// Deep-sleep behavior. Defaults live in config.h (deep sleep ON, button wake ON,
// 30 s minimum sleep); config.h only defines each if it is NOT already set here,
// so any override below wins and survives every git pull. Keep 0/1 numeric —
// the firmware uses preprocessor #if / plain boolean tests.
//
// Uncomment to override:
// #define DEEP_SLEEP_ENABLED          0            // stay awake (bench / mains-powered node)
// #define WAKE_BUTTON_FROM_DEEP_SLEEP 0            // ignore the setup button as a wake source
// #define MIN_DEEP_SLEEP_MS           (30UL*1000UL) // floor on a single sleep, in ms

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
// The 1-Wire DS18B20 probes (hive_N_temp_c) are optional and OFF by default on
// EVERY board (including the XIAO C6, which defaulted them ON before v0.24.0).
// Otherwise in-hive temperature comes from a paired in-hive BLE sensor (see
// below); when both are present the wired probe wins and BLE is the fallback.
// NOTE: setting this to 1 ALSO requires uncommenting the OneWire +
// DallasTemperature libraries in the matching platformio.ini env, or the build
// will fail to find <OneWire.h>/<DallasTemperature.h>.
#define ENABLE_DS18B20_HIVE_TEMP 0

// ==============================
// AMBIENT SENSOR — TEMP / HUMIDITY / PRESSURE (pick exactly one)
// ==============================
// The device-level ambient (outside-hive) sensor on the shared I2C bus. Exactly
// one family may be enabled; SHT4x is the default. Switching to SHT3x or BME280
// ALSO requires uncommenting that library in platformio.ini (see the notes
// there). The BME280 additionally reports barometric pressure as
// ambient_pressure_hpa. Defaults live in config.h — uncomment to override:
// #define ENABLE_SHT4X_AMBIENT 1   // Sensirion SHT40 (default)
// #define ENABLE_SHT3X_AMBIENT 1   // Sensirion SHT31/SHT35 (set SHT4x to 0)
// #define ENABLE_BME280_AMBIENT 1  // Bosch BME280, adds ambient pressure (set SHT4x to 0)

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
// MULTI-HIVE PRE-SEED (optional, up to 16 hives)
// ==============================
// Firmware v0.20.0+ generalises HiveHub to a dynamic registry of up to
// MAX_HIVES hives (16 supported wired scales), each with one scale source and at most one in-hive
// sensor — see hive_config.h and docs/multi-hive.md. The provisioning portal
// (hold the setup button after flashing) configures this registry directly and
// is the recommended way to set it up. To pre-seed it from secrets.h instead
// (so a device already knows every hive on FIRST BOOT, before ever visiting
// the portal), set HIVE_COUNT and one HIVE_<n>_JSON per hive, 1..HIVE_COUNT.
// The website config tool (website/configurator.html) generates these for you
// from a per-hive form; hand-editing is only for reference.
//
// Each HIVE_<n>_JSON is the exact blob shape the portal itself saves — see
// hiveToJson()/hiveFromJson() in firmware/src/hive_config.cpp:
//   i   hive index (matches <n>)
//   n   optional display name
//   s   scale array (0 or 1 entries — MAX_SCALES_PER_HIVE is 1): either
//         {"b":"hx","hx":0|1,"off":<raw offset>,"fac":<kg factor>}       (HX711, classic board only)
//       or {"b":"nau","mux":-1..7,"adc":1|2,"off":...,"fac":...}         (NAU7802; mux -1 = main bus)
//   ds  optional DS18B20 1-Wire ROM, 16 hex chars (omit to fall back to
//       probe enumeration order — fine if you don't know ROMs yet)
//   bl  optional array with AT MOST ONE entry: {"t":"holyiot"|"ruuvitag"|
//       "hiveinside"|"hiveheart"|"beecounter"|"hivescale","m":"AA:BB:CC:DD:EE:FF"}
//       — "hivescale" is a WIRELESS SCALE SOURCE (used instead of "s", not
//       alongside a wired one or "ds"); every other type is the hive's one
//       non-scale in-hive sensor and is mutually exclusive with "ds".
//       "beecounter" (HiveTraffic) is read from the registry over BLE/GATT and
//       works on ANY hive up to MAX_HIVES. BLE/GATT is the ONLY BeeCounter
//       transport — wired I2C BeeCounters are no longer supported.
//
// Example: 3 hives — hive 1 on HX711 #1 with a HiveHeart GATT sensor, hive 2
// on HX711 #2 with a DS18B20 probe, hive 3 on a wireless HiveScale:
//#define HIVE_COUNT 3
//#define HIVE_1_JSON "{\"i\":1,\"n\":\"Hive 1\",\"s\":[{\"b\":\"hx\",\"hx\":0,\"off\":0,\"fac\":-7050.0}],\"bl\":[{\"t\":\"hiveheart\",\"m\":\"AA:BB:CC:DD:EE:01\"}]}"
//#define HIVE_2_JSON "{\"i\":2,\"n\":\"Hive 2\",\"s\":[{\"b\":\"hx\",\"hx\":1,\"off\":0,\"fac\":-7050.0}],\"ds\":\"28FF64B2711304A3\"}"
//#define HIVE_3_JSON "{\"i\":3,\"n\":\"Hive 3\",\"s\":[],\"bl\":[{\"t\":\"hivescale\",\"m\":\"AA:BB:CC:DD:EE:03\"}]}"
// Also needs, since hive 1 and 3 use beehivemonitoring.com GATT sensors:
//#define ENABLE_BEEHIVE_GATT 1
//
// Leaving HIVE_COUNT at its default (0, below) keeps the historical behavior:
// first boot migrates whatever legacy 2-slot keys exist (or none) into a
// 2-hive registry, exactly as before this feature existed. Once a device has
// saved anything from the on-device portal, HIVE_COUNT/HIVE_i_JSON are never
// consulted again — the portal's NVS registry always wins from then on.
#define HIVE_COUNT 0

// ==============================
// WIRELESS SENSOR CATALOG — LEGACY 2-HIVE FORM (optional)
// ==============================
// Only read when HIVE_COUNT above is 0 (i.e. no HIVE_i_JSON pre-seed), and only
// pre-seeds a beehivemonitoring.com HiveHeart pairing for the first two hives
// (device_prefs.cpp writes INHIVE_n_MAC straight into the HiveHeart MAC slot
// regardless of what device is actually paired) — it predates the general
// per-hive sensor type. HolyIot / RuuviTag / HiveInside beacons and HiveTraffic
// counters cannot be pre-seeded through this legacy form; use HIVE_COUNT /
// HIVE_i_JSON above for those, or pair anything from the provisioning portal.
//#define INHIVE_1_MAC                 "AA:BB:CC:DD:EE:01"   // HiveHeart on hive 1
//#define INHIVE_2_MAC                 "AA:BB:CC:DD:EE:02"   // HiveHeart on hive 2

// Wireless scale category (placeholder — not consumed yet).
#define ENABLE_WIRELESS_SCALE        0

// HiveTraffic wireless entrance bee counter (BLE/GATT — the ONLY supported
// BeeCounter transport; wired I2C BeeCounters are no longer supported). Set to
// 1 and pair each counter's MAC in the provisioning portal (to any hive up to
// MAX_HIVES), or seed the legacy WBEECNT_n_MAC below for hives 1-2. A hive
// without a paired MAC reports no bee-counter data. The firmware uses the
// shared BEECOUNTER_GATT_* UUIDs.
#define ENABLE_WIRELESS_BEECOUNTER   0
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
