// storage_power.h — SD storage (cache, backup, TAR export) together with
// power management (scale power, deep sleep, wake configuration). These two
// concerns are grouped because the deep-sleep path has to flush and release
// the SD bus before sleeping.
#pragma once

#include <Arduino.h>
#include <SD.h>
#include <WiFiClient.h>
#include <esp_sleep.h>

// ---- Power / sleep --------------------------------------------------------
String wakeReasonName(uint32_t wakeCauses);
void releaseSleepPinHolds();
uint32_t cyclesForInterval(unsigned long intervalMs);
bool shouldCheckOtaThisCycle();
void markOtaChecked();
bool rtcHasValidTime();
void powerUpScales();
void powerDownScalesForSleep();
void shutdownWifiAndBt();
void configureButtonWake();
void preparePowerMonitorsForSleep();
void enterDeepSleep(unsigned long sleepMs);
void enterDeepSleepUntilNextCycle(unsigned long intervalMs);

// ---- SD card lifecycle ----------------------------------------------------
bool initSdCard();
void prepareSdForSleep();
size_t sdFileSize(const char* path);

// ---- Last-measurement helpers --------------------------------------------
String readLastNonEmptySdLine(const char* path);
void rememberLastMeasurement(const String& line);
void ensureLastMeasurementLoaded();

// ---- Cache / backup -------------------------------------------------------
bool quarantineSdFile(const char* path, const char* quarantinePath, const char* label);
bool cacheFileLooksSane();
bool appendLineToSdFile(const char* path, const String& line, const char* label);
bool appendBackupLine(const String& line);
bool appendCacheLine(const String& line);

// ---- TAR export (used by the setup portal) -------------------------------
String tarSafeName(String path);
void writeTarOctal(char* field, size_t fieldSize, uint64_t value);
bool writeTarHeader(WiFiClient& client, const String& name, uint64_t size, bool directory);
uint64_t paddedTarContentSize(uint64_t size);
uint64_t tarDirectorySize(File& dir, const String& prefix);
void streamTarFile(WiFiClient& client, File& file, const String& name);
void streamTarDirectory(WiFiClient& client, File& dir, const String& prefix);
