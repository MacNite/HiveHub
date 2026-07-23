// storage_power.cpp — SD storage and power/sleep implementation.
#include "storage_power.h"
#include "globals.h"
#include "config.h"

#include <SPI.h>
#include <WiFi.h>
#include <driver/gpio.h>
#ifndef CONFIG_IDF_TARGET_ESP32C6
#include <driver/rtc_io.h>
#endif

#include "scale_bus.h"
#include "i2c_bus.h"
#include "status_led.h"

#if ENABLE_INMP441_MICS
#include "mics.h"
#endif

String wakeReasonName(uint32_t wakeCauses) {
  // esp_sleep_get_wakeup_causes() returns a uint32_t bitmask; each cause is
  // checked with BIT(enum_value) because the ESP_SLEEP_WAKEUP_* values are
  // sequential indices, not powers of two.
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_TIMER))    return "timer";
#ifdef CONFIG_IDF_TARGET_ESP32C6
  // ESP32-C6 has no RTC GPIO / EXT0 subsystem; button wake uses GPIO wakeup.
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_GPIO))     return "button/gpio";
#else
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_EXT0))     return "button/ext0";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_EXT1))     return "ext1";
#endif
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_TOUCHPAD)) return "touchpad";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_ULP))      return "ulp";
  return "power-on/reset";
}

void releaseSleepPinHolds() {
#ifndef CONFIG_IDF_TARGET_ESP32C6
  // On classic ESP32/S2/S3/C3 the deep-sleep hold is a separate global switch;
  // disable it before releasing individual pin holds. The C6 has no such switch —
  // gpio_hold_en/dis() handle hold state for each pin individually.
  gpio_deep_sleep_hold_dis();
#endif
#if ENABLE_HX711
  gpio_hold_dis((gpio_num_t)HX1_SCK);
  gpio_hold_dis((gpio_num_t)HX2_SCK);
#endif
  gpio_hold_dis((gpio_num_t)SD_CS);

#ifdef CONFIG_IDF_TARGET_ESP32C6
  // On C6 the button pull-up is held via gpio_hold_en(); release it normally.
  gpio_hold_dis((gpio_num_t)SETUP_BUTTON_PIN);
#else
  // EXT0 wake config turns the button into an RTC IO. Return it to normal GPIO.
  rtc_gpio_deinit((gpio_num_t)SETUP_BUTTON_PIN);
#endif
}

uint32_t cyclesForInterval(unsigned long intervalMs) {
  if (sendIntervalMs == 0) return 1;
  unsigned long cycles = (intervalMs + sendIntervalMs - 1UL) / sendIntervalMs;
  if (cycles < 1UL) cycles = 1UL;
  return (uint32_t)cycles;
}

bool shouldCheckOtaThisCycle() {
  if (!DEEP_SLEEP_ENABLED) {
    return millis() - lastOtaCheckMs >= OTA_CHECK_INTERVAL_MS;
  }

  if (rtcCyclesUntilOta == 0) return true;

  rtcCyclesUntilOta--;
  return rtcCyclesUntilOta == 0;
}

void markOtaChecked() {
  rtcCyclesUntilOta = cyclesForInterval(OTA_CHECK_INTERVAL_MS);
}

bool rtcHasValidTime() {
  if (!rtcOk) return false;
  DateTime now = rtc.now();
  return now.year() >= 2024 && now.year() <= 2099;
}

void powerUpScales() {
#if ENABLE_HX711
  gpio_hold_dis((gpio_num_t)HX1_SCK);
  gpio_hold_dis((gpio_num_t)HX2_SCK);

  pinMode(HX1_SCK, OUTPUT);
  pinMode(HX2_SCK, OUTPUT);
  digitalWrite(HX1_SCK, LOW);
  digitalWrite(HX2_SCK, LOW);

  scale1.power_up();
  scale2.power_up();

  // HX711 needs a short settling period after power-up/reset at 10 SPS.
  delay(500);
#else
  // No HX711 on this build (e.g. XIAO C6). The I2C NAU7802 scales are re-powered
  // and re-calibrated each wake by scalebus::begin(), so there is nothing to do.
#endif
}

void powerDownScalesForSleep() {
  // Put every I2C NAU7802 (direct + behind each mux channel) into power-down
  // first, while the I2C bus is still active. This is the "sleep the NAU7802
  // before the ESP32 deep-sleeps" step; without it the ADC keeps converting and
  // burns milliamps through the whole sleep window. Each power-down write is
  // checked; a failure is logged with the chip's mux channel (see scale_bus)
  // and counted, because it can raise sleep current for the whole window.
  if (!scalebus::powerDownAllForSleep()) {
    Serial.println("[SLEEP] WARNING: not every NAU7802 confirmed power-down; sleep current may be elevated");
  }

#if ENABLE_HX711
  scale1.power_down();
  scale2.power_down();

  // Keep PD_SCK high during deep sleep so the HX711s and their bridge sensors
  // remain in power-down. Without GPIO hold, deep sleep may let these pins float.
  pinMode(HX1_SCK, OUTPUT);
  pinMode(HX2_SCK, OUTPUT);
  digitalWrite(HX1_SCK, HIGH);
  digitalWrite(HX2_SCK, HIGH);
  delayMicroseconds(80);

  gpio_hold_en((gpio_num_t)HX1_SCK);
  gpio_hold_en((gpio_num_t)HX2_SCK);
#endif
}

void shutdownWifiAndBt() {
  if (provisioningActive) {
    setupDnsServer.stop();
    setupServer.stop();
    WiFi.softAPdisconnect(true);
    provisioningActive = false;
  }

  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  btStop();
  delay(100);
}

bool initSdCard() {
  if (sdOk) return true;

  if (!sdBusInitialized) {
    SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
    sdBusInitialized = true;
  }

  sdOk = SD.begin(SD_CS);
  Serial.printf("[SD] %s\n", sdOk ? "OK" : "MISSING");
  return sdOk;
}

void prepareSdForSleep() {
  if (sdOk) {
    SD.end();
    sdOk = false;
  }

  SPI.end();
  sdBusInitialized = false;

  // Leave the SD card deselected if it remains powered.
  pinMode(SD_CS, OUTPUT);
  digitalWrite(SD_CS, HIGH);
  gpio_hold_en((gpio_num_t)SD_CS);
}

void configureButtonWake() {
  if (!WAKE_BUTTON_FROM_DEEP_SLEEP) return;

#ifdef CONFIG_IDF_TARGET_ESP32C6
  // ESP32-C6 has no RTC GPIO subsystem. Any GPIO can wake from deep sleep via
  // esp_deep_sleep_enable_gpio_wakeup(). gpio_hold_en() preserves the pull-up
  // state across the sleep boundary (C6 does not power-cycle GPIOs in deep sleep).
  gpio_pullup_en((gpio_num_t)SETUP_BUTTON_PIN);
  gpio_hold_en((gpio_num_t)SETUP_BUTTON_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << SETUP_BUTTON_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);
#else
  // GPIO27 is an RTC-capable pin on ESP32. The RTC pull-up lets the existing
  // button-to-GND wiring wake the device without an external pull-up. For the
  // lowest possible sleep current, use an external pull-up and remove this.
  rtc_gpio_init((gpio_num_t)SETUP_BUTTON_PIN);
  rtc_gpio_set_direction((gpio_num_t)SETUP_BUTTON_PIN, RTC_GPIO_MODE_INPUT_ONLY);
  rtc_gpio_pullup_en((gpio_num_t)SETUP_BUTTON_PIN);
  rtc_gpio_pulldown_dis((gpio_num_t)SETUP_BUTTON_PIN);
  esp_sleep_enable_ext0_wakeup((gpio_num_t)SETUP_BUTTON_PIN, 0);
#endif
}

void enterDeepSleep(unsigned long sleepMs) {
  if (!DEEP_SLEEP_ENABLED) return;

  if (calibrationModeActive) {
    Serial.println("[SLEEP] Calibration mode active; staying awake");
    return;
  }

  if (sleepMs < MIN_DEEP_SLEEP_MS) {
    Serial.println("[SLEEP] Interval too short for deep sleep; staying awake");
    return;
  }

  Serial.printf("[SLEEP] Entering deep sleep for %lu seconds\n", sleepMs / 1000UL);

  // Heartbeat: double blink to signal "cycle done, going to sleep". Runs only on
  // the real sleep path — the early returns above (calibration active / interval
  // too short) skip it because the device stays awake in those cases. The LED is
  // left off, so it draws nothing across the sleep window.
  statusLedSleepBlink();

  powerDownScalesForSleep();
  preparePowerMonitorsForSleep();
  // Last I2C users are done: shut the Wire peripheral down and leave SDA/SCL as
  // plain inputs (the external pull-ups park both lines high) so nothing is
  // back-powered or leaks through the ESP32 pads during deep sleep. Identical
  // and safe on both the classic ESP32 and the XIAO ESP32-C6; wake-up re-runs
  // i2cbus::begin() from setup(), so initialization is unaffected.
  i2cbus::endForSleep();
#if ENABLE_INMP441_MICS
  shutdownMicsI2s();
#endif
  prepareSdForSleep();
  shutdownWifiAndBt();

  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  esp_sleep_enable_timer_wakeup((uint64_t)sleepMs * US_PER_MS);
  configureButtonWake();

#ifndef CONFIG_IDF_TARGET_ESP32C6
  // Enable the global deep-sleep GPIO hold switch (classic ESP32/S2/S3/C3 only).
  // On C6, gpio_hold_en() already preserves pin state across deep sleep — no
  // separate global enable is needed or available.
  gpio_deep_sleep_hold_en();
#endif

  Serial.flush();
  esp_deep_sleep_start();
}

// Deep sleep until the next cycle boundary measured from THIS boot, rather than
// for a full interval measured from the (variable-length) end of the cycle. The
// in-hive BLE rendezvous scan happens near the start of each boot, so anchoring
// the sleep to boot keeps that scan on a stable cadence no matter how long the
// WiFi upload / cache replay / remote-config / OTA-check tail took this cycle.
// That removes the dominant source of wake-window misalignment with HiveInside.
//
// millis() has its origin at this boot, so it is the elapsed cycle time. We never
// pass less than MIN_DEEP_SLEEP_MS (enterDeepSleep would otherwise refuse to
// sleep); if the cycle ran long we just take the floor and re-align next boot.
void enterDeepSleepUntilNextCycle(unsigned long intervalMs) {
  unsigned long elapsed = millis();
  unsigned long sleepMs = (elapsed + MIN_DEEP_SLEEP_MS < intervalMs)
                            ? (intervalMs - elapsed)
                            : MIN_DEEP_SLEEP_MS;
  Serial.printf("[SLEEP] Cycle took %lums; sleeping %lums to hold a %lums cadence\n",
                elapsed, sleepMs, intervalMs);
  enterDeepSleep(sleepMs);
}

void preparePowerMonitorsForSleep() {
#if ENABLE_INA219_SOLAR
  if (solarMonitorOk) solarMonitor.powerSave(true);
#endif
}

String readLastNonEmptySdLine(const char* path) {
  if (!sdOk || !SD.exists(path)) return "";

  File file = SD.open(path, FILE_READ);
  if (!file) return "";

  size_t fileSize = file.size();
  if (fileSize == 0) {
    file.close();
    return "";
  }

  size_t start = fileSize > LAST_MEASUREMENT_TAIL_BYTES ? fileSize - LAST_MEASUREMENT_TAIL_BYTES : 0;
  if (!file.seek(start)) {
    file.close();
    return "";
  }

  // If we start in the middle of a large backup file, discard the partial line.
  if (start > 0) {
    file.readStringUntil('\n');
  }

  String lastLine;
  while (file.available()) {
    String line = file.readStringUntil('\n');
    line.trim();
    if (line.length() > 0 && line.length() <= CACHE_MAX_LINE_BYTES) {
      lastLine = line;
    }
    delay(0);
  }

  file.close();
  return lastLine;
}

void rememberLastMeasurement(const String& line) {
  if (line.length() == 0 || line.length() > CACHE_MAX_LINE_BYTES) return;
  lastMeasurementJson = line;
  lastMeasurementUpdatedMs = millis();
}

void ensureLastMeasurementLoaded() {
  if (lastMeasurementJson.length() > 0) return;

  if (!sdOk) {
    initSdCard();
  }

  String line = readLastNonEmptySdLine(BACKUP_FILE);
  if (line.length() > 0) {
    rememberLastMeasurement(line);
  }
}

size_t sdFileSize(const char* path) {
  if (!sdOk || !SD.exists(path)) return 0;

  File file = SD.open(path, FILE_READ);
  if (!file) return 0;

  size_t size = file.size();
  file.close();
  return size;
}

bool quarantineSdFile(const char* path, const char* quarantinePath, const char* label) {
  if (!sdOk || !SD.exists(path)) return true;

  Serial.printf("[%s] Quarantining %s as %s\n", label, path, quarantinePath);
  SD.remove(quarantinePath);

  if (SD.rename(path, quarantinePath)) {
    Serial.printf("[%s] Quarantined %s\n", label, quarantinePath);
    return true;
  }

  Serial.printf("[%s] Rename failed; removing %s instead\n", label, path);
  return SD.remove(path);
}

bool cacheFileLooksSane() {
  if (!sdOk || !SD.exists(CACHE_FILE)) return true;

  File file = SD.open(CACHE_FILE, FILE_READ);
  if (!file) {
    Serial.println("[CACHE] Cache file exists but cannot be opened. Quarantining/removing it.");
    quarantineSdFile(CACHE_FILE, CACHE_BAD_FILE, "CACHE");
    return false;
  }

  size_t size = file.size();
  file.close();

  if (size > CACHE_WARN_BYTES) {
    Serial.printf(
      "[CACHE] Warning: retry queue is large (%u bytes > %u bytes); replay will continue.\n",
      (unsigned)size,
      (unsigned)CACHE_WARN_BYTES
    );
  }

  return true;
}

bool appendLineToSdFile(const char* path, const String& line, const char* label) {
  if (!sdOk) {
    Serial.printf("[%s] SD unavailable, cannot write %s\n", label, path);
    return false;
  }

  if (line.length() == 0) {
    Serial.printf("[%s] Refusing to append empty line to %s\n", label, path);
    return false;
  }

  if (line.length() > CACHE_MAX_LINE_BYTES) {
    Serial.printf("[%s] Refusing to append oversized line (%u bytes) to %s\n", label, (unsigned)line.length(), path);
    return false;
  }

  File file = SD.open(path, FILE_APPEND);
  if (!file) {
    Serial.printf("[%s] Failed to open %s\n", label, path);
    return false;
  }

  size_t written = file.println(line);
  file.flush();
  size_t currentSize = file.size();
  file.close();

  if (written == 0) {
    Serial.printf("[%s] Write failed for %s\n", label, path);
    return false;
  }

  Serial.printf("[%s] Appended line to %s (%u bytes)\n", label, path, (unsigned)currentSize);
  return true;
}

bool appendBackupLine(const String& line) {
  if (!SD_KEEP_PERSISTENT_BACKUP) return true;

  bool ok = appendLineToSdFile(BACKUP_FILE, line, "BACKUP");
  if (!ok) return false;

  size_t currentSize = sdFileSize(BACKUP_FILE);
  if (currentSize >= BACKUP_WARN_SIZE_BYTES) {
    Serial.printf(
      "[BACKUP] Warning: %s is larger than %u bytes. Replace or offload SD soon.\n",
      BACKUP_FILE,
      (unsigned)BACKUP_WARN_SIZE_BYTES
    );
  }

  return true;
}

bool appendCacheLine(const String& line) {
  // The cache is only for failed live uploads. If it ever grows too large,
  // quarantine it and start a fresh retry queue. The persistent backup still
  // contains the complete measurement history for manual recovery.
  cacheFileLooksSane();
  return appendLineToSdFile(CACHE_FILE, line, "CACHE");
}

void configureC6Antenna() {
#ifdef CONFIG_IDF_TARGET_ESP32C6
  // The XIAO ESP32-C6 routes the radio through an on-board FM8625H RF switch
  // controlled by two internal-trace GPIOs:
  //   GPIO3  (RF_SWITCH_EN)  — drive LOW to ENABLE the switch (required for
  //                            either antenna).
  //   GPIO14 (RF_ANT_SELECT) — LOW = built-in ceramic, HIGH = external u.FL.
  // Selection is controlled by XIAO_C6_USE_EXTERNAL_ANTENNA in secrets.h
  // (default 0 = internal).
  pinMode(XIAO_C6_RF_SWITCH_EN_GPIO, OUTPUT);
  digitalWrite(XIAO_C6_RF_SWITCH_EN_GPIO, LOW);   // enable RF switch
  pinMode(XIAO_C6_ANTENNA_SELECT_GPIO, OUTPUT);
  digitalWrite(XIAO_C6_ANTENNA_SELECT_GPIO,
               XIAO_C6_USE_EXTERNAL_ANTENNA ? HIGH : LOW);
  logC6Antenna();
#endif
}

// Small helper: print which antenna the XIAO ESP32-C6 is currently using.
// Reads the driven level of the RF_ANT_SELECT pin (LOW = built-in ceramic,
// HIGH = external u.FL) so it reports the real switch state rather than just
// the compile-time preference. No-op on non-C6 targets. Safe to call any time
// after configureC6Antenna() (e.g. alongside network-status logging).
void logC6Antenna() {
#ifdef CONFIG_IDF_TARGET_ESP32C6
  bool external = digitalRead(XIAO_C6_ANTENNA_SELECT_GPIO) == HIGH;
  Serial.printf("[ANT] XIAO C6: %s antenna in use (EN GPIO%d=LOW, SEL GPIO%d=%s)\n",
      external ? "external u.FL" : "internal ceramic",
      (int)XIAO_C6_RF_SWITCH_EN_GPIO,
      (int)XIAO_C6_ANTENNA_SELECT_GPIO,
      external ? "HIGH" : "LOW");
#endif
}

String tarSafeName(String path) {
  path.trim();
  path.replace("\\", "/");
  while (path.startsWith("/")) path.remove(0, 1);
  if (path.length() == 0) path = "sd-root";
  return path;
}

void writeTarOctal(char* field, size_t fieldSize, uint64_t value) {
  // TAR numeric fields are ASCII octal, NUL-terminated.
  char fmt[12];
  snprintf(fmt, sizeof(fmt), "%%0%dllo", (int)fieldSize - 1);
  snprintf(field, fieldSize, fmt, (unsigned long long)value);
}

bool writeTarHeader(WiFiClient& client, const String& name, uint64_t size, bool directory) {
  String safeName = tarSafeName(name);
  if (safeName.length() > 99) {
    Serial.printf("[SD] Skipping TAR entry with too-long name: %s\n", safeName.c_str());
    return false;
  }

  uint8_t header[512];
  memset(header, 0, sizeof(header));

  strncpy((char*)header, safeName.c_str(), 100);
  writeTarOctal((char*)header + 100, 8, directory ? 0755 : 0644);
  writeTarOctal((char*)header + 108, 8, 0);
  writeTarOctal((char*)header + 116, 8, 0);
  writeTarOctal((char*)header + 124, 12, directory ? 0 : size);
  writeTarOctal((char*)header + 136, 12, 0);
  memset(header + 148, ' ', 8);
  header[156] = directory ? '5' : '0';
  memcpy(header + 257, "ustar", 5);
  memcpy(header + 263, "00", 2);

  unsigned int checksum = 0;
  for (size_t i = 0; i < sizeof(header); i++) checksum += header[i];
  snprintf((char*)header + 148, 8, "%06o", checksum);
  header[154] = '\0';
  header[155] = ' ';

  return client.write(header, sizeof(header)) == sizeof(header);
}

uint64_t paddedTarContentSize(uint64_t size) {
  return size + ((512 - (size % 512)) % 512);
}

uint64_t tarDirectorySize(File& dir, const String& prefix) {
  uint64_t total = 0;
  File entry = dir.openNextFile();
  while (entry) {
    String entryName = String(entry.name());
    int slash = entryName.lastIndexOf('/');
    if (slash >= 0) entryName = entryName.substring(slash + 1);

    String tarName = prefix.length() > 0 ? prefix + "/" + entryName : entryName;
    if (tarSafeName(tarName).length() <= 99) {
      if (entry.isDirectory()) {
        total += 512;
        total += tarDirectorySize(entry, tarName);
      } else {
        total += 512 + paddedTarContentSize(entry.size());
      }
    } else {
      Serial.printf("[SD] Skipping TAR size entry with too-long name: %s\n", tarName.c_str());
    }

    entry.close();
    entry = dir.openNextFile();
    delay(0);
  }
  return total;
}

void streamTarFile(WiFiClient& client, File& file, const String& name) {
  uint64_t size = file.size();
  if (!writeTarHeader(client, name, size, false)) return;

  uint8_t buf[1024];
  uint64_t remaining = size;
  while (remaining > 0 && file.available() && client.connected()) {
    size_t toRead = remaining > sizeof(buf) ? sizeof(buf) : (size_t)remaining;
    size_t n = file.read(buf, toRead);
    if (n == 0) break;
    client.write(buf, n);
    remaining -= n;
    delay(0);
  }

  size_t pad = (512 - (size % 512)) % 512;
  if (pad > 0) {
    uint8_t zeros[512];
    memset(zeros, 0, sizeof(zeros));
    client.write(zeros, pad);
  }
}

void streamTarDirectory(WiFiClient& client, File& dir, const String& prefix) {
  File entry = dir.openNextFile();
  while (entry && client.connected()) {
    String entryName = String(entry.name());
    int slash = entryName.lastIndexOf('/');
    if (slash >= 0) entryName = entryName.substring(slash + 1);

    String tarName = prefix.length() > 0 ? prefix + "/" + entryName : entryName;

    if (entry.isDirectory()) {
      writeTarHeader(client, tarName + "/", 0, true);
      streamTarDirectory(client, entry, tarName);
    } else {
      streamTarFile(client, entry, tarName);
    }

    entry.close();
    entry = dir.openNextFile();
    delay(0);
  }
}
