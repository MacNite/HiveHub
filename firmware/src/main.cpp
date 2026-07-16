// main.cpp — top-level orchestration. Wiring of the individual modules into
// the boot sequence (setup), the single wake/measure/upload cycle
// (runUploadCycle) and the awake-mode loop.
#include <Arduino.h>

#include "config.h"
#include "globals.h"
#include "device_prefs.h"
#include "storage_power.h"
#include "hivehub_network.h"
#include "sensors.h"
#include "portal.h"
#include "hive_config.h"
#include "scale_bus.h"
#include "i2c_bus.h"
#include "status_led.h"

#if ENABLE_INMP441_MICS
#include "mics.h"
#endif

void runUploadCycle() {
  debugLine();
  Serial.println("[CYCLE] Starting measurement/upload cycle");

  // Bring the network up before assembling the payload so rssi_dbm is sampled
  // while Wi-Fi is associated. Otherwise createMeasurementJson() reads
  // WiFi.RSSI() before connectWifi() has run (it only runs later inside
  // uploadLine()), so the ternary in sensors.cpp falls back to 0. On timer
  // deep-sleep wakes with a valid RTC, initializeTime() skips its NTP sync and
  // never connects either, which is why boards with a healthy RTC (e.g. the
  // ESP32-C6 scale modules) reported rssi_dbm = 0 on every cycle. Wi-Fi has to
  // come up for the upload regardless, so connecting here costs no extra power.
  connectNetwork();

  String json = createMeasurementJson();

  if (sdOk) {
    // Keep a durable local copy first. This file is never deleted by uploads,
    // so it works as a long-term backup and as an offline data log.
    appendBackupLine(json);
  }

  // Important: always try to upload the current measurement directly first.
  // The retry cache is only for failed live uploads. The previous firmware
  // added every row to the cache and then depended on cache replay, which could
  // stop all uploads if the cache file or FAT metadata became corrupted.
  bool serverConfirmedClaim = false;
  bool currentUploaded = uploadLine(json, &serverConfirmedClaim);

  // Only stop sending the claim code once the server confirms the claim. This
  // keeps the device claimable after a backend rebuild/restore: an unclaimed
  // (or newly reset) server reports claimed=false, so the device keeps sending
  // its claim code until the beekeeper claims it in HivePal.
  if (serverConfirmedClaim) markClaimRegistered();

  if (!currentUploaded) {
    if (sdOk) {
      Serial.println("[CYCLE] Live upload failed; adding measurement to retry cache");
      appendCacheLine(json);
    } else {
      Serial.println("[CYCLE] Live upload failed and no SD card is available; measurement not cached");
    }
  } else if (sdOk) {
    // Now that the network/backend is known to work, retry a small bounded
    // number of older cached rows. This prevents a large cache from blocking
    // the fresh measurement or keeping the device awake for too long.
    uploadCachedLines();
  }

  // A tare/span done offline on the provisioning portal only lives on the device
  // (the AP has no internet, so the server never learned it). Now that WiFi is up
  // again after the reboot, push it to the backend BEFORE fetching remote config,
  // so the server holds the real calibration instead of its defaults. Runs only
  // when a report is pending and clears the flag on success.
  if (scaleCalibrationReportPending()) reportScaleCalibration();

  fetchRemoteConfig();
  checkCommands();

  if (shouldCheckOtaThisCycle()) {
    lastOtaCheckMs = millis();
    markOtaChecked();
    checkForOtaUpdate();
  } else {
    Serial.printf("[OTA] Skipping; next scheduled check in %u cycle(s)\n", rtcCyclesUntilOta);
  }

  // One-line I2C/scale health summaries so a field log shows recovery attempts,
  // NACK/timeout counts and mux/NAU failures without extra chatter.
  i2cbus::logDiag();
  scalebus::logDiag();

  Serial.println("[CYCLE] Done");
  debugLine();
}

// I2C bring-up (stuck-bus recovery + checked Wire.begin at the explicit
// 100 kHz I2C_CLOCK_HZ) lives in i2c_bus.cpp. When it fails, i2cbus::ok()
// stays false and every I2C consumer (RTC, SHT4x, scalebus, power monitors)
// is skipped — readings fail closed as missing/invalid instead of the bus
// wedging every driver in turn.

void setup() {
  Serial.begin(115200);
  delay(1000);

  uint32_t wakeReason = esp_sleep_get_wakeup_causes();
  bool wokeFromDeepSleep = (wakeReason & BIT(ESP_SLEEP_WAKEUP_TIMER))
#ifdef CONFIG_IDF_TARGET_ESP32C6
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_GPIO))   // C6 GPIO deep-sleep wake
#else
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT0))   // classic ESP32 RTC-GPIO wake
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT1))
#endif
      ;

  releaseSleepPinHolds();
  configureC6Antenna();
  pinMode(SETUP_BUTTON_PIN, INPUT_PULLUP);

  // Heartbeat: a short blink of the on-board user LED as soon as we wake, so a
  // powered board visibly confirms it booted. The matching double blink before
  // deep sleep lives in enterDeepSleep(). No-op when ENABLE_STATUS_LED is 0.
  statusLedInit();
  statusLedBootBlink();

  rtcBootCount++;

  debugLine();
  Serial.println("Hive Scale ESP32 firmware with provisioning + OTA");
  Serial.printf("Firmware version: %s\n", FIRMWARE_VERSION);
  Serial.printf("Optional modules: INA219=%d MAX17048=%d INMP441=%d DS18B20=%d BleScan=%d\n",
                ENABLE_INA219_SOLAR, ENABLE_MAX17048_BATTERY, ENABLE_INMP441_MICS,
                ENABLE_DS18B20_HIVE_TEMP, ENABLE_BLE_SCAN);
  Serial.printf("Wake reason: %s; RTC boot count: %u\n", wakeReasonName(wakeReason).c_str(), rtcBootCount);
  debugLine();

  seedPrefsFromSecretsIfNeeded();
  loadConfigFromPrefs();
  // Parse the dynamic hive registry (migrating the legacy 2-slot config on the
  // first boot after upgrade). Must run before the provisioning portal and the
  // measurement cycle, both of which read gHives.
  hivecfg::loadHiveConfig();

  if (digitalRead(SETUP_BUTTON_PIN) == LOW
#ifdef CONFIG_IDF_TARGET_ESP32C6
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_GPIO))
#else
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT0))
#endif
  ) {
    Serial.println("[SETUP] Button wake/press detected; starting provisioning portal");
    initSdCard();
    // Bring up I2C + the scale bus + the 1-Wire bus so the portal's "Scan I2C
    // scales" and DS18B20 mapping can enumerate what is physically attached.
    // A failed bus bring-up is not fatal here: the portal still serves the
    // config pages; scans just come back empty (and log why).
    if (!i2cbus::begin()) {
      Serial.println("[SETUP] I2C bus unusable; portal scans will show no I2C devices");
    }
    scalebus::begin();
#if ENABLE_DS18B20_HIVE_TEMP
    ds18b20.begin();
#endif
    startProvisioningPortal();
    return;
  }

  const bool i2cOk = i2cbus::begin();
  if (!i2cOk) {
    // Fail closed: no I2C device init on a stuck bus. The cycle still runs so
    // the device can report its state (sd_ok/rtc_ok/sht_ok all false, scales
    // invalid) instead of going silent.
    Serial.println("[SETUP] I2C bus initialization FAILED; skipping all I2C device initialization");
  }

  rtcOk = i2cOk && rtc.begin();
  Serial.printf("[RTC] %s\n", rtcOk ? "OK" : "MISSING");

  if (rtcOk && rtc.lostPower()) {
    Serial.println("[RTC] Lost power");
  }

  shtOk = i2cOk && sht4.begin();
  Serial.printf("[SHT4x] %s\n", shtOk ? "OK" : "MISSING");

  if (shtOk) {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
  }

#if ENABLE_INA219_SOLAR
  // Address-probe first: Adafruit_INA219::begin() reads the config register, and
  // a register read to an ABSENT chip wedges the C6 I2C-NG driver. A clean
  // address NACK reports "MISSING" without ever issuing that transaction.
  solarMonitorOk = i2cOk && i2cbus::deviceResponds(INA219_I2C_ADDRESS) &&
                   solarMonitor.begin(&Wire);
  Serial.printf("[INA219] %s\n", solarMonitorOk ? "OK" : "MISSING");
  if (solarMonitorOk) {
    solarMonitor.setCalibration_32V_2A();
    solarMonitor.powerSave(true);
  }
#endif

#if ENABLE_MAX17048_BATTERY
  // Address-probe first: SFE_MAX1704X::begin()/isConnected() reads the VERSION
  // register, and that register read to an ABSENT gauge is exactly what leaves
  // the C6 I2C-NG master in ESP_ERR_INVALID_STATE (a wedge that then fails every
  // later transfer, e.g. the RTC and SHT4x reads). Gate it behind a clean
  // address NACK so a missing gauge is reported without wedging the bus.
  batteryMonitorOk = i2cOk && i2cbus::deviceResponds(MAX17048_I2C_ADDRESS) &&
                     batteryGauge.begin();
  Serial.printf("[MAX17048] %s\n", batteryMonitorOk ? "OK" : "MISSING");
  if (batteryMonitorOk) {
    batteryGauge.quickStart();
    batteryGauge.setThreshold(MAX17048_ALERT_PERCENT);
  }
#endif

#if ENABLE_DS18B20_HIVE_TEMP
  ds18b20.begin();
  Serial.printf("[DS18B20] Device count: %d\n", ds18b20.getDeviceCount());
#else
  Serial.println("[DS18B20] Disabled (ENABLE_DS18B20_HIVE_TEMP=0); hive temp from BLE sensor if paired");
#endif

#if ENABLE_HX711
  scale1.begin(HX1_DOUT, HX1_SCK);
  scale2.begin(HX2_DOUT, HX2_SCK);
  Serial.println("[HX711] Initialized");
#endif

  // Detect the TCA9548A mux and configure every NAU7802 in the registry. Needs
  // Wire (started above) and the loaded registry; safe when no I2C scales exist.
  scalebus::begin();

  initSdCard();

  initializeTime(wokeFromDeepSleep);

  Serial.println("[SETUP] Running upload cycle now");
  runUploadCycle();

  lastCycleMs = millis();
  lastOtaCheckMs = millis();
  lastCommandCheckMs = millis();

  if (provisioningActive) {
    Serial.println("[SETUP] Provisioning active; staying awake until portal timeout");
    return;
  }

  // Anchor the next wake to THIS boot (not to the end of this variable-length
  // cycle) so the in-hive BLE rendezvous scan recurs on a stable cadence and
  // stays aligned with the wake-sync schedule we just handed HiveInside.
  enterDeepSleepUntilNextCycle(sendIntervalMs);
}

void loop() {
  handleButton();

  if (provisioningActive) {
    setupDnsServer.processNextRequest();
    setupServer.handleClient();
    if (millis() - provisioningStartedMs > PROVISIONING_TIMEOUT_MS) {
      stopProvisioningPortal();
      enterDeepSleep(sendIntervalMs);
    }
    delay(10);
    return;
  }

  unsigned long now = millis();

  if (calibrationModeExpired()) {
    stopCalibrationMode("timeout reached");
    enterDeepSleep(sendIntervalMs);
    return;
  }

  unsigned long activeIntervalMs = calibrationModeActive ? calibrationModeIntervalMs : sendIntervalMs;

  if (DEEP_SLEEP_ENABLED && !calibrationModeActive) {
    enterDeepSleep(sendIntervalMs);
    return;
  }

  if (now - lastCycleMs >= activeIntervalMs) {
    lastCycleMs = now;
    runUploadCycle();
  }

  unsigned long activeCommandIntervalMs = calibrationModeActive ? calibrationModeIntervalMs : COMMAND_CHECK_INTERVAL_MS;

  if (now - lastCommandCheckMs >= activeCommandIntervalMs) {
    lastCommandCheckMs = now;
    checkCommands();
  }

  if (now - lastOtaCheckMs >= OTA_CHECK_INTERVAL_MS) {
    lastOtaCheckMs = now;
    checkForOtaUpdate();
  }

  delay(1000);
}
