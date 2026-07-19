// main.cpp — top-level orchestration. Wiring of the individual modules into
// the boot sequence, the single wake/measure/upload cycle and awake-mode loop.
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

  // WiFi is required for upload and must be associated before JSON assembly so
  // rssi_dbm reflects the live connection.
  connectNetwork();

  String json = createMeasurementJson();

  // SD.begin() is the reproducible boundary after which ESP32-C6 I2C-NG may
  // reject later transfers. Capture the timestamp before bringing SPI/SD up.
  initSdCard();

  if (sdOk) appendBackupLine(json);

  bool serverConfirmedClaim = false;
  bool currentUploaded = uploadLine(json, &serverConfirmedClaim);
  if (serverConfirmedClaim) markClaimRegistered();

  if (!currentUploaded) {
    if (sdOk) {
      Serial.println("[CYCLE] Live upload failed; adding measurement to retry cache");
      appendCacheLine(json);
    } else {
      Serial.println("[CYCLE] Live upload failed and no SD card is available; measurement not cached");
    }
  } else if (sdOk) {
    uploadCachedLines();
  }

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

  i2cbus::logDiag();
  scalebus::logDiag();

  Serial.println("[CYCLE] Done");
  debugLine();
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  uint32_t wakeReason = esp_sleep_get_wakeup_causes();
  bool wokeFromDeepSleep = (wakeReason & BIT(ESP_SLEEP_WAKEUP_TIMER))
#ifdef CONFIG_IDF_TARGET_ESP32C6
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_GPIO))
#else
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT0))
      || (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT1))
#endif
      ;

  releaseSleepPinHolds();
  configureC6Antenna();
  pinMode(SETUP_BUTTON_PIN, INPUT_PULLUP);
  statusLedInit();
  statusLedBootBlink();

  rtcBootCount++;

  debugLine();
  Serial.println("Hive Scale ESP32 firmware with provisioning + OTA");
  Serial.printf("Firmware version: %s\n", FIRMWARE_VERSION);
  Serial.printf("Optional modules: INA219=%d MAX17048=%d INMP441=%d DS18B20=%d BleScan=%d\n",
                ENABLE_INA219_SOLAR, ENABLE_MAX17048_BATTERY, ENABLE_INMP441_MICS,
                ENABLE_DS18B20_HIVE_TEMP, ENABLE_BLE_SCAN);
  Serial.printf("Wake reason: %s; RTC boot count: %u\n",
                wakeReasonName(wakeReason).c_str(), rtcBootCount);
  debugLine();

  seedPrefsFromSecretsIfNeeded();
  loadConfigFromPrefs();
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
    Serial.println("[SETUP] I2C bus initialization FAILED; skipping all I2C device initialization");
  }

  rtcOk = i2cOk && rtc.begin();
  Serial.printf("[RTC] %s\n", rtcOk ? "OK" : "MISSING");

  // A DS3231 with OSF/lostPower set does not hold trustworthy time. Treat it as
  // unusable for this boot so initializeTime()/timestampNow() go straight to the
  // system/NTP clock and never format garbage register bytes as a timestamp.
  if (rtcOk && rtc.lostPower()) {
    Serial.println("[RTC] Lost power; disabling RTC time for this boot");
    rtcOk = false;
  }

  shtOk = i2cOk && sht4.begin();
  Serial.printf("[SHT4x] %s\n", shtOk ? "OK" : "MISSING");
  if (shtOk) {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
    // Let the sensor settle after configuration before the first checked
    // measurement (the high-precision command times out otherwise on a cold
    // cross-radio start). Small and bounded — see sht4x_measure.h.
    delay(20);
  }

#if ENABLE_INA219_SOLAR
  solarMonitorOk = i2cOk && i2cbus::deviceResponds(INA219_I2C_ADDRESS) &&
                   solarMonitor.begin(&Wire);
  Serial.printf("[INA219] %s\n", solarMonitorOk ? "OK" : "MISSING");
  if (solarMonitorOk) {
    solarMonitor.setCalibration_32V_2A();
    solarMonitor.powerSave(true);
  }
#endif

#if ENABLE_MAX17048_BATTERY
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

  // Wired I2C acquisition, all before radio and before SD/SPI, in three ordered
  // phases: (1) device-level ambient sensors (SHT4x/INA219/MAX17048) on the
  // known-good bus, BEFORE any optional/absent-device probe; (2) scale-bus
  // init, which probes the optional TCA9548A; (3) wired scale reads. Keeping the
  // ambient SHT4x read ahead of the mux probe stops an absent TCA9548A from
  // wedging the ESP32-C6 I2C-NG driver before the ambient measurement is taken.
  prefetchAmbientSensors();
  scalebus::begin();
  prefetchWiredScales();

  // The hardware log shows SD.begin() is the boundary after which the C6 I2C-NG
  // driver starts returning ESP_ERR_INVALID_STATE. Resolve time first, and do not
  // make any required RTC transaction after this point.
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
    // Awake/calibration mode: scalebus::begin() already ran once in setup() and
    // its chip state persists, so re-acquire both wired phases (ambient then
    // scales) without re-initializing the scale bus. Each cycle resets the
    // snapshot, so a failed read reports null/sht_ok:false rather than a stale
    // value even though WiFi is already up.
    prefetchAmbientSensors();
    prefetchWiredScales();
    runUploadCycle();
  }

  unsigned long activeCommandIntervalMs = calibrationModeActive
      ? calibrationModeIntervalMs : COMMAND_CHECK_INTERVAL_MS;

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
