"""
Sample nRF54LM20A HiveInside beacon frame + reference decoder.

The XIAO nRF54LM20A Sense HiveInside node advertises the same 26-byte
manufacturer-data frame the HiveHub firmware already decodes in
firmware/src/ble_sensor.cpp (parseHiveInside). This module carries a known-good
frame and a tiny reference decoder so:

  * the mock server can emit a realistic HiveInside beacon, and
  * decoder changes have a host-side unit test (parseHiveInside itself is a
    static C++ function, so this Python mirror is the executable spec).

Run: python3 hiveinside_nrf54_beacon.py

Frame layout (little-endian for the sensor fields; company id is LE too):

    off 0..1  : company id (LE)        == 0x02E5 (Espressif; kept so existing
                                          HiveHubs decode the Nordic node unchanged)
    off 2     : magic                   == 0x48 ('H')
    off 3     : version                 == 0x01
    off 4     : flags  bit0 sht bit1 accel bit2 mic bit3 batt
    off 5..6  : temperature  int16 LE, 0.1 °C   (valid iff flags bit0)
    off 7..8  : humidity      uint16 LE, 0.1 %RH (valid iff flags bit0)
    off 9..10 : battery       uint16 LE, milli-volt (valid iff flags bit3)
    off 11    : battery percent (uint8)             (valid iff flags bit3)
    off 12..13: accel RMS      uint16 LE, 0.1 mg     (valid iff flags bit1)
    off 14..15: accel band swarm    uint16 LE, 0.1 mg
    off 16..17: accel band fanning  uint16 LE, 0.1 mg
    off 18..19: accel band activity uint16 LE, 0.1 mg
    off 20    : mic RMS        int8, dBFS           (valid iff flags bit2)
    off 21    : mic sub-bass   int8, dBFS
    off 22    : mic hum        int8, dBFS
    off 23    : mic piping     int8, dBFS
    off 24    : mic stress     int8, dBFS
    off 25    : mic high       int8, dBFS
"""

import struct

HI_COMPANY_ID = 0x02E5
HI_MAGIC = 0x48
HI_VERSION = 0x01

FLAG_SHT = 1 << 0
FLAG_ACCEL = 1 << 1
FLAG_MIC = 1 << 2
FLAG_BATT = 1 << 3

# A full frame: all four sensor groups present (flags = 0x0F).
#   temp = 34.5 °C, humidity = 60.0 %RH, battery = 3700 mV / 92 %,
#   accel RMS = 12.0 mg (bands 8.0 / 5.0 / 3.0 mg),
#   mic RMS = -42 dBFS (bands -55 / -48 / -60 / -58 / -66 dBFS).
SAMPLE_FRAME_FULL = bytes([
    0xE5, 0x02,        # company id 0x02E5
    0x48,              # magic 'H'
    0x01,              # version
    0x0F,              # flags: sht | accel | mic | batt
    0x59, 0x01,        # temp 345 -> 34.5 °C
    0x58, 0x02,        # humidity 600 -> 60.0 %RH
    0x74, 0x0E,        # battery 3700 mV
    0x5C,              # battery 92 %
    0x78, 0x00,        # accel RMS 120 -> 12.0 mg
    0x50, 0x00,        # swarm 80 -> 8.0 mg
    0x32, 0x00,        # fanning 50 -> 5.0 mg
    0x1E, 0x00,        # activity 30 -> 3.0 mg
    0xD6,              # mic RMS -42 dBFS
    0xC9,              # sub-bass -55
    0xD0,              # hum -48
    0xC4,              # piping -60
    0xC6,              # stress -58
    0xBE,              # high -66
])

# Same node with a failed SHT40 this cycle (flags clears bit0): the temp/humidity
# bytes are stale zeros and MUST decode as "absent", not 0.0 °C.
SAMPLE_FRAME_NO_SHT = bytes([
    0xE5, 0x02, 0x48, 0x01,
    0x0E,              # flags: accel | mic | batt  (sht cleared)
    0x00, 0x00,        # temp bytes present but invalid
    0x00, 0x00,        # humidity bytes present but invalid
    0x74, 0x0E, 0x5C,
    0x78, 0x00, 0x50, 0x00, 0x32, 0x00, 0x1E, 0x00,
    0xD6, 0xC9, 0xD0, 0xC4, 0xC6, 0xBE,
])


def decode(frame: bytes) -> dict:
    """Reference decoder mirroring parseHiveInside(). Returns a dict of the
    fields present this cycle; groups whose flag bit is clear are omitted."""
    if len(frame) < 26:
        raise ValueError("frame too short")
    company = struct.unpack_from("<H", frame, 0)[0]
    if company != HI_COMPANY_ID:
        raise ValueError(f"unexpected company id 0x{company:04X}")
    if frame[2] != HI_MAGIC:
        raise ValueError(f"unexpected magic 0x{frame[2]:02X}")

    flags = frame[4]
    out = {"version": frame[3], "flags": flags}

    if flags & FLAG_SHT:
        out["temp_c"] = struct.unpack_from("<h", frame, 5)[0] * 0.1
        out["humidity_pct"] = struct.unpack_from("<H", frame, 7)[0] * 0.1
    if flags & FLAG_BATT:
        out["battery_mv"] = struct.unpack_from("<H", frame, 9)[0]
        out["battery_pct"] = frame[11]
    if flags & FLAG_ACCEL:
        out["accel_rms_mg"] = struct.unpack_from("<H", frame, 12)[0] * 0.1
        out["accel_band_swarm_mg"] = struct.unpack_from("<H", frame, 14)[0] * 0.1
        out["accel_band_fanning_mg"] = struct.unpack_from("<H", frame, 16)[0] * 0.1
        out["accel_band_activity_mg"] = struct.unpack_from("<H", frame, 18)[0] * 0.1
    if flags & FLAG_MIC:
        out["mic_rms_dbfs"] = struct.unpack_from("<b", frame, 20)[0]
        out["mic_sub_bass_dbfs"] = struct.unpack_from("<b", frame, 21)[0]
        out["mic_hum_dbfs"] = struct.unpack_from("<b", frame, 22)[0]
        out["mic_piping_dbfs"] = struct.unpack_from("<b", frame, 23)[0]
        out["mic_stress_dbfs"] = struct.unpack_from("<b", frame, 24)[0]
        out["mic_high_dbfs"] = struct.unpack_from("<b", frame, 25)[0]
    return out


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _main() -> None:
    full = decode(SAMPLE_FRAME_FULL)
    assert len(SAMPLE_FRAME_FULL) == 26, "frame must be exactly 26 bytes"
    assert _approx(full["temp_c"], 34.5), full
    assert _approx(full["humidity_pct"], 60.0), full
    assert full["battery_mv"] == 3700, full
    assert full["battery_pct"] == 92, full
    assert _approx(full["accel_rms_mg"], 12.0), full
    assert _approx(full["accel_band_swarm_mg"], 8.0), full
    assert full["mic_rms_dbfs"] == -42, full
    assert full["mic_high_dbfs"] == -66, full
    print("  [PASS] full frame decodes all four groups")

    # A failed SHT40 must map to absent, NOT 0.0 °C (regression for the flags fix).
    no_sht = decode(SAMPLE_FRAME_NO_SHT)
    assert "temp_c" not in no_sht, no_sht
    assert "humidity_pct" not in no_sht, no_sht
    assert no_sht["battery_mv"] == 3700, no_sht
    assert _approx(no_sht["accel_rms_mg"], 12.0), no_sht
    print("  [PASS] cleared SHT flag reports temperature/humidity as absent")

    print("All HiveInside nRF54 beacon decode tests passed.")


if __name__ == "__main__":
    _main()
