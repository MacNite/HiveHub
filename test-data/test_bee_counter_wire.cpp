// test_bee_counter_wire.cpp — host unit test for the HiveTraffic measurement
// document decoder. Builds with plain g++ (no Arduino / NimBLE / ArduinoJson),
// so it runs in CI and on a dev box:
//
//   g++ -std=gnu++17 -I firmware/include test-data/test_bee_counter_wire.cpp -o /tmp/t && /tmp/t
//
// The two documents at the top are the contract. HiveTraffic emits fw:3 as of
// its protocol-v3 revision, but every counter already in a hive keeps emitting
// fw:2 until the OTA relay reaches it — and the relay reads this very
// characteristic before it can update anything. Both must parse, and must parse
// to the same meaning, or the fleet splits in half at deployment time.
#include "bee_counter_wire.h"

#include <cstdio>
#include <cstring>
#include <cstdlib>

using namespace beecnt::wire;

static int g_failures = 0;
static const char* g_case = "";

static void eqi(const char* what, unsigned long got, unsigned long want) {
  const bool ok = got == want;
  std::printf("  %-16s got=%-12lu want=%-12lu %s\n", what, got, want, ok ? "OK" : "FAIL");
  if (!ok) g_failures++;
}

static void eqs(const char* what, const char* got, const char* want) {
  const bool ok = std::strcmp(got, want) == 0;
  std::printf("  %-16s got=%-12s want=%-12s %s\n", what, got, want, ok ? "OK" : "FAIL");
  if (!ok) g_failures++;
}

static void check(const char* what, bool cond) {
  std::printf("  %-16s %s\n", what, cond ? "OK" : "FAIL");
  if (!cond) g_failures++;
}

static Measurement parseOrDie(const char* json) {
  Measurement m;
  if (!parseMeasurement(json, std::strlen(json), m)) {
    std::printf("  [%s] parse FAILED for: %s\n", g_case, json);
    g_failures++;
  }
  return m;
}

// ---------------------------------------------------------------------------
// The captured documents.
// ---------------------------------------------------------------------------

// A counter still on the pre-v3 firmware: "gates_healthy", 16-bit uptime and
// glitch tally. Byte-for-byte the example in HiveTraffic's docs/ble-mode.md as
// it read before the revision.
static const char* kV2 =
    "{\"fw\":2,\"ver\":\"0.1.0\",\"uptime_s\":1234,\"status\":15,"
    "\"num_gates\":24,\"gates_healthy\":3,\"total_in\":100,"
    "\"total_out\":95,\"glitches\":2}";

// The same device after the OTA: "mcps_healthy", 32-bit fields. Same reading,
// so both must decode to identical values apart from protocol_version.
static const char* kV3 =
    "{\"fw\":3,\"ver\":\"0.2.0\",\"uptime_s\":1234,\"status\":15,"
    "\"num_gates\":24,\"mcps_healthy\":3,\"total_in\":100,"
    "\"total_out\":95,\"glitches\":2}";

// The same device again after the night-mode update, counting normally: every
// v3 key unchanged plus idle_s, which is 0 whenever the counter is sensing.
static const char* kV4 =
    "{\"fw\":4,\"ver\":\"0.2.0\",\"uptime_s\":1234,\"status\":15,"
    "\"num_gates\":24,\"mcps_healthy\":3,\"total_in\":100,"
    "\"total_out\":95,\"glitches\":2,\"idle_s\":0}";

// A counter mid-suspension at 02:00: status carries bit 0x80 on top of the
// usual 15, the totals are frozen, and idle_s counts the grant down. This is
// the document that has to be distinguishable from a counter whose emitters
// have failed — which would look identical without the last two fields.
static const char* kV4Idle =
    "{\"fw\":4,\"ver\":\"0.2.0\",\"uptime_s\":50000,\"status\":143,"
    "\"num_gates\":24,\"mcps_healthy\":3,\"total_in\":8123,"
    "\"total_out\":8090,\"glitches\":2,\"idle_s\":1187}";

// ---------------------------------------------------------------------------

static void test_v2_document() {
  g_case = "fw:2";
  std::printf("fw:2 document (counter not yet updated):\n");
  const Measurement m = parseOrDie(kV2);
  eqi("protocol_version", m.protocol_version, 2);
  eqs("version", m.version, "0.1.0");
  eqi("status_flags", m.status_flags, 15);
  eqi("uptime_s", m.uptime_s, 1234);
  eqi("num_gates", m.num_gates, 24);
  eqi("mcps_healthy", m.mcps_healthy, 3);   // read from "gates_healthy"
  eqi("total_in", m.total_in, 100);
  eqi("total_out", m.total_out, 95);
  eqi("glitch_count", m.glitch_count, 2);
}

static void test_v3_document() {
  g_case = "fw:3";
  std::printf("fw:3 document (counter updated):\n");
  const Measurement m = parseOrDie(kV3);
  eqi("protocol_version", m.protocol_version, 3);
  eqs("version", m.version, "0.2.0");
  eqi("status_flags", m.status_flags, 15);
  eqi("uptime_s", m.uptime_s, 1234);
  eqi("num_gates", m.num_gates, 24);
  eqi("mcps_healthy", m.mcps_healthy, 3);   // read from "mcps_healthy"
  eqi("total_in", m.total_in, 100);
  eqi("total_out", m.total_out, 95);
  eqi("glitch_count", m.glitch_count, 2);
}

static void test_v4_document() {
  g_case = "fw:4";
  std::printf("fw:4 document (counter counting):\n");
  const Measurement m = parseOrDie(kV4);
  eqi("protocol_version", m.protocol_version, 4);
  eqi("mcps_healthy", m.mcps_healthy, 3);
  eqi("idle_s", m.idle_s, 0);
  check("not night idle", !isNightIdle(m));
  check("night bit clear", (m.status_flags & STATUS_NIGHT_IDLE) == 0);
}

static void test_v4_suspended_document() {
  // Why the field exists. Totals frozen and no crossings this interval is
  // exactly what a dead emitter bank produces; idle_s and bit 0x80 are the only
  // things separating "deliberately not looking" from "broken".
  g_case = "fw:4 suspended";
  std::printf("fw:4 document (counter in night mode):\n");
  const Measurement m = parseOrDie(kV4Idle);
  eqi("protocol_version", m.protocol_version, 4);
  eqi("idle_s", m.idle_s, 1187);
  eqi("status_flags", m.status_flags, 143);      // 15 | 0x80
  check("is night idle", isNightIdle(m));
  check("night bit set", (m.status_flags & STATUS_NIGHT_IDLE) != 0);
  // The rest of the document still decodes: a suspended counter is still
  // reporting its health, and mcps_healthy staying at 3 is what says the
  // expanders are fine and the silence is deliberate.
  eqi("mcps_healthy", m.mcps_healthy, 3);
  eqi("total_in", m.total_in, 8123);
}

static void test_night_status_bit_matches_the_counter() {
  // Duplicated from HiveTraffic's counter_protocol.h because the two firmwares
  // share no header. If the counter ever moves the bit, this is what catches it
  // — on this side, where the consequence is misreading every suspended device.
  g_case = "status bit 0x80";
  check("STATUS_NIGHT_IDLE is bit 7", STATUS_NIGHT_IDLE == 0x80);
  check("REV_NIGHT_MODE is 4", REV_NIGHT_MODE == 4);
}

static void test_older_counters_report_no_suspension() {
  // A counter that predates night mode cannot be suspended, and must not look
  // suspended: idle_s defaults to 0 and isNightIdle() is false for both older
  // revisions, whatever their status byte happens to carry.
  g_case = "pre-v4 counters";
  const Measurement v2 = parseOrDie(kV2);
  const Measurement v3 = parseOrDie(kV3);
  eqi("v2 idle_s", v2.idle_s, 0);
  eqi("v3 idle_s", v3.idle_s, 0);
  check("v2 not idle", !isNightIdle(v2));
  check("v3 not idle", !isNightIdle(v3));
}

static void test_both_revisions_agree() {
  // The rename carried no change of meaning, so the two documents above
  // describe the same device state. If this ever diverges, a fleet mid-rollout
  // is charting two different quantities under one name.
  g_case = "revisions agree";
  std::printf("both revisions decode to the same reading:\n");
  const Measurement a = parseOrDie(kV2);
  const Measurement b = parseOrDie(kV3);
  check("mcps_healthy", a.mcps_healthy == b.mcps_healthy);
  check("uptime_s", a.uptime_s == b.uptime_s);
  check("totals", a.total_in == b.total_in && a.total_out == b.total_out);
  check("glitches", a.glitch_count == b.glitch_count);
  check("status", a.status_flags == b.status_flags);
  check("num_gates", a.num_gates == b.num_gates);
  check("fw differs", a.protocol_version != b.protocol_version);

  // v4 describes the same device once more: the revision is purely additive, so
  // nothing a v3 consumer already read may have moved underneath it.
  const Measurement c = parseOrDie(kV4);
  check("v3/v4 mcps_healthy", b.mcps_healthy == c.mcps_healthy);
  check("v3/v4 uptime_s", b.uptime_s == c.uptime_s);
  check("v3/v4 totals",
        b.total_in == c.total_in && b.total_out == c.total_out);
  check("v3/v4 glitches", b.glitch_count == c.glitch_count);
  check("v3/v4 status", b.status_flags == c.status_flags);
}

static void test_v3_wide_fields() {
  // The point of the revision: values a v2 counter could not express. 604800 s
  // is a week (a v2 counter clamped at 65535), and the glitch tally now runs to
  // 32 bits instead of pinning at 65535.
  g_case = "wide fields";
  std::printf("fw:3 values past the old 16-bit ceilings:\n");
  const Measurement m = parseOrDie(
      "{\"fw\":3,\"ver\":\"0.2.0\",\"uptime_s\":604800,\"status\":95,"
      "\"num_gates\":24,\"mcps_healthy\":2,\"total_in\":4294967295,"
      "\"total_out\":3000000000,\"glitches\":1000000}");
  eqi("uptime_s", m.uptime_s, 604800);
  eqi("glitch_count", m.glitch_count, 1000000);
  eqi("total_in", m.total_in, 4294967295UL);   // saturated on the device
  eqi("total_out", m.total_out, 3000000000UL);
  eqi("mcps_healthy", m.mcps_healthy, 2);      // one expander has dropped out
}

static void test_out_of_range_saturates() {
  // A value wider than 32 bits should pin at the maximum, not wrap. Wrapping
  // would turn a saturated lifetime total into a small number, which the
  // server's interval differencing reads as a counter reboot.
  g_case = "saturation";
  std::printf("oversized integers saturate rather than wrap:\n");
  const Measurement m = parseOrDie(
      "{\"fw\":3,\"uptime_s\":99999999999999,\"total_in\":18446744073709551615,"
      "\"mcps_healthy\":999,\"glitches\":4294967296}");
  eqi("uptime_s", m.uptime_s, 4294967295UL);
  eqi("total_in", m.total_in, 4294967295UL);
  eqi("glitch_count", m.glitch_count, 4294967295UL);
  eqi("mcps_healthy", m.mcps_healthy, 255);   // clamped into the uint8_t
}

static void test_missing_fields_keep_defaults() {
  // A counter too old to report "ver" must not be mistaken for one reporting an
  // empty string: the OTA version gate treats an unknown version as "never
  // block", which is only correct for the former.
  g_case = "missing fields";
  std::printf("absent fields keep their defaults:\n");
  const Measurement m = parseOrDie("{\"fw\":2,\"total_in\":7,\"total_out\":9}");
  eqs("version", m.version, "");
  eqi("uptime_s", m.uptime_s, 0);
  eqi("mcps_healthy", m.mcps_healthy, 0);
  eqi("total_in", m.total_in, 7);
  eqi("total_out", m.total_out, 9);
}

static void test_unknown_fields_are_skipped() {
  // A later revision that only ADDS fields must still parse here, including one
  // that nests — an unknown object has to be skipped whole or it desynchronizes
  // the key scan and silently corrupts every field after it.
  g_case = "forward compat";
  std::printf("unknown and nested fields are skipped:\n");
  const Measurement m = parseOrDie(
      "{\"fw\":4,\"ver\":\"0.3.0\",\"per_gate\":[1,2,3],"
      "\"detail\":{\"a\":1,\"b\":{\"c\":\"}\"}},\"uptime_s\":42,"
      "\"mcps_healthy\":3,\"total_in\":5,\"note\":\"hello\",\"total_out\":6}");
  eqi("protocol_version", m.protocol_version, 4);
  eqs("version", m.version, "0.3.0");
  eqi("uptime_s", m.uptime_s, 42);
  eqi("mcps_healthy", m.mcps_healthy, 3);
  eqi("total_in", m.total_in, 5);
  eqi("total_out", m.total_out, 6);
}

static void test_whitespace_is_tolerated() {
  g_case = "whitespace";
  std::printf("whitespace between tokens:\n");
  const Measurement m = parseOrDie(
      "  { \"fw\" : 3 , \"ver\" : \"1.0.0\" , \"uptime_s\" : 7 , "
      "\"mcps_healthy\" : 1 }  ");
  eqi("protocol_version", m.protocol_version, 3);
  eqs("version", m.version, "1.0.0");
  eqi("uptime_s", m.uptime_s, 7);
  eqi("mcps_healthy", m.mcps_healthy, 1);
}

static void test_long_version_is_truncated_not_overrun() {
  g_case = "long version";
  std::printf("an over-long version string is truncated safely:\n");
  const Measurement m = parseOrDie(
      "{\"fw\":3,\"ver\":\"0.1.0-verylongsuffix-that-overflows\"}");
  check("fits the buffer", std::strlen(m.version) == sizeof(m.version) - 1);
  eqs("version", m.version, "0.1.0-verylongs");   // 15 chars + NUL
}

static void test_malformed_documents_are_refused() {
  // Returning defaults for a document we did not understand would report a
  // healthy counter that has never seen a bee. Refusing lets the caller mark
  // the read failed instead.
  g_case = "malformed";
  std::printf("malformed input is refused:\n");
  const char* bad[] = {
    "",                                        // empty read
    "not json at all",
    "[1,2,3]",                                 // not an object
    "{\"fw\":3",                               // truncated: no closing brace
    "{\"fw\":3,\"ver\":\"0.1.0",               // truncated inside a string
    "{\"ver\":\"0.1.0\",\"total_in\":5}",      // no "fw": not a measurement
    "{}",                                      // ditto
  };
  for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
    Measurement m;
    const bool parsed = parseMeasurement(bad[i], std::strlen(bad[i]), m);
    std::printf("  %-42s refused=%s %s\n", bad[i][0] ? bad[i] : "(empty)",
                parsed ? "no" : "yes", parsed ? "FAIL" : "OK");
    if (parsed) g_failures++;
  }
  // A null buffer must not be dereferenced.
  Measurement m;
  check("null buffer", !parseMeasurement(nullptr, 10, m));
}

static void test_mismatched_revision_and_field_name() {
  // Belt and braces for a partially-deployed rename: a counter that bumped
  // "fw" without renaming the field (or renamed it without bumping "fw") is
  // still read correctly rather than reported as zero healthy expanders, which
  // would look like a completely dead device.
  g_case = "mismatch";
  std::printf("revision and field name disagree:\n");
  const Measurement a = parseOrDie("{\"fw\":3,\"gates_healthy\":2}");
  eqi("v3 + old name", a.mcps_healthy, 2);
  const Measurement b = parseOrDie("{\"fw\":2,\"mcps_healthy\":1}");
  eqi("v2 + new name", b.mcps_healthy, 1);
  // When both are present the revision decides.
  const Measurement c = parseOrDie("{\"fw\":3,\"gates_healthy\":9,\"mcps_healthy\":3}");
  eqi("v3 prefers new", c.mcps_healthy, 3);
  const Measurement d = parseOrDie("{\"fw\":2,\"gates_healthy\":3,\"mcps_healthy\":9}");
  eqi("v2 prefers old", d.mcps_healthy, 3);
}

int main() {
  test_v2_document();
  test_v3_document();
  test_v4_document();
  test_v4_suspended_document();
  test_night_status_bit_matches_the_counter();
  test_older_counters_report_no_suspension();
  test_both_revisions_agree();
  test_v3_wide_fields();
  test_out_of_range_saturates();
  test_missing_fields_keep_defaults();
  test_unknown_fields_are_skipped();
  test_whitespace_is_tolerated();
  test_long_version_is_truncated_not_overrun();
  test_malformed_documents_are_refused();
  test_mismatched_revision_and_field_name();

  if (g_failures) {
    std::printf("\n%d check(s) FAILED\n", g_failures);
    return EXIT_FAILURE;
  }
  std::printf("\nall checks passed\n");
  return EXIT_SUCCESS;
}
