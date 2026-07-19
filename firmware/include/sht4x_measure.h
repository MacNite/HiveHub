// sht4x_measure.h — a CHECKED native SHT4x high-precision measurement, factored
// out of sensors.cpp so the CRC, response validation, unit conversion and the
// full transaction sequence can all run against a scripted mock in the host test
// suite (firmware/host_test/) exactly as they run on the device.
//
// WHY this exists (see docs/… and the i2c-debug field log): with the Adafruit
// SHT4x 1.0.4 getEvent() path the firmware could see sht4.begin() ACK the sensor
// yet every measurement come back as a bare boolean "false" — no way to tell a
// command NACK from a short read from a CRC-corrupted frame. This talks to the
// SHT4x directly through the shared checked I2C interface and returns a full
// per-stage diagnostic record instead, so the next hardware log distinguishes:
//   * command NACK (endTransmission != 0);
//   * short / zero-byte response (received/available < 6);
//   * CRC corruption (temperature and/or humidity checksum bad);
//   * a valid measurement.
//
// Deliberately Arduino-free (only <stdint.h>/<stddef.h>): the transaction is a
// template over any object exposing the hivei2c::I2cBusIface shape
// (beginTransmission/write/endTransmission/requestFrom/available/read/delayMs),
// so on the device it runs against the Wire-backed adapter (i2cbus::iface()) —
// whose endTransmission() issues a STOP, i.e. the Wire.endTransmission(true)
// the SHT4x needs between the command and the read-back — and against a scripted
// mock in the host tests. The pure crc8()/parse() helpers are usable on their
// own for the CRC known-answer vectors.
#pragma once

#include <stdint.h>
#include <stddef.h>

namespace sht4xmeas {

// ── SHT4x measurement protocol constants ─────────────────────────────────────
constexpr uint8_t  ADDRESS         = 0x44;   // SHT4x default I2C address
constexpr uint8_t  CMD_MEASURE_HI  = 0xFD;   // high precision, no heater
constexpr uint8_t  RESPONSE_BYTES  = 6;      // T[msb,lsb,crc] + RH[msb,lsb,crc]
constexpr uint32_t MEASURE_WAIT_MS = 25;     // high-precision conversion time
constexpr uint8_t  CRC_INIT        = 0xFF;   // Sensirion CRC-8 seed
constexpr uint8_t  CRC_POLY        = 0x31;   // Sensirion CRC-8 polynomial

// Sensirion CRC-8 (init 0xFF, polynomial 0x31, MSB-first, no final XOR) over the
// two data bytes that precede each checksum byte in an SHT4x response.
inline uint8_t crc8(const uint8_t* data, size_t len) {
  uint8_t crc = CRC_INIT;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ CRC_POLY) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// Per-stage record of one measurement attempt — every field a diagnostic log
// wants, so the caller can name the exact failing stage without re-deriving it.
struct Result {
  bool    ok              = false;             // BOTH CRCs valid; tempC/humidity set
  bool    commandWritten  = false;             // the 0xFD byte was buffered/written
  uint8_t endTransmission = 0xFF;              // endTransmission() status (0 = ACK)
  uint8_t requested       = RESPONSE_BYTES;    // bytes asked for
  uint8_t received        = 0;                 // requestFrom() return value
  int     available       = 0;                 // available() right after requestFrom()
  uint8_t readCount       = 0;                 // bytes actually clocked into raw[]
  uint8_t raw[RESPONSE_BYTES] = {0};           // response bytes (only [0,readCount) valid)
  bool    haveRaw         = false;             // all six bytes were read
  bool    tempCrcOk       = false;
  bool    humidityCrcOk   = false;
  float   tempC           = 0.0f;
  float   humidity        = 0.0f;
};

// Validate both CRCs and convert a full six-byte response. Fills tempC/humidity
// (humidity clamped to 0–100 %) and the two CRC flags regardless of CRC outcome
// so callers can log the converted values; returns true only when BOTH CRCs pass.
inline bool parse(const uint8_t raw[RESPONSE_BYTES], float& tempC, float& humidity,
                  bool& tempCrcOk, bool& humidityCrcOk) {
  tempCrcOk     = crc8(&raw[0], 2) == raw[2];
  humidityCrcOk = crc8(&raw[3], 2) == raw[5];

  const uint16_t rawTemperature = (uint16_t)(((uint16_t)raw[0] << 8) | raw[1]);
  const uint16_t rawHumidity    = (uint16_t)(((uint16_t)raw[3] << 8) | raw[4]);

  tempC    = -45.0f + 175.0f * (float)rawTemperature / 65535.0f;
  humidity =  -6.0f + 125.0f * (float)rawHumidity    / 65535.0f;

  if (humidity < 0.0f)   humidity = 0.0f;    // RH is physically bounded; the raw
  if (humidity > 100.0f) humidity = 100.0f;  // conversion can overshoot slightly

  return tempCrcOk && humidityCrcOk;
}

// Run one full checked measurement over `bus`. The sequence mirrors the SHT4x
// datasheet exactly: START to 0x44, write 0xFD, STOP (endTransmission), wait
// 25 ms for the high-precision conversion, request 6 bytes, read them, validate
// both CRCs and convert. Never averages or trusts a partial/NACKed transfer —
// any failed stage returns a Result with .ok == false and the stage recorded.
template <typename Bus>
Result measure(Bus& bus) {
  Result r;

  // 1–4. Send the measurement command and check the STOP was ACKed.
  bus.beginTransmission(ADDRESS);
  r.commandWritten  = bus.write(CMD_MEASURE_HI) == 1;
  r.endTransmission = bus.endTransmission();   // STOP condition (Wire: stop=true)
  if (!r.commandWritten || r.endTransmission != 0) return r;

  // 5. Let the high-precision conversion finish before reading it back.
  bus.delayMs(MEASURE_WAIT_MS);

  // 6–7. Request exactly six bytes and record what the bus actually offered.
  r.received  = bus.requestFrom(ADDRESS, RESPONSE_BYTES);
  r.available = bus.available();

  // 8. Read up to six bytes; only [0, readCount) of raw[] is ever meaningful, so
  //    a short-response log can print exactly the bytes that arrived and never
  //    uninitialized buffer contents.
  const uint8_t want = r.received < RESPONSE_BYTES ? r.received : RESPONSE_BYTES;
  uint8_t got = 0;
  for (; got < want; got++) {
    if (bus.available() <= 0) break;
    r.raw[got] = (uint8_t)bus.read();
  }
  r.readCount = got;

  // Drain any leftover bytes so a subsequent transfer starts clean.
  while (bus.available() > 0) (void)bus.read();

  if (r.received != RESPONSE_BYTES || r.readCount != RESPONSE_BYTES) {
    return r;  // short / zero-byte response — haveRaw stays false
  }
  r.haveRaw = true;

  // 9–10. Validate CRCs and convert.
  r.ok = parse(r.raw, r.tempC, r.humidity, r.tempCrcOk, r.humidityCrcOk);
  return r;
}

}  // namespace sht4xmeas
