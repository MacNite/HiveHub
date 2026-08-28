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
#include "heap_diag.h"
#include "inspection.h"

#if ENABLE_INMP441_MICS
#include "mics.h"
#endif

void runUploadCycle() {
  debugLine();
  Serial.println("[CYCLE] Starting measurement/upload cycle");

  // Heap probes bracket the stages that bring a whole subsystem up and tear it
  // down again — WiFi/TLS, the BLE scan, SPI/SD. A hub panicked in the
  // allocator on a corrupted free block during a firmware relay, and the block
  // that faulted was damaged well before the relay touched it; whichever stage
  // reports the first failure below is the one that did it. See heap_diag.h.
  heapdiag::probe("cycle-start");

  // Before anything is measured: an inspection nobody switched off ends here,
  // so this cycle's readings already count again rather than being flagged for
  // one more interval.
  inspection::enforceTimeout();

  // WiFi is required for upload and must be associated before JSON assembly so
  // rssi_dbm reflects the live connection.
  connectNetwork();
  heapdiag::probe("after-network");

  JsonDocument doc;
  // Assembly runs the BLE scan, which inits and deinits the NimBLE stack. That
  // teardown is deliberately incomplete (deinit(false) — deinit(true) panics on
  // the C6 once a scan has run this boot, see ble_sensor.cpp), so it is the
  // stage most worth watching.
  buildMeasurementDoc(doc);
  heapdiag::probe("after-measure");

  // SD.begin() is the reproducible boundary after which ESP32-C6 I2C-NG may
  // reject later transfers. Capture the timestamp before bringing SPI/SD up.
  initSdCard();
  heapdiag::probe("after-sd");

  // Stamp sd_ok only now. sdOk is a plain global that starts false on every boot
  // and prepareSdForSleep() clears it before each deep sleep, so it is always
  // false while the document above is assembled — stamping it there reported an
  // SD card fault on every cycle even though the card mounted and the backup
  // line was written moments later.
  doc["sd_ok"] = sdOk;

  String json = finalizeMeasurementJson(doc);

  if (sdOk) appendBackupLine(json);

  // Track the claim in both directions: latch it once the server confirms the
  // device is claimed, and un-latch it when the server explicitly says it is
  // not. The second half is what makes "remove the device in the app" a
  // recoverable action — the device notices the pairing is gone and starts
  // offering its claim code again, so it can simply be re-claimed. A server
  // that reports nothing (older build, or a failed upload) changes neither.
  ClaimStatus claimStatus = ClaimStatus::Unknown;
  bool currentUploaded = uploadLine(json, &claimStatus);
  if (claimStatus == ClaimStatus::Claimed) markClaimRegistered();
  else if (claimStatus == ClaimStatus::Unclaimed) clearClaimRegistered();

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

  heapdiag::probe("after-upload");

  fetchRemoteConfig();
  // checkCommands() is where a firmware relay runs, so this is the last clean
  // reading before the stage that crashed in the field.
  heapdiag::probe("before-commands");
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
  heapdiag::probe("cycle-end");

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

  // Which pin woke us, on the boards where more than one can. The setup button
  // and the inspection button mean completely different things, and by the time
  // this runs a press is often already released, so the wake status — not a
  // digitalRead — is what tells them apart. Read BEFORE the pin holds are
  // released, so nothing downstream can disturb the latched status.
  uint64_t gpioWakeMask = 0;
#ifdef CONFIG_IDF_TARGET_ESP32C6
  const bool gpioWake = (wakeReason & BIT(ESP_SLEEP_WAKEUP_GPIO)) != 0;
  if (gpioWake) gpioWakeMask = esp_sleep_get_gpio_wakeup_status();
#endif

  releaseSleepPinHolds();
  configureC6Antenna();
  pinMode(SETUP_BUTTON_PIN, INPUT_PULLUP);
#ifdef CONFIG_IDF_TARGET_ESP32C6
  // Fallback for the case where the status register comes back empty on a wake
  // we know was a GPIO wake: read the pins instead. A firm press is often still
  // held this early in the boot, and an empty mask would otherwise mean neither
  // button did anything — the button "working sometimes" is a far worse failure
  // than acting on a press that has already been released.
#if HAS_INSPECTION_BUTTON
  pinMode(INSPECTION_BUTTON_PIN, INPUT_PULLUP);
#endif
  if (gpioWake && gpioWakeMask == 0) {
    if (digitalRead(SETUP_BUTTON_PIN) == LOW) gpioWakeMask |= 1ULL << SETUP_BUTTON_PIN;
#if HAS_INSPECTION_BUTTON
    if (digitalRead(INSPECTION_BUTTON_PIN) == LOW) gpioWakeMask |= 1ULL << INSPECTION_BUTTON_PIN;
#endif
    Serial.printf("[SETUP] Empty GPIO wake status; inferred mask 0x%llX from pin levels\n",
                  (unsigned long long)gpioWakeMask);
  }
#endif
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
  // The wake reason above answers "which sleep did we come out of"; this answers
  // "did the last boot end cleanly". They are different questions, and a boot
  // that follows a panic looks identical to a normal one without this line.
  Serial.printf("Reset reason: %s\n", resetReasonName());
  heapdiag::logDiag("boot");
  debugLine();

  seedPrefsFromSecretsIfNeeded();
  loadConfigFromPrefs();
  hivecfg::loadHiveConfig();

  // Before the first measurement is assembled: a press on the inspection button
  // must be reflected in the cycle it woke, not one cycle later.
  inspection::begin(wakeReason, gpioWakeMask);

  // Setup button — provisioning AP / factory reset. On the C6 both buttons wake
  // through the same GPIO cause, so the mask decides; a wake on the inspection
  // button must NOT open the portal, or every inspection would strand the hub
  // in AP mode for the portal timeout.
  const bool setupWake =
#ifdef CONFIG_IDF_TARGET_ESP32C6
      (gpioWakeMask & (1ULL << SETUP_BUTTON_PIN)) != 0;
#else
      (wakeReason & BIT(ESP_SLEEP_WAKEUP_EXT0)) != 0;
#endif

  if (digitalRead(SETUP_BUTTON_PIN) == LOW || setupWake) {
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

  // Ambient temp/humidity[/pressure] sensor. Exactly one family is compiled in
  // (see config.h); shtOk / the sht_ok payload field stay the generic "ambient
  // sensor detected/read OK" signal regardless of which part is fitted, so the
  // backend schema is unchanged.
#if ENABLE_SHT4X_AMBIENT
  shtOk = i2cOk && sht4.begin();
  Serial.printf("[AMBIENT] SHT4x %s\n", shtOk ? "OK" : "MISSING");
  if (shtOk) {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
  }
#elif ENABLE_SHT3X_AMBIENT
  shtOk = i2cOk && sht3.begin(SHT3X_I2C_ADDRESS);
  Serial.printf("[AMBIENT] SHT3x %s\n", shtOk ? "OK" : "MISSING");
  if (shtOk) {
    sht3.heater(false);  // keep the on-chip heater off (matches SHT4x_NO_HEATER)
  }
#elif ENABLE_BME280_AMBIENT
  shtOk = i2cOk && bme.begin(BME280_I2C_ADDRESS, &Wire);
  Serial.printf("[AMBIENT] BME280 %s\n", shtOk ? "OK" : "MISSING");
  if (shtOk) {
    // Weather-station / low-power profile: 1x oversampling, forced mode, filter
    // off. Forced mode takes one measurement per read then sleeps — ideal for the
    // once-per-cycle deep-sleep duty cycle (no continuous conversion current).
    bme.setSampling(Adafruit_BME280::MODE_FORCED,
                    Adafruit_BME280::SAMPLING_X1,   // temperature
                    Adafruit_BME280::SAMPLING_X1,   // pressure
                    Adafruit_BME280::SAMPLING_X1,   // humidity
                    Adafruit_BME280::FILTER_OFF);
  }
#else
  shtOk = false;
  Serial.println("[AMBIENT] No ambient sensor compiled (all families disabled)");
#endif

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

  if (provisioningActive) {
    Serial.println("[SETUP] Provisioning active; staying awake until portal timeout");
    return;
  }

  enterDeepSleepUntilNextCycle(sendIntervalMs);
}

void loop() {
  handleButton();
  inspection::poll();

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

  // Commands are checked once per upload cycle, inside runUploadCycle() above —
  // there is no separate timer. The old 5-minute poll here was unreachable in the
  // default configuration anyway (with DEEP_SLEEP_ENABLED, loop() sleeps and
  // returns before this point), so it only ever ran on an always-awake build and
  // made the real cadence hard to reason about: the documented "up to 5 minutes"
  // was never true for a sleeping device. Every mode now shares one rule — a
  // command is picked up on the next cycle, at the configured send interval.

  if (now - lastOtaCheckMs >= OTA_CHECK_INTERVAL_MS) {
    lastOtaCheckMs = now;
    checkForOtaUpdate();
  }

  delay(1000);
}
