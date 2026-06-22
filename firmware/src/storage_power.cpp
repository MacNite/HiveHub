// storage_power.cpp — SD storage and power/sleep implementation.
#include "storage_power.h"
#include "globals.h"
#include "config.h"

#include <SPI.h>
#include <WiFi.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>

#if ENABLE_INMP441_MICS
#include "mics.h"
#endif

String wakeReasonName(uint32_t wakeCauses) {
  // esp_sleep_get_wakeup_causes() returns a uint32_t bitmask; each cause is
  // checked with BIT(enum_value) because the ESP_SLEEP_WAKEUP_* values are
  // sequential indices, not powers of two.
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_TIMER))    return "timer";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_EXT0))     return "button/ext0";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_EXT1))     return "ext1";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_TOUCHPAD)) return "touchpad";
  if (wakeCauses & BIT(ESP_SLEEP_WAKEUP_ULP))      return "ulp";
  return "power-on/reset";
}

void releaseSleepPinHolds() {
  gpio_deep_sleep_hold_dis();
  gpio_hold_dis((gpio_num_t)HX1_SCK);
  gpio_hold_dis((gpio_num_t)HX2_SCK);
  gpio_hold_dis((gpio_num_t)SD_CS);

  // EXT0 wake config turns the button into an RTC IO. Return it to normal GPIO.
  rtc_gpio_deinit((gpio_num_t)SETUP_BUTTON_PIN);
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
}

void powerDownScalesForSleep() {
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

  // GPIO27 is an RTC-capable pin on ESP32. The RTC pull-up lets the existing
  // button-to-GND wiring wake the device without an external pull-up. For the
  // lowest possible sleep current, use an external pull-up and remove this.
  rtc_gpio_init((gpio_num_t)SETUP_BUTTON_PIN);
  rtc_gpio_set_direction((gpio_num_t)SETUP_BUTTON_PIN, RTC_GPIO_MODE_INPUT_ONLY);
  rtc_gpio_pullup_en((gpio_num_t)SETUP_BUTTON_PIN);
  rtc_gpio_pulldown_dis((gpio_num_t)SETUP_BUTTON_PIN);
  esp_sleep_enable_ext0_wakeup((gpio_num_t)SETUP_BUTTON_PIN, 0);
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

  powerDownScalesForSleep();
  preparePowerMonitorsForSleep();
#if ENABLE_INMP441_MICS
  shutdownMicsI2s();
#endif
  prepareSdForSleep();
  shutdownWifiAndBt();

  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  esp_sleep_enable_timer_wakeup((uint64_t)sleepMs * US_PER_MS);
  configureButtonWake();

  gpio_deep_sleep_hold_en();

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

  if (size > CACHE_MAX_BYTES) {
    Serial.printf(
      "[CACHE] Cache file is too large (%u bytes > %u bytes). Quarantining it. Backup file remains available.\n",
      (unsigned)size,
      (unsigned)CACHE_MAX_BYTES
    );
    quarantineSdFile(CACHE_FILE, CACHE_BAD_FILE, "CACHE");
    return false;
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
