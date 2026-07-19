// test_sht4x_measure.cpp — host-level tests for the checked native SHT4x
// measurement path (sht4x_measure.h): the Sensirion CRC-8, the six-byte response
// validation + conversion, humidity clamping, and the full transaction sequence
// against a scripted mock bus (short read, CRC corruption, valid frame). No
// Arduino toolchain needed. Build & run:
//   g++ -std=gnu++17 -I../include -o test_sht4x_measure test_sht4x_measure.cpp && ./test_sht4x_measure
#include <cstdio>
#include <cmath>
#include <cstdint>
#include <vector>

#include "sht4x_measure.h"

static int gFailures = 0;
static int gChecks = 0;

#define CHECK(cond) do { \
    gChecks++; \
    if (!(cond)) { gFailures++; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

// ── Scripted SHT4x bus mock (duck-typed to the I2cBusIface shape) ────────────
// Delivers a configurable response; `receivedOverride` lets a test make
// requestFrom() claim a byte count that differs from the buffer, modelling a
// short read where the driver reports fewer bytes than requested.
struct MockShtBus {
  std::vector<uint8_t> response;   // bytes the sensor returns
  int      receivedOverride = -1;  // <0 = report response.size(); else this
  uint8_t  endStatus        = 0;   // endTransmission() result
  bool     writeOk          = true;
  uint32_t waitedMs         = 0;

  std::vector<uint8_t> buf;        // deliverable queue for available()/read()
  size_t   pos = 0;

  void beginTransmission(uint8_t) {}
  size_t write(uint8_t) { return writeOk ? 1 : 0; }
  uint8_t endTransmission() { return endStatus; }
  uint8_t requestFrom(uint8_t, uint8_t) {
    buf = response;
    pos = 0;
    return (uint8_t)(receivedOverride >= 0 ? receivedOverride : (int)response.size());
  }
  int available() { return (int)(buf.size() - pos); }
  int read() { return pos < buf.size() ? buf[pos++] : -1; }
  uint32_t millisMs() { return 0; }
  void delayMs(uint32_t ms) { waitedMs += ms; }
};

// Build a valid six-byte frame from raw T and RH words, filling both CRC bytes.
static std::vector<uint8_t> makeFrame(uint16_t rawT, uint16_t rawH) {
  std::vector<uint8_t> f(6);
  f[0] = (uint8_t)(rawT >> 8); f[1] = (uint8_t)(rawT & 0xFF);
  f[2] = sht4xmeas::crc8(&f[0], 2);
  f[3] = (uint8_t)(rawH >> 8); f[4] = (uint8_t)(rawH & 0xFF);
  f[5] = sht4xmeas::crc8(&f[3], 2);
  return f;
}

// ── CRC known-answer vectors ─────────────────────────────────────────────────
static void testCrcKnownVector() {
  const uint8_t beef[2] = {0xBE, 0xEF};   // Sensirion datasheet vector
  CHECK(sht4xmeas::crc8(beef, 2) == 0x92);

  // A single 0x00 byte with the 0xFF seed and 0x31 poly is a stable extra check.
  const uint8_t zero = 0x00;
  CHECK(sht4xmeas::crc8(&zero, 1) == 0xAC);
}

// ── Valid six-byte response over the full transaction ────────────────────────
static void testValidResponse() {
  MockShtBus bus;
  bus.response = makeFrame(0x6666, 0x8000);   // ~25.0 C, ~56.5 %RH
  sht4xmeas::Result r = sht4xmeas::measure(bus);

  CHECK(r.ok);
  CHECK(r.commandWritten && r.endTransmission == 0);
  CHECK(r.haveRaw && r.readCount == 6);
  CHECK(r.received == 6 && r.available == 6);
  CHECK(r.tempCrcOk && r.humidityCrcOk);
  CHECK(bus.waitedMs == sht4xmeas::MEASURE_WAIT_MS);   // 25 ms conversion wait
  CHECK(std::fabs(r.tempC - 25.0f) < 0.1f);
  CHECK(std::fabs(r.humidity - 56.5f) < 0.1f);
}

// ── Command NACK ─────────────────────────────────────────────────────────────
static void testCommandNack() {
  MockShtBus bus;
  bus.endStatus = 2;                 // address NACK on the command
  bus.response = makeFrame(0x6666, 0x8000);
  sht4xmeas::Result r = sht4xmeas::measure(bus);
  CHECK(!r.ok);
  CHECK(r.endTransmission == 2);
  CHECK(!r.haveRaw);
  CHECK(bus.waitedMs == 0);          // never waited/read after the failed command
}

// ── Bad temperature CRC ──────────────────────────────────────────────────────
static void testBadTemperatureCrc() {
  MockShtBus bus;
  bus.response = makeFrame(0x6666, 0x8000);
  bus.response[2] ^= 0xFF;           // corrupt the temperature checksum
  sht4xmeas::Result r = sht4xmeas::measure(bus);
  CHECK(!r.ok);
  CHECK(r.haveRaw);                  // six bytes did arrive
  CHECK(!r.tempCrcOk);
  CHECK(r.humidityCrcOk);            // humidity checksum still valid
}

// ── Bad humidity CRC ─────────────────────────────────────────────────────────
static void testBadHumidityCrc() {
  MockShtBus bus;
  bus.response = makeFrame(0x6666, 0x8000);
  bus.response[5] ^= 0xFF;           // corrupt the humidity checksum
  sht4xmeas::Result r = sht4xmeas::measure(bus);
  CHECK(!r.ok);
  CHECK(r.haveRaw);
  CHECK(r.tempCrcOk);
  CHECK(!r.humidityCrcOk);
}

// ── Short response ───────────────────────────────────────────────────────────
static void testShortResponse() {
  {   // requestFrom reports 3 bytes, only 3 delivered
    MockShtBus bus;
    bus.response = {0x66, 0x66, 0x93};   // 3 partial bytes
    sht4xmeas::Result r = sht4xmeas::measure(bus);
    CHECK(!r.ok && !r.haveRaw);
    CHECK(r.received == 3);
    CHECK(r.readCount == 3);              // only the bytes that arrived
  }
  {   // zero-byte response (sensor NACKed the read entirely)
    MockShtBus bus;
    bus.response = {};
    sht4xmeas::Result r = sht4xmeas::measure(bus);
    CHECK(!r.ok && !r.haveRaw);
    CHECK(r.received == 0 && r.readCount == 0);
  }
  {   // requestFrom claims 6 but only 4 bytes are actually available
    MockShtBus bus;
    bus.response = {0x66, 0x66, 0x93, 0x80};
    bus.receivedOverride = 6;
    sht4xmeas::Result r = sht4xmeas::measure(bus);
    CHECK(!r.ok && !r.haveRaw);
    CHECK(r.received == 6);
    CHECK(r.readCount == 4);              // read only what the bus actually had
  }
}

// ── Humidity clamping (via the pure parse()) ─────────────────────────────────
static void testHumidityClamp() {
  float t = NAN, h = NAN;
  bool tc = false, hc = false;

  // rawH = 0xFFFF -> -6 + 125*1.0 = 119 % -> clamps to 100.
  std::vector<uint8_t> hi = makeFrame(0x6666, 0xFFFF);
  CHECK(sht4xmeas::parse(hi.data(), t, h, tc, hc));
  CHECK(tc && hc);
  CHECK(h == 100.0f);

  // rawH = 0x0000 -> -6 + 0 = -6 % -> clamps to 0.
  std::vector<uint8_t> lo = makeFrame(0x6666, 0x0000);
  CHECK(sht4xmeas::parse(lo.data(), t, h, tc, hc));
  CHECK(tc && hc);
  CHECK(h == 0.0f);
}

int main() {
  testCrcKnownVector();
  testValidResponse();
  testCommandNack();
  testBadTemperatureCrc();
  testBadHumidityCrc();
  testShortResponse();
  testHumidityClamp();

  std::printf("%d checks, %d failure(s)\n", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
