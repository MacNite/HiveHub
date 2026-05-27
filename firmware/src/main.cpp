#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Update.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <HX711.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_SHT4x.h>
#include <RTClib.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>
#include <esp_sleep.h>
#include <driver/gpio.h>
#include <driver/rtc_io.h>

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
// INMP441 STEREO MICS (defaults)
// ==============================
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

#if ENABLE_INA219_SOLAR
#include <Adafruit_INA219.h>
#endif

#if ENABLE_MAX17048_BATTERY
#include <SparkFun_MAX1704x_Fuel_Gauge_Arduino_Library.h>
#endif

#if ENABLE_INMP441_MICS
#include <driver/i2s.h>
#endif

static const char* FIRMWARE_VERSION = "0.7.1-mic-fft";

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

HX711 scale1;
HX711 scale2;
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature ds18b20(&oneWire);
Adafruit_SHT4x sht4;
RTC_DS3231 rtc;
Preferences prefs;
WebServer setupServer(80);
DNSServer setupDnsServer;

#if ENABLE_INA219_SOLAR
Adafruit_INA219 solarMonitor(INA219_I2C_ADDRESS);
bool solarMonitorOk = false;
#endif

#if ENABLE_MAX17048_BATTERY
SFE_MAX1704X batteryGauge(MAX1704X_MAX17048);
bool batteryMonitorOk = false;
#endif

#if ENABLE_INMP441_MICS
#include <arduinoFFT.h>
 
// Number of samples fed into the FFT.  Must be a power of two and fit in RAM.
// 4096 * sizeof(double) * 4 arrays (vReal/vImag x2 channels) = 128 kB, which
// is comfortably within the ESP32's 320 kB heap.  At 16 kHz this gives a
// frequency resolution of 16000 / 4096 ≈ 3.9 Hz — more than enough to resolve
// the narrow piping band (300–550 Hz).
#define FFT_SAMPLE_COUNT 4096
 
struct MicBands {
  float sub_bass_dbfs = NAN;  //   50 – 150 Hz  structural / low rumble
  float hum_dbfs      = NAN;  //  150 – 300 Hz  normal colony hum
  float piping_dbfs   = NAN;  //  300 – 550 Hz  queen piping / tooting (pre-swarm)
  float stress_dbfs   = NAN;  //  550 – 1500 Hz agitated / robbing colony
  float high_dbfs     = NAN;  // 1500 – 3000 Hz  harmonic overtones
};
 
struct MicChannelStats {
  bool ok = false;
  float rmsDbfs        = NAN;  // Broadband RMS in dBFS
  float peakDbfs       = NAN;  // Peak in dBFS
  float rmsNormalized  = NAN;  // Linear RMS fraction of full scale (0..1)
  uint32_t sampleCount = 0;
  MicBands bands;              // Per-band energy in dBFS
};
 
struct MicMeasurement {
  bool ok = false;
  MicChannelStats left;
  MicChannelStats right;
};
#endif // struct definitions guard


bool sdOk = false;
bool sdBusInitialized = false;
bool shtOk = false;
bool rtcOk = false;
bool provisioningActive = false;
bool calibrationModeActive = false;

unsigned long lastCycleMs = 0;
unsigned long lastOtaCheckMs = 0;
unsigned long lastCommandCheckMs = 0;
unsigned long provisioningStartedMs = 0;
unsigned long sendIntervalMs = 10UL * 60UL * 1000UL;
unsigned long calibrationModeStartedMs = 0;
unsigned long calibrationModeIntervalMs = CALIBRATION_MODE_DEFAULT_INTERVAL_MS;
unsigned long calibrationModeTimeoutMs = CALIBRATION_MODE_DEFAULT_TIMEOUT_MS;

String timeSource = "unknown";
String apiBaseUrl;
String apiKey;
String deviceId;
String claimCode;
String activeWifiSsid;
String lastMeasurementJson;
unsigned long lastMeasurementUpdatedMs = 0;

long scale1Offset = 0;
long scale2Offset = 0;
float scale1Factor = -7050.0f;
float scale2Factor = -7050.0f;

bool buttonWasDown = false;
unsigned long buttonDownMs = 0;
bool longPressHandled = false;

RTC_DATA_ATTR uint32_t rtcCyclesUntilOta = 0;
RTC_DATA_ATTR uint32_t rtcBootCount = 0;

String timestampNow();
void syncTime();
bool connectNetwork();
void preparePowerMonitorsForSleep();
#if ENABLE_INMP441_MICS
bool initMicsI2s();
void shutdownMicsI2s();
#endif

void debugLine() {
  Serial.println("----------------------------------------");
}

String wakeReasonName(esp_sleep_wakeup_cause_t reason) {
  switch (reason) {
    case ESP_SLEEP_WAKEUP_TIMER: return "timer";
    case ESP_SLEEP_WAKEUP_EXT0: return "button/ext0";
    case ESP_SLEEP_WAKEUP_EXT1: return "ext1";
    case ESP_SLEEP_WAKEUP_TOUCHPAD: return "touchpad";
    case ESP_SLEEP_WAKEUP_ULP: return "ulp";
    default: return "power-on/reset";
  }
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


bool calibrationModeExpired() {
  if (!calibrationModeActive) return false;
  return millis() - calibrationModeStartedMs >= calibrationModeTimeoutMs;
}

void stopCalibrationMode(const String& reason) {
  if (!calibrationModeActive) return;
  calibrationModeActive = false;
  Serial.print("[CAL] Calibration mode stopped");
  if (reason.length() > 0) {
    Serial.print(": ");
    Serial.print(reason);
  }
  Serial.println();
}

void startCalibrationMode(unsigned long intervalSeconds, unsigned long timeoutSeconds) {
  unsigned long intervalMs = intervalSeconds * 1000UL;
  unsigned long timeoutMs = timeoutSeconds * 1000UL;

  if (intervalMs < CALIBRATION_MODE_MIN_INTERVAL_MS) intervalMs = CALIBRATION_MODE_MIN_INTERVAL_MS;
  if (intervalMs > CALIBRATION_MODE_MAX_INTERVAL_MS) intervalMs = CALIBRATION_MODE_MAX_INTERVAL_MS;
  if (timeoutMs == 0) timeoutMs = CALIBRATION_MODE_DEFAULT_TIMEOUT_MS;
  if (timeoutMs > CALIBRATION_MODE_MAX_TIMEOUT_MS) timeoutMs = CALIBRATION_MODE_MAX_TIMEOUT_MS;

  calibrationModeActive = true;
  calibrationModeStartedMs = millis();
  calibrationModeIntervalMs = intervalMs;
  calibrationModeTimeoutMs = timeoutMs;

  Serial.printf(
    "[CAL] Calibration mode started: interval=%lu sec timeout=%lu sec\n",
    calibrationModeIntervalMs / 1000UL,
    calibrationModeTimeoutMs / 1000UL
  );
}

// ==============================
// INMP441 stereo microphone support
// ==============================
#if ENABLE_INMP441_MICS
 
bool micsI2sInstalled = false;  // keep this global as before
 
// ---------------------------------------------------------------------------
// Helper: compute band energy from a magnitude spectrum.
// binFreq(n) = n * sampleRate / fftSize
// Returns the RMS energy of all bins whose centre frequency falls in [loHz, hiHz],
// expressed in dBFS relative to the same full-scale reference used for broadband RMS.
// Returns NAN if no bins fall in the range.
// ---------------------------------------------------------------------------
static float bandEnergyDbfs(const double* magnitudes, size_t fftSize,
                             uint32_t sampleRate,
                             uint32_t loHz, uint32_t hiHz,
                             double fullScale) {
  double sumSq = 0.0;
  size_t count = 0;
  double freqPerBin = (double)sampleRate / (double)fftSize;
  size_t binLo = (size_t)ceil((double)loHz  / freqPerBin);
  size_t binHi = (size_t)floor((double)hiHz / freqPerBin);
  // Only use the first half of the spectrum (Nyquist)
  size_t nyquist = fftSize / 2;
  if (binLo >= nyquist) return NAN;
  if (binHi >= nyquist) binHi = nyquist - 1;
  for (size_t b = binLo; b <= binHi; b++) {
    double m = magnitudes[b] / fullScale;
    sumSq += m * m;
    count++;
  }
  if (count == 0 || sumSq <= 0.0) return NAN;
  float rms = (float)sqrt(sumSq / (double)count);
  return (float)(20.0 * log10((double)rms));
}
 
// ---------------------------------------------------------------------------
// Helper: run arduinoFFT on a block of time-domain samples, fill bands.
// samples: array of FFT_SAMPLE_COUNT int32_t values (24-bit in MSB-shifted int32)
// ---------------------------------------------------------------------------
static void computeBands(const int32_t* samples, size_t count,
                          uint32_t sampleRate, double fullScale,
                          MicBands& out) {
  // arduinoFFT needs two double arrays; allocate on heap to avoid stack overflow.
  double* vReal = (double*)malloc(FFT_SAMPLE_COUNT * sizeof(double));
  double* vImag = (double*)malloc(FFT_SAMPLE_COUNT * sizeof(double));
  if (!vReal || !vImag) {
    free(vReal); free(vImag);
    Serial.println("[FFT] heap alloc failed");
    return;
  }
 
  size_t n = min(count, (size_t)FFT_SAMPLE_COUNT);
  for (size_t i = 0; i < n; i++) {
    vReal[i] = (double)samples[i] / fullScale;
    vImag[i] = 0.0;
  }
  // Zero-pad if we captured fewer than FFT_SAMPLE_COUNT samples
  for (size_t i = n; i < FFT_SAMPLE_COUNT; i++) {
    vReal[i] = 0.0;
    vImag[i] = 0.0;
  }
 
  ArduinoFFT<double> fft(vReal, vImag, FFT_SAMPLE_COUNT, (double)sampleRate);
  fft.windowing(FFTWindow::Hann, FFTDirection::Forward);
  fft.compute(FFTDirection::Forward);
  fft.complexToMagnitude();  // magnitudes now in vReal[0..FFT_SAMPLE_COUNT/2-1]
 
  out.sub_bass_dbfs = bandEnergyDbfs(vReal, FFT_SAMPLE_COUNT, sampleRate,   50,  150, fullScale);
  out.hum_dbfs      = bandEnergyDbfs(vReal, FFT_SAMPLE_COUNT, sampleRate,  150,  300, fullScale);
  out.piping_dbfs   = bandEnergyDbfs(vReal, FFT_SAMPLE_COUNT, sampleRate,  300,  550, fullScale);
  out.stress_dbfs   = bandEnergyDbfs(vReal, FFT_SAMPLE_COUNT, sampleRate,  550, 1500, fullScale);
  out.high_dbfs     = bandEnergyDbfs(vReal, FFT_SAMPLE_COUNT, sampleRate, 1500, 3000, fullScale);
 
  free(vReal);
  free(vImag);
}
 

// ---------------------------------------------------------------------------
// initMicsI2s: configure I2S port for the two INMP441 microphones.
// Returns true on success. Safe to call multiple times (no-op if already up).
// ---------------------------------------------------------------------------
bool initMicsI2s() {
  if (micsI2sInstalled) return true;

  i2s_config_t i2sConfig = {};
  i2sConfig.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  i2sConfig.sample_rate = INMP441_SAMPLE_RATE;
  // Read as 32-bit samples and shift right by 8 to get the 24-bit signed value.
  i2sConfig.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  // Stereo so we capture both mics (one wired L, the other wired R).
  i2sConfig.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  i2sConfig.communication_format = (i2s_comm_format_t)(I2S_COMM_FORMAT_STAND_I2S);
  i2sConfig.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  i2sConfig.dma_buf_count = 4;
  i2sConfig.dma_buf_len = 512;
  i2sConfig.use_apll = false;
  i2sConfig.tx_desc_auto_clear = false;
  i2sConfig.fixed_mclk = 0;

  esp_err_t err = i2s_driver_install(INMP441_I2S_PORT, &i2sConfig, 0, nullptr);
  if (err != ESP_OK) {
    Serial.printf("[INMP441] i2s_driver_install failed: %d\n", (int)err);
    return false;
  }

  i2s_pin_config_t pinConfig = {};
  pinConfig.bck_io_num   = INMP441_BCLK_PIN;
  pinConfig.ws_io_num    = INMP441_WS_PIN;
  pinConfig.data_out_num = I2S_PIN_NO_CHANGE;
  pinConfig.data_in_num  = INMP441_SD_PIN;

  err = i2s_set_pin(INMP441_I2S_PORT, &pinConfig);
  if (err != ESP_OK) {
    Serial.printf("[INMP441] i2s_set_pin failed: %d\n", (int)err);
    i2s_driver_uninstall(INMP441_I2S_PORT);
    return false;
  }

  i2s_zero_dma_buffer(INMP441_I2S_PORT);
  micsI2sInstalled = true;
  Serial.printf("[INMP441] I2S installed: BCLK=%d WS=%d SD=%d rate=%d\n",
                INMP441_BCLK_PIN, INMP441_WS_PIN, INMP441_SD_PIN, INMP441_SAMPLE_RATE);
  return true;
}

void shutdownMicsI2s() {
  if (!micsI2sInstalled) return;
  i2s_driver_uninstall(INMP441_I2S_PORT);
  micsI2sInstalled = false;
  Serial.println("[INMP441] I2S uninstalled");
}

MicMeasurement readMicSamples() {
  MicMeasurement result;
 
  if (!initMicsI2s()) {
    Serial.println("[INMP441] I2S init failed; skipping mic measurement");
    return result;
  }
 
  // Settling: discard the first 256 frames so the mic's DC blocker stabilises.
  // Heap-allocated (512 * 4 = 2 KB) to avoid eating into the loopTask stack.
  const size_t WARMUP_FRAMES = 256;
  {
    int32_t* warmup = (int32_t*)malloc(WARMUP_FRAMES * 2 * sizeof(int32_t));
    if (warmup) {
      size_t warmupBytesRead = 0;
      i2s_read(INMP441_I2S_PORT, warmup, WARMUP_FRAMES * 2 * sizeof(int32_t), &warmupBytesRead, pdMS_TO_TICKS(500));
      free(warmup);
    } else {
      Serial.println("[INMP441] warmup alloc failed; continuing without settling");
    }
  }
 
  // ── RMS / Peak pass ──────────────────────────────────────────────────────
  // We store every sample so we can reuse the same data for the FFT pass,
  // avoiding a second I2S capture.  The buffer holds FFT_SAMPLE_COUNT frames
  // per channel; anything beyond that still updates RMS/peak but is not fed
  // into the FFT (we have plenty of resolution with 4096 samples at 16 kHz).
  //
  // Memory: 4096 * 2 channels * 4 bytes = 32 kB — fits in heap.
  int32_t* leftBuf  = (int32_t*)malloc(FFT_SAMPLE_COUNT * sizeof(int32_t));
  int32_t* rightBuf = (int32_t*)malloc(FFT_SAMPLE_COUNT * sizeof(int32_t));
  if (!leftBuf || !rightBuf) {
    free(leftBuf); free(rightBuf);
    Serial.println("[INMP441] FFT buffer alloc failed; falling back to RMS-only");
    // Fall back: run the original RMS-only loop (no FFT data will be set)
    leftBuf = rightBuf = nullptr;
  }
 
  // chunk is 512 frames * 2 channels * 4 bytes = 4 KB — moved to heap to
  // prevent the "Stack canary watchpoint triggered (loopTask)" crash.
  const size_t CHUNK_FRAMES = 512;
  int32_t* chunk = (int32_t*)malloc(CHUNK_FRAMES * 2 * sizeof(int32_t));
  if (!chunk) {
    Serial.println("[INMP441] chunk alloc failed; skipping mic measurement");
    free(leftBuf);
    free(rightBuf);
    return result;
  }
 
  double leftSumSq = 0.0, rightSumSq = 0.0;
  int32_t leftPeak = 0, rightPeak = 0;
  uint32_t leftCount = 0, rightCount = 0;
  size_t leftFftCount = 0, rightFftCount = 0;
 
  uint32_t framesRemaining = INMP441_SAMPLE_FRAMES;
 
  while (framesRemaining > 0) {
    size_t framesThisRound = framesRemaining > CHUNK_FRAMES ? CHUNK_FRAMES : framesRemaining;
    size_t bytesWanted = framesThisRound * 2 * sizeof(int32_t);
    size_t bytesRead = 0;
    esp_err_t err = i2s_read(INMP441_I2S_PORT, chunk, bytesWanted, &bytesRead, pdMS_TO_TICKS(1000));
    if (err != ESP_OK || bytesRead == 0) break;
 
    size_t framesRead = bytesRead / (2 * sizeof(int32_t));
    for (size_t i = 0; i < framesRead; i++) {
      // ESP-IDF legacy driver: index 0 = right, 1 = left
      int32_t rs = chunk[i * 2 + 0] >> 8;  // 24-bit signed
      int32_t ls = chunk[i * 2 + 1] >> 8;
 
      double rf = (double)rs, lf = (double)ls;
      rightSumSq += rf * rf;
      leftSumSq  += lf * lf;
 
      int32_t absR = rs < 0 ? -rs : rs;
      int32_t absL = ls < 0 ? -ls : ls;
      if (absR > rightPeak) rightPeak = absR;
      if (absL > leftPeak)  leftPeak  = absL;
 
      rightCount++;
      leftCount++;
 
      // Store into FFT buffers while there is space
      if (leftBuf  && leftFftCount  < FFT_SAMPLE_COUNT) leftBuf[leftFftCount++]   = ls;
      if (rightBuf && rightFftCount < FFT_SAMPLE_COUNT) rightBuf[rightFftCount++] = rs;
    }
    framesRemaining -= framesRead;
    if (framesRead == 0) break;
  }
 
  free(chunk);  // release before FFT heap allocations in computeBands()
 
  // ── Fill RMS / Peak stats ─────────────────────────────────────────────────
  const double FULL_SCALE = 8388608.0; // 2^23
 
  auto fillStats = [&](MicChannelStats& s, double sumSq, int32_t peak, uint32_t count) {
    if (count == 0) return;
    s.ok = true;
    s.sampleCount = count;
    double rms = sqrt(sumSq / (double)count);
    s.rmsNormalized = (float)(rms / FULL_SCALE);
    s.rmsDbfs  = s.rmsNormalized > 0.0f
                 ? (float)(20.0 * log10((double)s.rmsNormalized))
                 : -200.0f;
    s.peakDbfs = peak > 0
                 ? (float)(20.0 * log10((double)peak / FULL_SCALE))
                 : -200.0f;
  };
 
  fillStats(result.left,  leftSumSq,  leftPeak,  leftCount);
  fillStats(result.right, rightSumSq, rightPeak, rightCount);
  result.ok = result.left.ok || result.right.ok;
 
  // ── FFT band analysis ──────────────────────────────────────────────────────
  if (leftBuf && leftFftCount >= 64) {
    computeBands(leftBuf,  leftFftCount,  INMP441_SAMPLE_RATE, FULL_SCALE, result.left.bands);
  }
  if (rightBuf && rightFftCount >= 64) {
    computeBands(rightBuf, rightFftCount, INMP441_SAMPLE_RATE, FULL_SCALE, result.right.bands);
  }
  free(leftBuf);
  free(rightBuf);
 
  Serial.printf("[INMP441] L: rms=%.1f dBFS peak=%.1f dBFS | sub=%.1f hum=%.1f pipe=%.1f stress=%.1f hi=%.1f\n",
    result.left.rmsDbfs, result.left.peakDbfs,
    result.left.bands.sub_bass_dbfs, result.left.bands.hum_dbfs,
    result.left.bands.piping_dbfs,   result.left.bands.stress_dbfs,
    result.left.bands.high_dbfs);
  Serial.printf("[INMP441] R: rms=%.1f dBFS peak=%.1f dBFS | sub=%.1f hum=%.1f pipe=%.1f stress=%.1f hi=%.1f\n",
    result.right.rmsDbfs, result.right.peakDbfs,
    result.right.bands.sub_bass_dbfs, result.right.bands.hum_dbfs,
    result.right.bands.piping_dbfs,   result.right.bands.stress_dbfs,
    result.right.bands.high_dbfs);
 
  return result;
}
 
#endif // ENABLE_INMP441_MICS

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

void initializeTime(bool wokeFromDeepSleep) {
  if (rtcHasValidTime()) {
    timeSource = "rtc";
    Serial.print("[TIME] Using RTC: ");
    Serial.println(timestampNow());

    // Refresh RTC from NTP on cold/manual boots, but avoid doing this on every
    // timer wake because WiFi will already be used for upload and RTC is valid.
    if (!wokeFromDeepSleep) {
      syncTime();
    }
    return;
  }

  syncTime();
}

String trimTrailingSlash(String value) {
  value.trim();
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}

String apiUrl(const String& path) {
  String base = trimTrailingSlash(apiBaseUrl);
  return base + path;
}

bool isBlank(const String& s) {
  return s.length() == 0;
}

String prefString(const char* key, const char* fallback = "") {
  prefs.begin("hivescale", true);
  String value = prefs.getString(key, fallback);
  prefs.end();
  return value;
}

void putPrefString(const char* key, const String& value) {
  prefs.begin("hivescale", false);
  prefs.putString(key, value);
  prefs.end();
}

String wifiSsidKey(int index) { return String("wifi") + index + "_ssid"; }
String wifiPassKey(int index) { return String("wifi") + index + "_pass"; }

void seedPrefsFromSecretsIfNeeded() {
  prefs.begin("hivescale", false);
  bool seeded = prefs.getBool("seeded", false);

  if (!seeded) {
    Serial.println("[PREFS] First boot seed from secrets.h");

    prefs.putString("api_base", API_BASE_URL);
    prefs.putString("api_key", API_KEY);
    prefs.putString("device_id", DEVICE_ID);

    #ifdef CLAIM_CODE
      prefs.putString("claim_code", CLAIM_CODE);
    #endif

    #ifdef CLAIM_CODE_REVISION
      prefs.putUInt("claim_rev", CLAIM_CODE_REVISION);
    #else
      prefs.putUInt("claim_rev", 1);
    #endif

    int wifiCount = 0;

    // --- NEW: support WIFI1 / WIFI2 / WIFI3 ---
    #ifdef WIFI1_SSID
      prefs.putString("wifi0_ssid", WIFI1_SSID);
      prefs.putString("wifi0_pass", WIFI1_PASS);
      wifiCount++;
    #endif

    #ifdef WIFI2_SSID
      prefs.putString("wifi1_ssid", WIFI2_SSID);
      prefs.putString("wifi1_pass", WIFI2_PASS);
      wifiCount++;
    #endif

    #ifdef WIFI3_SSID
      prefs.putString("wifi2_ssid", WIFI3_SSID);
      prefs.putString("wifi2_pass", WIFI3_PASS);
      wifiCount++;
    #endif

    // --- fallback: old single-WiFi format ---
    #if defined(WIFI_SSID) && defined(WIFI_PASS)
      if (wifiCount == 0 && String(WIFI_SSID).length() > 0) {
        prefs.putString("wifi0_ssid", WIFI_SSID);
        prefs.putString("wifi0_pass", WIFI_PASS);
        wifiCount = 1;
      }
    #endif

    prefs.putUInt("wifi_count", wifiCount);
    prefs.putBool("seeded", true);
    prefs.putBool("provisioned", true);
  }

  #ifdef CLAIM_CODE
    uint32_t storedClaimRevision = prefs.getUInt("claim_rev", 0);
    #ifdef CLAIM_CODE_REVISION
      uint32_t firmwareClaimRevision = CLAIM_CODE_REVISION;
    #else
      uint32_t firmwareClaimRevision = 1;
    #endif

    if (FORCE_RESEED || storedClaimRevision < firmwareClaimRevision) {
      Serial.println("[PREFS] Updating claim code from secrets.h revision");
      prefs.putString("claim_code", CLAIM_CODE);
      prefs.putUInt("claim_rev", firmwareClaimRevision);
    }
  #endif

  prefs.end();
}

void loadConfigFromPrefs() {
  prefs.begin("hivescale", false);

  apiBaseUrl = prefs.getString("api_base", API_BASE_URL);
  apiKey = prefs.getString("api_key", API_KEY);
  deviceId = prefs.getString("device_id", DEVICE_ID);
  claimCode = prefs.getString("claim_code", CLAIM_CODE);

  sendIntervalMs = prefs.getUInt("interval", 600) * 1000UL;
  scale1Offset = prefs.getLong("s1_offset", 0);
  scale2Offset = prefs.getLong("s2_offset", 0);
  scale1Factor = prefs.getFloat("s1_factor", -7050.0f);
  scale2Factor = prefs.getFloat("s2_factor", -7050.0f);

  prefs.end();

  Serial.println("[PREFS] Loaded config");
  Serial.printf("[PREFS] device_id: %s\n", deviceId.c_str());
  Serial.printf("[PREFS] claim_code present: %s\n", claimCode.length() > 0 ? "yes" : "no");
  Serial.printf("[PREFS] api_base: %s\n", apiBaseUrl.c_str());
  Serial.printf("[PREFS] interval ms: %lu\n", sendIntervalMs);
  Serial.printf("[PREFS] scale1 offset: %ld factor: %.6f\n", scale1Offset, scale1Factor);
  Serial.printf("[PREFS] scale2 offset: %ld factor: %.6f\n", scale2Offset, scale2Factor);
}

void saveScaleConfig() {
  prefs.begin("hivescale", false);
  prefs.putUInt("interval", sendIntervalMs / 1000UL);
  prefs.putLong("s1_offset", scale1Offset);
  prefs.putLong("s2_offset", scale2Offset);
  prefs.putFloat("s1_factor", scale1Factor);
  prefs.putFloat("s2_factor", scale2Factor);
  prefs.end();
}

int getWifiCount() {
  prefs.begin("hivescale", true);
  uint32_t count = prefs.getUInt("wifi_count", 0);
  prefs.end();
  if (count > MAX_WIFI_NETWORKS) count = MAX_WIFI_NETWORKS;
  return (int)count;
}

bool saveWifiNetwork(int index, const String& ssid, const String& pass) {
  if (index < 0 || index >= MAX_WIFI_NETWORKS || ssid.length() == 0) return false;

  prefs.begin("hivescale", false);
  prefs.putString(wifiSsidKey(index).c_str(), ssid);
  prefs.putString(wifiPassKey(index).c_str(), pass);

  int count = prefs.getUInt("wifi_count", 0);
  if (index + 1 > count) prefs.putUInt("wifi_count", index + 1);
  prefs.putBool("provisioned", true);
  prefs.end();
  return true;
}

void clearWifiCredentials() {
  prefs.begin("hivescale", false);
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    prefs.remove(wifiSsidKey(i).c_str());
    prefs.remove(wifiPassKey(i).c_str());
  }
  prefs.putUInt("wifi_count", 0);
  prefs.end();
  activeWifiSsid = "";
}

void factoryResetPreferences() {
  Serial.println("[PREFS] Factory reset requested");
  prefs.begin("hivescale", false);
  prefs.clear();
  prefs.end();
  delay(300);
  ESP.restart();
}

bool connectWifi(unsigned long timeoutMs = WIFI_CONNECT_TIMEOUT_MS) {
  if (WiFi.status() == WL_CONNECTED) return true;

  int count = getWifiCount();
  if (count <= 0) {
    Serial.println("[WIFI] No saved WiFi credentials");
    return false;
  }

  String ssids[MAX_WIFI_NETWORKS];
  String passes[MAX_WIFI_NETWORKS];
  prefs.begin("hivescale", true);
  for (int i = 0; i < count; i++) {
    ssids[i] = prefs.getString(wifiSsidKey(i).c_str(), "");
    passes[i] = prefs.getString(wifiPassKey(i).c_str(), "");
  }
  prefs.end();

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);

  for (int i = 0; i < count; i++) {
    String ssid = ssids[i];
    String pass = passes[i];

    if (ssid.length() == 0) continue;

    Serial.printf("[WIFI] Trying saved network %d/%d: %s\n", i + 1, count, ssid.c_str());
    WiFi.disconnect(true, true);
    delay(200);
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
      Serial.print(".");
      delay(500);
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
      activeWifiSsid = ssid;
      Serial.println("[WIFI] Connected");
      Serial.print("[WIFI] IP: ");
      Serial.println(WiFi.localIP());
      Serial.printf("[WIFI] RSSI: %d dBm\n", WiFi.RSSI());
      return true;
    }

    Serial.printf("[WIFI] Failed network: %s status=%d\n", ssid.c_str(), WiFi.status());
  }

  Serial.println("[WIFI] All saved networks failed. Not starting AP automatically for power saving.");
  return false;
}

bool connectNetwork() {
  return connectWifi();
}

void preparePowerMonitorsForSleep() {
#if ENABLE_INA219_SOLAR
  if (solarMonitorOk) solarMonitor.powerSave(true);
#endif
}

String htmlEscape(String s) {
  s.replace("&", "&amp;");
  s.replace("<", "&lt;");
  s.replace(">", "&gt;");
  s.replace("\"", "&quot;");
  return s;
}

IPAddress provisioningPortalIp() {
  return IPAddress(192, 168, 4, 1);
}

String provisioningPortalUrl() {
  return String("http://") + provisioningPortalIp().toString() + "/";
}

void sendNoCacheHeaders() {
  setupServer.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  setupServer.sendHeader("Pragma", "no-cache");
  setupServer.sendHeader("Expires", "0");
}

void sendPortalRedirect() {
  sendNoCacheHeaders();
  setupServer.sendHeader("Location", provisioningPortalUrl(), true);
  setupServer.send(302, "text/plain", "Redirecting to HiveScale setup portal");
}

void handleCaptivePortalProbe() {
  sendPortalRedirect();
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

String jsonStringOrNA(JsonDocument& doc, const char* key) {
  if (doc[key].isNull()) return "n/a";
  String value = doc[key].as<String>();
  value.trim();
  return value.length() > 0 ? value : String("n/a");
}

String jsonNumberOrNA(JsonDocument& doc, const char* key, uint8_t decimals, const char* unit) {
  if (doc[key].isNull()) return "n/a";
  double value = doc[key].as<double>();
  if (isnan(value)) return "n/a";

  String text = String(value, static_cast<unsigned int>(decimals));
  if (unit != nullptr && unit[0] != '\0') {
    text += " ";
    text += unit;
  }
  return text;
}

String jsonBoolOrNA(JsonDocument& doc, const char* key) {
  if (doc[key].isNull()) return "n/a";
  return doc[key].as<bool>() ? "yes" : "no";
}

void addMeasurementRow(String& html, const String& label, const String& value) {
  html += "<tr><th>" + htmlEscape(label) + "</th><td>" + htmlEscape(value) + "</td></tr>";
}

void appendLastSensorPanel(String& html) {
  ensureLastMeasurementLoaded();

  html += "<fieldset><legend>Last sensor values</legend>";

  if (lastMeasurementJson.length() == 0) {
    html += "<p>No saved sensor values are available yet. After the next measurement cycle this panel will show the latest stored reading.</p>";
    html += "</fieldset>";
    return;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, lastMeasurementJson);
  if (err) {
    html += "<p>The last stored measurement could not be parsed.</p>";
    html += "</fieldset>";
    return;
  }

  html += "<table>";
  addMeasurementRow(html, "Timestamp", jsonStringOrNA(doc, "timestamp"));
  addMeasurementRow(html, "Scale 1 weight", jsonNumberOrNA(doc, "scale_1_weight_kg", 3, "kg"));
  addMeasurementRow(html, "Scale 2 weight", jsonNumberOrNA(doc, "scale_2_weight_kg", 3, "kg"));
  addMeasurementRow(html, "Hive 1 temperature", jsonNumberOrNA(doc, "hive_1_temp_c", 2, "C"));
  addMeasurementRow(html, "Hive 2 temperature", jsonNumberOrNA(doc, "hive_2_temp_c", 2, "C"));
  addMeasurementRow(html, "Ambient temperature", jsonNumberOrNA(doc, "ambient_temp_c", 2, "C"));
  addMeasurementRow(html, "Ambient humidity", jsonNumberOrNA(doc, "ambient_humidity_percent", 1, "%"));
  addMeasurementRow(html, "Scale 1 raw", jsonNumberOrNA(doc, "scale_1_raw", 0, ""));
  addMeasurementRow(html, "Scale 2 raw", jsonNumberOrNA(doc, "scale_2_raw", 0, ""));
  addMeasurementRow(html, "WiFi RSSI", jsonNumberOrNA(doc, "rssi_dbm", 0, "dBm"));
  addMeasurementRow(html, "SD card OK", jsonBoolOrNA(doc, "sd_ok"));
  addMeasurementRow(html, "RTC OK", jsonBoolOrNA(doc, "rtc_ok"));
  addMeasurementRow(html, "SHT4x OK", jsonBoolOrNA(doc, "sht_ok"));

  if (!doc["solar_load_voltage_v"].isNull() || !doc["solar_current_ma"].isNull() || !doc["solar_power_mw"].isNull()) {
    addMeasurementRow(html, "Solar voltage", jsonNumberOrNA(doc, "solar_load_voltage_v", 3, "V"));
    addMeasurementRow(html, "Solar current", jsonNumberOrNA(doc, "solar_current_ma", 1, "mA"));
    addMeasurementRow(html, "Solar power", jsonNumberOrNA(doc, "solar_power_mw", 1, "mW"));
  }

  if (!doc["battery_voltage_v"].isNull() || !doc["battery_soc_percent"].isNull()) {
    addMeasurementRow(html, "Battery voltage", jsonNumberOrNA(doc, "battery_voltage_v", 3, "V"));
    addMeasurementRow(html, "Battery state of charge", jsonNumberOrNA(doc, "battery_soc_percent", 1, "%"));
    addMeasurementRow(html, "Battery alert", jsonBoolOrNA(doc, "battery_alert"));
  }

  if (!doc["mic_left_rms_dbfs"].isNull() || !doc["mic_right_rms_dbfs"].isNull()) {
    addMeasurementRow(html, "Mic left RMS", jsonNumberOrNA(doc, "mic_left_rms_dbfs", 1, "dBFS"));
    addMeasurementRow(html, "Mic right RMS", jsonNumberOrNA(doc, "mic_right_rms_dbfs", 1, "dBFS"));
  }

  html += "</table>";
  html += "<p class='meta'>Shown from the latest measurement in memory or from ";
  html += BACKUP_FILE;
  html += " on the SD card. Refresh this page after a new cycle to update it.</p>";
  html += "</fieldset>";
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

void handleSdDownloadAll() {
  if (!initSdCard()) {
    setupServer.send(503, "text/plain", "SD card not available");
    return;
  }

  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    setupServer.send(500, "text/plain", "Could not open SD root directory");
    return;
  }

  uint64_t tarSize = tarDirectorySize(root, "") + 1024;
  root.close();

  if (tarSize > 0xFFFFFFFFULL) {
    setupServer.send(413, "text/plain", "SD data is too large to stream in one download on this firmware");
    return;
  }

  root = SD.open("/");
  if (!root || !root.isDirectory()) {
    setupServer.send(500, "text/plain", "Could not reopen SD root directory");
    return;
  }

  setupServer.sendHeader("Content-Disposition", "attachment; filename=\"hivescale-sd-data.tar\"");
  setupServer.sendHeader("Connection", "close");
  setupServer.setContentLength((size_t)tarSize);
  setupServer.send(200, "application/x-tar", "");

  WiFiClient client = setupServer.client();
  streamTarDirectory(client, root, "");

  uint8_t zeros[1024];
  memset(zeros, 0, sizeof(zeros));
  client.write(zeros, sizeof(zeros));
  root.close();
  Serial.println("[SD] Download-all TAR completed");
}

void handleSetupRoot() {
  sendNoCacheHeaders();

  String html;
  html += "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>HiveScale Setup</title>";
  html += "<style>body{font-family:system-ui;margin:24px;max-width:760px}input{width:100%;padding:10px;margin:6px 0 14px}button,a.button{display:inline-block;padding:12px 16px;margin:4px 0;text-decoration:none;border:1px solid #333;border-radius:4px;background:#f4f4f4;color:#111}fieldset{margin:16px 0;padding:16px}table{border-collapse:collapse;width:100%}th,td{text-align:left;border-bottom:1px solid #ddd;padding:6px}th{width:48%}.meta{color:#666;font-size:.9em}</style>";
  html += "</head><body><h1>HiveScale Setup</h1>";
  html += "<p>Firmware: " + String(FIRMWARE_VERSION) + "</p>";
  html += "<p>Setup portal: <a href='" + provisioningPortalUrl() + "'>" + provisioningPortalUrl() + "</a></p>";
  appendLastSensorPanel(html);
  html += "<fieldset><legend>SD card data</legend>";
  if (sdOk) {
    html += "<p><a class='button' href='/sd/download-all'>Download all SD data (.tar)</a></p>";
    html += "<p>This streams the SD card contents directly; large cards can take a while.</p>";
  } else {
    html += "<p>SD card not available.</p>";
  }
  html += "</fieldset>";
  html += "<form method='POST' action='/save'>";
  html += "<fieldset><legend>Backend</legend>";
  html += "<label>Device ID</label><input name='device_id' value='" + htmlEscape(deviceId) + "'>";
  html += "<label>Claim code</label><input name='claim_code' value='" + htmlEscape(claimCode) + "'>";
  html += "<label>API base URL</label><input name='api_base' value='" + htmlEscape(apiBaseUrl) + "'>";
  html += "<label>API key</label><input name='api_key' value='" + htmlEscape(apiKey) + "'>";
  html += "</fieldset>";

  html += "<fieldset><legend>WiFi networks</legend>";
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    prefs.begin("hivescale", true);
    String ssid = prefs.getString(wifiSsidKey(i).c_str(), "");
    prefs.end();
    html += "<h3>Network " + String(i + 1) + "</h3>";
    html += "<label>SSID</label><input name='ssid" + String(i) + "' value='" + htmlEscape(ssid) + "'>";
    html += "<label>Password</label><input type='password' name='pass" + String(i) + "' placeholder='Leave blank to keep current password'>";
  }
  html += "</fieldset>";
  html += "<button type='submit'>Save and reboot</button></form>";
  html += "<form method='POST' action='/reset' onsubmit='return confirm(\"Reset all Preferences?\")'><button type='submit'>Factory reset Preferences</button></form>";
  html += "</body></html>";
  setupServer.send(200, "text/html", html);
}

void handleSetupSave() {
  prefs.begin("hivescale", false);

  String newDeviceId = setupServer.arg("device_id");
  String newClaimCode = setupServer.arg("claim_code");
  String newApiBase = trimTrailingSlash(setupServer.arg("api_base"));
  String newApiKey = setupServer.arg("api_key");

  newClaimCode.trim();

  if (newDeviceId.length() > 0) prefs.putString("device_id", newDeviceId);
  prefs.putString("claim_code", newClaimCode);
  if (newApiBase.length() > 0) prefs.putString("api_base", newApiBase);
  if (newApiKey.length() > 0) prefs.putString("api_key", newApiKey);

  int savedCount = 0;
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    String ssid = setupServer.arg("ssid" + String(i));
    String pass = setupServer.arg("pass" + String(i));
    ssid.trim();

    if (ssid.length() == 0) {
      prefs.remove(wifiSsidKey(i).c_str());
      prefs.remove(wifiPassKey(i).c_str());
      continue;
    }

    prefs.putString(wifiSsidKey(i).c_str(), ssid);
    if (pass.length() > 0) {
      prefs.putString(wifiPassKey(i).c_str(), pass);
    }
    savedCount = i + 1;
  }

  prefs.putUInt("wifi_count", savedCount);
  prefs.putBool("provisioned", true);
  prefs.putBool("seeded", true);
  prefs.end();

  setupServer.send(200, "text/html", "<html><body><h1>Saved</h1><p>Device will reboot now.</p></body></html>");
  delay(1000);
  ESP.restart();
}

void handleSetupReset() {
  setupServer.send(200, "text/html", "<html><body><h1>Resetting</h1></body></html>");
  delay(500);
  factoryResetPreferences();
}

void startProvisioningPortal() {
  if (provisioningActive) return;

  Serial.println("[SETUP] Starting provisioning AP");
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.mode(WIFI_AP);

  IPAddress apIp = provisioningPortalIp();
  IPAddress subnet(255, 255, 255, 0);
  if (!WiFi.softAPConfig(apIp, apIp, subnet)) {
    Serial.println("[SETUP] softAPConfig failed; continuing with default AP configuration");
  }

  String suffix = String((uint32_t)ESP.getEfuseMac(), HEX);
  suffix.toUpperCase();
  String apName = "HiveScale-Setup-" + suffix.substring(suffix.length() - 4);

  bool ok = WiFi.softAP(apName.c_str());
  if (!ok) {
    Serial.println("[SETUP] softAP failed");
    return;
  }

  setupServer.on("/", HTTP_GET, handleSetupRoot);
  setupServer.on("/save", HTTP_POST, handleSetupSave);
  setupServer.on("/reset", HTTP_POST, handleSetupReset);
  setupServer.on("/sd/download-all", HTTP_GET, handleSdDownloadAll);

  // Common captive-portal probe URLs used by Android, iOS/macOS, Windows, and Firefox.
  // Redirecting these makes most phones/laptops show the setup page automatically
  // after they connect to the HiveScale AP. Devices that suppress captive portals
  // can still open http://192.168.4.1/ manually.
  setupServer.on("/generate_204", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/gen_204", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/hotspot-detect.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/library/test/success.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/connecttest.txt", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/ncsi.txt", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/canonical.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/fwlink", HTTP_GET, handleCaptivePortalProbe);
  setupServer.onNotFound(handleCaptivePortalProbe);
  setupServer.begin();

  setupDnsServer.start(CAPTIVE_DNS_PORT, "*", WiFi.softAPIP());

  provisioningActive = true;
  provisioningStartedMs = millis();

  Serial.printf("[SETUP] AP SSID: %s\n", apName.c_str());
  Serial.print("[SETUP] Open ");
  Serial.println(provisioningPortalUrl());
  Serial.println("[SETUP] Captive DNS redirect enabled for AP clients");
}

void stopProvisioningPortal() {
  if (!provisioningActive) return;
  Serial.println("[SETUP] Stopping provisioning AP");
  setupDnsServer.stop();
  setupServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  provisioningActive = false;
}

void handleButton() {
  bool down = digitalRead(SETUP_BUTTON_PIN) == LOW;
  unsigned long now = millis();

  if (down && !buttonWasDown) {
    buttonWasDown = true;
    buttonDownMs = now;
    longPressHandled = false;
  }

  if (down && buttonWasDown && !longPressHandled && now - buttonDownMs >= BUTTON_LONG_PRESS_MS) {
    longPressHandled = true;
    Serial.println("[BUTTON] Long press detected: factory reset Preferences");
    factoryResetPreferences();
  }

  if (!down && buttonWasDown) {
    unsigned long held = now - buttonDownMs;
    buttonWasDown = false;

    if (held > BUTTON_DEBOUNCE_MS && held < BUTTON_LONG_PRESS_MS && !longPressHandled) {
      Serial.println("[BUTTON] Short press detected: start provisioning AP");
      startProvisioningPortal();
    }
  }
}

String timestampNow() {
  if (rtcOk) {
    DateTime now = rtc.now();
    if (now.year() >= 2024 && now.year() <= 2099) {
      char buf[25];
      snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", now.year(), now.month(), now.day(), now.hour(), now.minute(), now.second());
      return String(buf);
    }
  }

  struct tm tmNow;
  if (getLocalTime(&tmNow, 100)) {
    char buf[25];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", tmNow.tm_year + 1900, tmNow.tm_mon + 1, tmNow.tm_mday, tmNow.tm_hour, tmNow.tm_min, tmNow.tm_sec);
    return String(buf);
  }

  return String("1970-01-01T00:00:00Z");
}

void syncTime() {
  if (!connectWifi()) {
    Serial.println("[TIME] Cannot sync time: WiFi unavailable");
    return;
  }

  Serial.println("[TIME] Syncing with NTP...");
  configTime(0, 0, "pool.ntp.org", "time.nist.gov", "time.google.com");

  struct tm tmNow;
  for (int i = 0; i < 20; i++) {
    if (getLocalTime(&tmNow, 500)) {
      time_t nowUnix = mktime(&tmNow);

      if (nowUnix > 1700000000) {
        Serial.println("[TIME] NTP sync OK");
        timeSource = "ntp";

        if (rtcOk) {
          struct tm* utc = gmtime(&nowUnix);
          rtc.adjust(DateTime(utc->tm_year + 1900, utc->tm_mon + 1, utc->tm_mday, utc->tm_hour, utc->tm_min, utc->tm_sec));
          Serial.println("[TIME] RTC updated from NTP");
        }

        Serial.print("[TIME] Current timestamp: ");
        Serial.println(timestampNow());
        return;
      }
    }
    delay(500);
  }

  Serial.println("[TIME] NTP sync FAILED");

  if (rtcOk) {
    DateTime now = rtc.now();
    if (now.year() >= 2024 && now.year() <= 2099) {
      timeSource = "rtc";
      Serial.println("[TIME] Using RTC");
      return;
    }
  }

  timeSource = "invalid";
}

void addAuthHeader(HTTPClient& http) {
  if (apiKey.length() > 0) http.addHeader("X-API-Key", apiKey);
}

bool httpGetJson(const String& url, JsonDocument& doc) {
  if (!connectWifi()) return false;

  Serial.println("[HTTP GET]");
  Serial.println(url);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP GET] http.begin failed");
    return false;
  }

  addAuthHeader(http);

  int code = http.GET();
  String body = http.getString();

  Serial.printf("[HTTP GET] Status: %d\n", code);
  Serial.print("[HTTP GET] Body: ");
  Serial.println(body);

  http.end();

  if (code < 200 || code >= 300) return false;

  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.print("[HTTP GET] JSON parse error: ");
    Serial.println(err.c_str());
    return false;
  }

  return true;
}

bool httpPostJson(const String& url, const String& json, String* response = nullptr) {
  if (!connectWifi()) {
    Serial.println("[HTTP POST] No WiFi");
    return false;
  }

  Serial.println("[HTTP POST]");
  Serial.print("[HTTP POST] URL: ");
  Serial.println(url);
  Serial.print("[HTTP POST] Payload: ");
  Serial.println(json);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP POST] http.begin failed");
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  addAuthHeader(http);

  int code = http.POST((uint8_t*)json.c_str(), json.length());
  String body = http.getString();

  Serial.printf("[HTTP POST] Status: %d\n", code);
  Serial.print("[HTTP POST] Response: ");
  Serial.println(body);

  if (response) *response = body;

  http.end();

  if (code >= 200 && code < 300) {
    Serial.println("[HTTP POST] SUCCESS");
    return true;
  }

  Serial.println("[HTTP POST] FAILED");
  return false;
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

long readAverageRaw(HX711& scale, int samples = 15) {
  if (!scale.wait_ready_timeout(2000)) {
    Serial.println("[HX711] Not ready");
    return 0;
  }
  return scale.read_average(samples);
}

float weightFromRaw(long raw, long offset, float factor) {
  if (factor == 0.0f) return NAN;
  return ((float)(raw - offset)) / factor;
}

String createMeasurementJson() {
  Serial.println("[MEASURE] Reading sensors...");

  powerUpScales();

  ds18b20.requestTemperatures();

  float hiveTemp1 = ds18b20.getTempCByIndex(0);
  float hiveTemp2 = ds18b20.getTempCByIndex(1);
  float ambientTemp = NAN;
  float ambientHumidity = NAN;

#if ENABLE_INA219_SOLAR
  float solarBusVoltage = NAN;
  float solarShuntVoltageMv = NAN;
  float solarLoadVoltage = NAN;
  float solarCurrentMa = NAN;
  float solarPowerMw = NAN;
#endif

#if ENABLE_MAX17048_BATTERY
  float batteryVoltage = NAN;
  float batterySoc = NAN;
  bool batteryAlert = false;
#endif

  if (shtOk) {
    sensors_event_t humidity, temp;
    if (sht4.getEvent(&humidity, &temp)) {
      ambientTemp = temp.temperature;
      ambientHumidity = humidity.relative_humidity;
    } else {
      Serial.println("[SHT4x] Read failed");
    }
  }

  long raw1 = readAverageRaw(scale1);
  long raw2 = readAverageRaw(scale2);
  float weight1 = weightFromRaw(raw1, scale1Offset, scale1Factor);
  float weight2 = weightFromRaw(raw2, scale2Offset, scale2Factor);

#if ENABLE_INA219_SOLAR
  if (solarMonitorOk) {
    solarMonitor.powerSave(false);
    delay(10);
    solarBusVoltage = solarMonitor.getBusVoltage_V();
    solarShuntVoltageMv = solarMonitor.getShuntVoltage_mV();
    solarCurrentMa = solarMonitor.getCurrent_mA();
    solarPowerMw = solarMonitor.getPower_mW();
    solarLoadVoltage = solarBusVoltage + (solarShuntVoltageMv / 1000.0f);
    solarMonitor.powerSave(true);
  }
#endif

#if ENABLE_MAX17048_BATTERY
  if (batteryMonitorOk) {
    batteryVoltage = batteryGauge.getVoltage();
    batterySoc = batteryGauge.getSOC();
    batteryAlert = batteryGauge.getAlert();
  }
#endif
#if ENABLE_INMP441_MICS
  MicMeasurement micResult = readMicSamples();
#endif


  Serial.printf("[MEASURE] raw1=%ld weight1=%.3f kg\n", raw1, weight1);
  Serial.printf("[MEASURE] raw2=%ld weight2=%.3f kg\n", raw2, weight2);
  Serial.printf("[MEASURE] hiveTemp1=%.2f hiveTemp2=%.2f\n", hiveTemp1, hiveTemp2);
  Serial.printf("[MEASURE] ambientTemp=%.2f humidity=%.2f\n", ambientTemp, ambientHumidity);
#if ENABLE_INA219_SOLAR
  Serial.printf("[MEASURE] solar load=%.3f V current=%.2f mA power=%.2f mW\n", solarLoadVoltage, solarCurrentMa, solarPowerMw);
#endif
#if ENABLE_MAX17048_BATTERY
  Serial.printf("[MEASURE] battery=%.3f V soc=%.1f%% alert=%s\n", batteryVoltage, batterySoc, batteryAlert ? "yes" : "no");
#endif

  JsonDocument doc;
  doc["device_id"] = deviceId;
  if (claimCode.length() > 0) doc["claim_code"] = claimCode;
  doc["timestamp"] = timestampNow();
  doc["scale_1_weight_kg"] = weight1;
  doc["scale_2_weight_kg"] = weight2;
  doc["hive_1_temp_c"] = hiveTemp1;
  doc["hive_2_temp_c"] = hiveTemp2;
  doc["ambient_temp_c"] = ambientTemp;
  doc["ambient_humidity_percent"] = ambientHumidity;
  doc["network_transport"] = "wifi";
  doc["rssi_dbm"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["calibration_mode"] = calibrationModeActive;
  doc["boot_count"] = rtcBootCount;
  doc["time_source"] = timeSource;
  doc["scale_1_raw"] = raw1;
  doc["scale_2_raw"] = raw2;
  doc["sd_ok"] = sdOk;
  doc["rtc_ok"] = rtcOk;
  doc["sht_ok"] = shtOk;
#if ENABLE_INA219_SOLAR
  doc["solar_monitor_ok"] = solarMonitorOk;
  doc["solar_bus_voltage_v"] = solarBusVoltage;
  doc["solar_shunt_voltage_mv"] = solarShuntVoltageMv;
  doc["solar_load_voltage_v"] = solarLoadVoltage;
  doc["solar_current_ma"] = solarCurrentMa;
  doc["solar_power_mw"] = solarPowerMw;
#endif
#if ENABLE_MAX17048_BATTERY
  doc["battery_monitor_ok"] = batteryMonitorOk;
  doc["battery_voltage_v"] = batteryVoltage;
  doc["battery_soc_percent"] = batterySoc;
  doc["battery_alert"] = batteryAlert;
#endif
#if ENABLE_INMP441_MICS
  doc["mic_ok"]                       = micResult.ok;
  doc["mic_sample_rate_hz"]           = (uint32_t)INMP441_SAMPLE_RATE;
  doc["mic_sample_frames"]            = (uint32_t)INMP441_SAMPLE_FRAMES;
  doc["mic_left_ok"]                  = micResult.left.ok;
  doc["mic_left_rms_dbfs"]            = micResult.left.rmsDbfs;
  doc["mic_left_peak_dbfs"]           = micResult.left.peakDbfs;
  doc["mic_left_rms_normalized"]      = micResult.left.rmsNormalized;
  doc["mic_right_ok"]                 = micResult.right.ok;
  doc["mic_right_rms_dbfs"]           = micResult.right.rmsDbfs;
  doc["mic_right_peak_dbfs"]          = micResult.right.peakDbfs;
  doc["mic_right_rms_normalized"]     = micResult.right.rmsNormalized;
  doc["mic_left_band_sub_bass_dbfs"]  = micResult.left.bands.sub_bass_dbfs;
  doc["mic_left_band_hum_dbfs"]       = micResult.left.bands.hum_dbfs;
  doc["mic_left_band_piping_dbfs"]    = micResult.left.bands.piping_dbfs;
  doc["mic_left_band_stress_dbfs"]    = micResult.left.bands.stress_dbfs;
  doc["mic_left_band_high_dbfs"]      = micResult.left.bands.high_dbfs;
  doc["mic_right_band_sub_bass_dbfs"] = micResult.right.bands.sub_bass_dbfs;
  doc["mic_right_band_hum_dbfs"]      = micResult.right.bands.hum_dbfs;
  doc["mic_right_band_piping_dbfs"]   = micResult.right.bands.piping_dbfs;
  doc["mic_right_band_stress_dbfs"]   = micResult.right.bands.stress_dbfs;
  doc["mic_right_band_high_dbfs"]     = micResult.right.bands.high_dbfs;
#endif

  String output;
  serializeJson(doc, output);
  rememberLastMeasurement(output);

  Serial.print("[MEASURE] JSON: ");
  Serial.println(output);
  return output;
}

bool uploadLine(const String& line) {
  String response;
  bool ok = httpPostJson(apiUrl("/api/v1/measurements"), line, &response);

  if (!ok) Serial.println("[UPLOAD] Upload failed");
  else Serial.println("[UPLOAD] Upload accepted by server");

  return ok;
}

bool uploadCachedLines() {
  if (!sdOk) {
    Serial.println("[CACHE] No SD card, skipping cached upload");
    return true;
  }

  if (!SD.exists(CACHE_FILE)) {
    Serial.println("[CACHE] No cache file");
    return true;
  }

  if (!cacheFileLooksSane()) {
    Serial.println("[CACHE] Cache file was quarantined or removed; skipping cached upload this cycle");
    return false;
  }

  File in = SD.open(CACHE_FILE, FILE_READ);
  if (!in) {
    Serial.println("[CACHE] Failed to open cache file for read");
    return false;
  }

  SD.remove(TEMP_FILE);
  File out = SD.open(TEMP_FILE, FILE_WRITE);
  if (!out) {
    Serial.println("[CACHE] Failed to open temp cache file");
    in.close();
    return false;
  }

  bool encounteredFailure = false;
  bool hitUploadLimit = false;
  int total = 0;
  int uploaded = 0;
  int kept = 0;
  int dropped = 0;

  while (in.available()) {
    String line = in.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    total++;

    if (line.length() > CACHE_MAX_LINE_BYTES) {
      dropped++;
      Serial.printf("[CACHE] Dropping oversized cached line %d (%u bytes)\n", total, (unsigned)line.length());
      continue;
    }

    bool mayUpload = !encounteredFailure && uploaded < CACHE_UPLOAD_MAX_LINES_PER_CYCLE;

    if (mayUpload) {
      Serial.printf("[CACHE] Uploading cached line %d\n", total);
      if (uploadLine(line)) {
        uploaded++;
        delay(100);
        continue;
      }

      encounteredFailure = true;
      Serial.println("[CACHE] Cached upload failed; keeping this and remaining cached lines");
    } else if (!encounteredFailure && uploaded >= CACHE_UPLOAD_MAX_LINES_PER_CYCLE) {
      hitUploadLimit = true;
    }

    kept++;
    size_t written = out.println(line);
    if (written == 0) {
      Serial.println("[CACHE] Failed to write retained line to temp cache");
      encounteredFailure = true;
    }
  }

  in.close();
  out.flush();
  out.close();

  if (!SD.remove(CACHE_FILE)) {
    Serial.println("[CACHE] Warning: failed to remove old cache file");
  }

  if (kept > 0) {
    if (!SD.rename(TEMP_FILE, CACHE_FILE)) {
      Serial.println("[CACHE] ERROR: failed to rename temp cache file back to cache file");
      return false;
    }
  } else {
    SD.remove(TEMP_FILE);
  }

  Serial.printf(
    "[CACHE] Total=%d Uploaded=%d Kept=%d Dropped=%d Limit=%s\n",
    total,
    uploaded,
    kept,
    dropped,
    hitUploadLimit ? "yes" : "no"
  );

  return kept == 0 && !encounteredFailure;
}

void fetchRemoteConfig() {
  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/config");

  Serial.println("[CONFIG] Fetching remote config");

  if (!httpGetJson(url, doc)) {
    Serial.println("[CONFIG] Failed to fetch config");
    return;
  }

  sendIntervalMs = (unsigned long)(doc["send_interval_seconds"] | 600) * 1000UL;
  scale1Offset = doc["scale1_offset"] | scale1Offset;
  scale1Factor = doc["scale1_factor"] | scale1Factor;
  scale2Offset = doc["scale2_offset"] | scale2Offset;
  scale2Factor = doc["scale2_factor"] | scale2Factor;

  if (doc["claim_code"].is<const char*>()) {
    String remoteClaimCode = doc["claim_code"].as<String>();
    remoteClaimCode.trim();
    if (remoteClaimCode.length() > 0 && remoteClaimCode != claimCode) {
      Serial.println("[CONFIG] Updating claim code from remote config");
      claimCode = remoteClaimCode;
      putPrefString("claim_code", claimCode);
    }
  }

  saveScaleConfig();
  Serial.println("[CONFIG] Remote config applied");
}

String absoluteUrl(String maybeRelativeUrl) {
  maybeRelativeUrl.trim();
  if (maybeRelativeUrl.startsWith("http://") || maybeRelativeUrl.startsWith("https://")) return maybeRelativeUrl;
  if (!maybeRelativeUrl.startsWith("/")) maybeRelativeUrl = "/" + maybeRelativeUrl;
  return trimTrailingSlash(apiBaseUrl) + maybeRelativeUrl;
}

bool performFirmwareUpdate(const String& firmwareUrl) {
  if (!connectWifi()) return false;

  String url = absoluteUrl(firmwareUrl);
  Serial.print("[OTA] Downloading firmware: ");
  Serial.println(url);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!http.begin(client, url)) {
    Serial.println("[OTA] http.begin failed");
    return false;
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[OTA] Download failed. HTTP %d\n", code);
    http.end();
    return false;
  }

  int contentLength = http.getSize();
  if (contentLength <= 0) {
    Serial.println("[OTA] Invalid content length");
    http.end();
    return false;
  }

  if (!Update.begin(contentLength)) {
    Serial.printf("[OTA] Update.begin failed. Error %d\n", Update.getError());
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t written = Update.writeStream(*stream);

  if (written != (size_t)contentLength) {
    Serial.printf("[OTA] Written only %u/%d bytes\n", (unsigned)written, contentLength);
  }

  bool ok = Update.end();
  if (!ok) {
    Serial.printf("[OTA] Update.end failed. Error %d\n", Update.getError());
    http.end();
    return false;
  }

  if (!Update.isFinished()) {
    Serial.println("[OTA] Update not finished");
    http.end();
    return false;
  }

  http.end();
  Serial.println("[OTA] Update successful, rebooting");
  delay(1000);
  ESP.restart();
  return true;
}

void checkForOtaUpdate() {
  if (!connectNetwork()) {
    Serial.println("[OTA] Skipping: network unavailable");
    return;
  }

  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/firmware?version=" + FIRMWARE_VERSION);

  Serial.println("[OTA] Checking for update");
  if (!httpGetJson(url, doc)) {
    Serial.println("[OTA] Check failed");
    return;
  }

  bool updateAvailable = doc["update"] | false;
  if (!updateAvailable) {
    Serial.println("[OTA] No update available");
    return;
  }

  String version = doc["version"] | "unknown";
  String fwUrl = doc["url"] | "";

  if (fwUrl.length() == 0) {
    Serial.println("[OTA] Update response missing url");
    return;
  }

  Serial.printf("[OTA] Update available: %s\n", version.c_str());
  performFirmwareUpdate(fwUrl);
}

void postCommandResult(int commandId, bool success, const String& message) {
  JsonDocument result;
  result["success"] = success;
  result["message"] = message;

  String payload;
  serializeJson(result, payload);

  httpPostJson(apiUrl(String("/api/v1/devices/") + deviceId + "/commands/" + commandId + "/result"), payload);
}

void checkCommands() {
  if (!connectNetwork()) return;

  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/commands/next");

  Serial.println("[CMD] Checking for command");
  if (!httpGetJson(url, doc)) {
    Serial.println("[CMD] Command check failed");
    return;
  }

  bool hasCommand = doc["command"] | false;
  if (!hasCommand) {
    Serial.println("[CMD] No pending command");
    return;
  }

  int commandId = doc["id"] | 0;
  String type = doc["command_type"] | "";
  JsonObject payload = doc["payload"].as<JsonObject>();
  Serial.printf("[CMD] Received command %d: %s\n", commandId, type.c_str());

  if (type == "reset_preferences" || type == "factory_reset") {
    postCommandResult(commandId, true, "Preferences reset; rebooting");
    delay(500);
    factoryResetPreferences();
  } else if (type == "reset_wifi") {
    clearWifiCredentials();
    postCommandResult(commandId, true, "WiFi credentials cleared");
    delay(500);
    ESP.restart();
  } else if (type == "check_ota" || type == "ota_update") {
    postCommandResult(commandId, true, "OTA check started");
    checkForOtaUpdate();
  } else if (type == "start_provisioning") {
    // This only makes sense while someone is physically near the device.
    postCommandResult(commandId, true, "Provisioning AP started");
    startProvisioningPortal();
  } else if (type == "start_calibration_mode") {
    unsigned long intervalSeconds = payload["interval_seconds"] | (CALIBRATION_MODE_DEFAULT_INTERVAL_MS / 1000UL);
    unsigned long timeoutSeconds = payload["timeout_seconds"] | (CALIBRATION_MODE_DEFAULT_TIMEOUT_MS / 1000UL);
    startCalibrationMode(intervalSeconds, timeoutSeconds);
    postCommandResult(commandId, true, "Calibration mode started");
  } else if (type == "stop_calibration_mode") {
    stopCalibrationMode("command received");
    postCommandResult(commandId, true, "Calibration mode stopped");
  } else {
    postCommandResult(commandId, false, String("Unknown command: ") + type);
  }
}

void runUploadCycle() {
  debugLine();
  Serial.println("[CYCLE] Starting measurement/upload cycle");

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
  bool currentUploaded = uploadLine(json);

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

  fetchRemoteConfig();
  checkCommands();

  if (shouldCheckOtaThisCycle()) {
    lastOtaCheckMs = millis();
    markOtaChecked();
    checkForOtaUpdate();
  } else {
    Serial.printf("[OTA] Skipping; next scheduled check in %u cycle(s)\n", rtcCyclesUntilOta);
  }

  Serial.println("[CYCLE] Done");
  debugLine();
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  esp_sleep_wakeup_cause_t wakeReason = esp_sleep_get_wakeup_cause();
  bool wokeFromDeepSleep = wakeReason == ESP_SLEEP_WAKEUP_TIMER ||
                            wakeReason == ESP_SLEEP_WAKEUP_EXT0 ||
                            wakeReason == ESP_SLEEP_WAKEUP_EXT1;

  releaseSleepPinHolds();
  pinMode(SETUP_BUTTON_PIN, INPUT_PULLUP);

  rtcBootCount++;

  debugLine();
  Serial.println("Hive Scale ESP32 firmware with provisioning + OTA");
  Serial.printf("Firmware version: %s\n", FIRMWARE_VERSION);
  Serial.printf("Optional modules: INA219=%d MAX17048=%d INMP441=%d\n",
                ENABLE_INA219_SOLAR, ENABLE_MAX17048_BATTERY, ENABLE_INMP441_MICS);
  Serial.printf("Wake reason: %s; RTC boot count: %u\n", wakeReasonName(wakeReason).c_str(), rtcBootCount);
  debugLine();

  seedPrefsFromSecretsIfNeeded();
  loadConfigFromPrefs();

  if (digitalRead(SETUP_BUTTON_PIN) == LOW || wakeReason == ESP_SLEEP_WAKEUP_EXT0) {
    Serial.println("[SETUP] Button wake/press detected; starting provisioning portal");
    initSdCard();
    startProvisioningPortal();
    return;
  }

  Wire.begin(I2C_SDA, I2C_SCL);
  Serial.println("[I2C] Started");

  rtcOk = rtc.begin();
  Serial.printf("[RTC] %s\n", rtcOk ? "OK" : "MISSING");

  if (rtcOk && rtc.lostPower()) {
    Serial.println("[RTC] Lost power");
  }

  shtOk = sht4.begin();
  Serial.printf("[SHT4x] %s\n", shtOk ? "OK" : "MISSING");

  if (shtOk) {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
  }

#if ENABLE_INA219_SOLAR
  solarMonitorOk = solarMonitor.begin(&Wire);
  Serial.printf("[INA219] %s\n", solarMonitorOk ? "OK" : "MISSING");
  if (solarMonitorOk) {
    solarMonitor.setCalibration_32V_2A();
    solarMonitor.powerSave(true);
  }
#endif

#if ENABLE_MAX17048_BATTERY
  batteryMonitorOk = batteryGauge.begin();
  Serial.printf("[MAX17048] %s\n", batteryMonitorOk ? "OK" : "MISSING");
  if (batteryMonitorOk) {
    batteryGauge.quickStart();
    batteryGauge.setThreshold(MAX17048_ALERT_PERCENT);
  }
#endif

  ds18b20.begin();
  Serial.printf("[DS18B20] Device count: %d\n", ds18b20.getDeviceCount());

  scale1.begin(HX1_DOUT, HX1_SCK);
  scale2.begin(HX2_DOUT, HX2_SCK);
  Serial.println("[HX711] Initialized");

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

  enterDeepSleep(sendIntervalMs);
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