// scale_bus.cpp — HX711 + NAU7802(+TCA9548A) raw reads for the scale registry.
#include "scale_bus.h"
#include "globals.h"
#include "config.h"

#include <Wire.h>
#if ENABLE_HX711
#include <HX711.h>
#endif

#if ENABLE_NAU7802
#include "SparkFun_Qwiic_Scale_NAU7802_Arduino_Library.h"
#endif

namespace scalebus {

using hivecfg::ScaleBackend;
using hivecfg::ScaleChannel;

static bool gMuxPresent = false;
// One init flag per physical NAU7802 chip: index 0..7 = mux channel, 8 = direct
// (no mux). Reset each begin() so a fresh wake re-configures every chip.
static bool gNauInited[9];

static int chipKey(const ScaleChannel& ch) {
  return (ch.muxChannel < 0) ? 8 : (ch.muxChannel & 0x07);
}

bool muxPresent() { return gMuxPresent; }

void muxSelect(uint8_t channel) {
#if ENABLE_I2C_MUX
  if (!gMuxPresent) return;
  Wire.beginTransmission(TCA9548A_I2C_ADDRESS);
  Wire.write((uint8_t)(1 << (channel & 0x07)));
  Wire.endTransmission();
#else
  (void)channel;
#endif
}

void muxDisableAll() {
#if ENABLE_I2C_MUX
  if (!gMuxPresent) return;
  Wire.beginTransmission(TCA9548A_I2C_ADDRESS);
  Wire.write((uint8_t)0x00);
  Wire.endTransmission();
#endif
}

// Route the bus to the chip that owns `ch`: enable its mux channel, or open all
// channels for a chip wired directly on the main bus.
static void routeTo(const ScaleChannel& ch) {
  if (ch.muxChannel >= 0) muxSelect((uint8_t)ch.muxChannel);
  else                    muxDisableAll();
}

#if ENABLE_NAU7802
// Configure one NAU7802 (already routed on the bus). begin() resets, powers up,
// sets the 3.3 V LDO, gain 128, 80 SPS and runs the internal AFE calibration.
static bool nauConfigure() {
  if (!nau.begin(Wire)) return false;
  nau.setGain(NAU7802_GAIN_128);
  nau.setSampleRate(NAU7802_SPS_80);
  nau.calibrateAFE();
  return true;
}

// Averaged raw read with a short settling discard after an input/mux switch.
static long nauAverage(uint8_t samples) {
  const uint8_t discard = 2;
  long total = 0;
  int got = 0, seen = 0;
  uint32_t start = millis();
  while (got < samples && (millis() - start) < 2000UL) {
    if (!nau.available()) { delay(1); continue; }
    int32_t r = nau.getReading();
    if (seen++ < discard) continue;  // let the channel mux settle
    total += r;
    got++;
  }
  return got ? (total / got) : 0;
}
#endif  // ENABLE_NAU7802

void begin() {
  for (int i = 0; i < 9; i++) gNauInited[i] = false;

#if ENABLE_I2C_MUX
  Wire.beginTransmission(TCA9548A_I2C_ADDRESS);
  gMuxPresent = (Wire.endTransmission() == 0);
  Serial.printf("[SCALEBUS] TCA9548A %s\n", gMuxPresent ? "detected" : "absent");
#else
  gMuxPresent = false;
#endif

#if ENABLE_NAU7802
  // Configure each unique NAU7802 chip referenced by the registry exactly once.
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802) continue;
      int key = chipKey(ch);
      if (gNauInited[key]) continue;
      routeTo(ch);
      bool ok = nauConfigure();
      gNauInited[key] = ok;
      Serial.printf("[SCALEBUS] NAU7802 mux=%d %s\n", ch.muxChannel, ok ? "OK" : "MISSING");
    }
  }
  muxDisableAll();
#endif
}

long readRaw(const ScaleChannel& ch) {
#if ENABLE_HX711
  if (ch.backend == ScaleBackend::HX711) {
    HX711& hx = (ch.hxIndex == 0) ? scale1 : scale2;
    if (!hx.wait_ready_timeout(2000)) {
      Serial.println("[SCALEBUS] HX711 not ready");
      return 0;
    }
    return hx.read_average(15);
  }
#endif

#if ENABLE_NAU7802
  if (ch.backend == ScaleBackend::NAU7802) {
    int key = chipKey(ch);
    routeTo(ch);
    if (!gNauInited[key]) {
      // Lazy (re)configure if begin() missed it (e.g. chip hot-plugged).
      gNauInited[key] = nauConfigure();
      if (!gNauInited[key]) return 0;
    }
    nau.setChannel(ch.adcChannel == 2 ? NAU7802_CHANNEL_2 : NAU7802_CHANNEL_1);
    long raw = nauAverage(NAU7802_SAMPLES);
    muxDisableAll();
    return raw;
  }
#endif

  return 0;
}

void powerDownAllForSleep() {
#if ENABLE_NAU7802
  bool done[9] = {false, false, false, false, false, false, false, false, false};
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802) continue;
      int key = chipKey(ch);
      if (done[key]) continue;
      done[key] = true;
      routeTo(ch);
      nau.powerDown();   // clears PU_CTRL PUD/PUA — ~microamp standby
    }
  }
  muxDisableAll();
#endif
}

}  // namespace scalebus
