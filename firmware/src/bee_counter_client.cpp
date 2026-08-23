// bee_counter_client.cpp — HiveTraffic bee counter over BLE/GATT (totals-only).
// See bee_counter_client.h: the wired I2C BeeCounter path no longer exists.

#include "bee_counter_client.h"

#if ENABLE_WIRELESS_BEECOUNTER
#include <string.h>       // strlcpy for the counter's reported version
#include <NimBLEDevice.h>
#include "ble_stack.h"
#include "bee_counter_wire.h"   // the tolerant fw:2/3/4 document decoder
#include "hive_config.h"
#include "night_mode.h"
#include "sensors.h"            // localMinuteOfDay()
#include "globals.h"            // the night-mode config + RTC totals baseline
#endif

namespace beecnt {

void writeSnapshotToHive(JsonObject hive, const Snapshot& snap) {
    // Nested per-hive form for the hives[] array (server maps it onto the
    // hive_readings bee_counter_* columns). Only hives with a configured
    // counter get this object at all; ok:false means "paired but unreachable
    // this cycle". Totals-only: the backend differences consecutive totals
    // into intervals, so interval_*/per_gate_* fields are never emitted.
    JsonObject bc = hive["bee_counter"].to<JsonObject>();
    bc["ok"] = snap.present;
    if (!snap.present) return;

    bc["protocol_version"] = snap.fw_version;   // key kept for server compat
    // Image version, when the counter reports one. Omitted rather than sent
    // empty so the server can tell "counter too old to report a version" from
    // "counter reported an empty string" — the OTA version gate treats an
    // unknown version as "never block", which is only correct for the former.
    //
    // Wrapped in String() to force a COPY into the document. ArduinoJson stores
    // a const char* by pointer and duplicates only String / char* / Flash
    // strings, and Snapshot::version is a char[] — so the plain assignment
    // linked the document to a buffer belonging to buildMeasurementDoc()'s
    // `beeSnaps` stack array, which is destroyed when that function returns.
    // The document is serialized later, from finalizeMeasurementJson() and with
    // initSdCard() in between (see runUploadCycle in main.cpp), so what got
    // uploaded was whatever the reclaimed frame happened to hold: "0.1.1" came
    // out truncated ("0.", "0.1", occasionally worse). That is invisible on the
    // wire but poisons the OTA gate — server/firmware.py::parse_version reads
    // "0." as 0.0, so the counter looked ancient no matter how often it was
    // updated and HiveHub re-relayed the same image every cycle, each relay
    // costing the counter minutes of not counting bees. Every other field
    // written here is a number, copied by value, which is why only the version
    // was affected.
    if (snap.version[0]) bc["version"] = String(snap.version);
    bc["status_flags"]     = snap.status_flags;
    bc["uptime_s"]         = snap.uptime_s;
    bc["num_gates"]        = snap.num_gates;
    // Always emitted under the revision-3 name, whichever name the counter used
    // on the wire: the value has the same meaning in both revisions (MCP23017
    // expanders answering, 0..3), and normalizing here means the server and
    // anything reading history sees one key rather than having to branch on
    // protocol_version too. Readings taken before this change carry the old
    // "gates_healthy" key in hive_readings.raw_json, with the same meaning.
    bc["mcps_healthy"]     = snap.mcps_healthy;
    bc["total_in"]         = snap.total_in;
    bc["total_out"]        = snap.total_out;
    bc["glitch_count"]     = snap.glitch_count;
    // Emitted unconditionally, including the 0 a counting counter reports and
    // the 0 a pre-v4 counter implies. Omitting it when zero would make "this
    // counter cannot be suspended" and "this counter is counting right now"
    // the same absence in stored history, and the whole reason the field
    // exists is to tell a deliberate flat interval from a broken one.
    bc["idle_s"]           = snap.idle_s;
    // Same argument as idle_s: emitted unconditionally, including the 0x07 of a
    // counter nobody has narrowed and the 0x07 implied by a pre-v5 counter.
    // A field that appeared only when a bank was off would make "all banks on"
    // and "counter too old to say" the same absence in stored history, and the
    // whole point is to tell a deliberately dark bank from a dead FET.
    bc["banks"]            = snap.bank_mask;
}

// ---------------------------------------------------------------------------
// HiveTraffic GATT-client read
// ---------------------------------------------------------------------------
#if ENABLE_WIRELESS_BEECOUNTER

namespace {

// Parse the HiveTraffic measurement JSON (see 2026-easy-bee-counter
// docs/ble-mode.md) into a Snapshot. Returns false on malformed JSON or a
// document carrying no "fw". Fields absent in the document keep their Snapshot
// defaults.
//
// The decoding — including the fw:2 / fw:3 branch and the saturating integer
// reads — lives in bee_counter_wire.h so it can be tested on a host compiler
// against captured documents of both revisions
// (test-data/test_bee_counter_wire.cpp). This wrapper is only the copy into
// Snapshot.
bool parseTrafficJson(const char* json, size_t len, Snapshot& out) {
    wire::Measurement m;
    if (!wire::parseMeasurement(json, len, m)) {
        Serial.println("[TRAFFIC] measurement document rejected (malformed or not a measurement)");
        return false;
    }
    out.present       = true;
    out.fw_version    = m.protocol_version;
    strlcpy(out.version, m.version, sizeof(out.version));
    out.status_flags  = m.status_flags;
    out.uptime_s      = m.uptime_s;
    out.num_gates     = m.num_gates;
    out.mcps_healthy  = m.mcps_healthy;
    out.total_in      = m.total_in;
    out.total_out     = m.total_out;
    out.glitch_count  = m.glitch_count;
    out.idle_s        = m.idle_s;
    out.bank_mask     = m.bank_mask;
    return true;
}

// Write one night-mode grant to an already-connected counter.
//
// Returns false when the counter has no control characteristic, which is simply
// what a pre-v4 firmware looks like: night mode then degrades to "this counter
// keeps counting", never to an error, and the OTA relay will bring it up to a
// firmware that can be suspended in the normal course of things.
//
// Deliberately fire-and-forget beyond that. The counter's own deadline is the
// authority, we re-arm every cycle anyway, and a failed write costs one cycle
// of emitters — not correctness. Blocking the measurement read on it would
// trade the reading we came for against a power optimisation.
bool writeIdleGrant(NimBLERemoteService* svc, uint32_t duration_s) {
    if (!svc) return false;
    NimBLERemoteCharacteristic* chr =
        svc->getCharacteristic(NimBLEUUID(BEECOUNTER_GATT_CONTROL_UUID));
    if (!chr || !chr->canWrite()) return false;

    // opcode + uint32 little-endian, matching beecounter_proto::CTRL_OP_SET_IDLE.
    uint8_t frame[5];
    frame[0] = BEECOUNTER_CTRL_OP_SET_IDLE;
    frame[1] = (uint8_t)(duration_s);
    frame[2] = (uint8_t)(duration_s >> 8);
    frame[3] = (uint8_t)(duration_s >> 16);
    frame[4] = (uint8_t)(duration_s >> 24);
    // With response: the counter's reply is the only confirmation that the
    // grant landed, and there is exactly one small write per cycle, so there is
    // nothing to gain from the unacknowledged variant.
    return chr->writeValue(frame, sizeof(frame), true);
}

// Write the configured emitter-bank mask to an already-connected counter.
//
// Gated by the caller on the counter reporting wire revision 5 or later: an
// older firmware has the control characteristic but not this opcode, and would
// log an "unknown opcode" line every cycle for the rest of its deployment.
//
// The caller writes whenever the mask the counter just REPORTED differs from
// the configured one — never against a HiveHub-side memory of what it last
// sent. That distinction is the whole self-healing property: the counter does
// not persist the mask, so one that browned out, watchdogged or rebooted out of
// an OTA comes back reporting all three banks, and the next cycle's read shows
// the disagreement and fixes it. A HiveHub that instead remembered "I already
// configured this one" would leave that counter wide open indefinitely, and a
// HiveHub that wrote unconditionally would spend a GATT write every ten minutes
// to change nothing.
//
// Fire-and-forget beyond the return value, like writeIdleGrant: a failed write
// costs one cycle of a wider entrance than asked for, never a reading.
bool writeBankMask(NimBLERemoteService* svc, uint8_t mask) {
    if (!svc) return false;
    NimBLERemoteCharacteristic* chr =
        svc->getCharacteristic(NimBLEUUID(BEECOUNTER_GATT_CONTROL_UUID));
    if (!chr || !chr->canWrite()) return false;

    uint8_t frame[2];
    frame[0] = BEECOUNTER_CTRL_OP_SET_BANKS;
    frame[1] = mask;
    // With response, for the same reason the idle grant is: one small write per
    // cycle, and the reply is the only confirmation it landed.
    return chr->writeValue(frame, sizeof(frame), true);
}

// Everything the night-mode decision needs that is the same for every counter
// on this hub: the configured window, the current local time, the upload
// interval and whether a firmware relay is unfinished. Gathered once per cycle
// rather than per hive — the clock does not move meaningfully across a handful
// of GATT sessions, and a window boundary falling between two hives would
// suspend one and not the other for no reason a beekeeper could reconstruct.
struct NightContext {
    nightmode::Config cfg;
    uint16_t          minute = nightmode::MINUTES_PER_DAY;   // "no clock"
    uint32_t          cycle_s = 600;
    bool              relay_pending = false;
};

// Is a firmware relay in flight?
//
// The ordinary relay path needs no guard: checkCommands() runs after this
// function, a suspended counter still accepts an image (HiveTraffic pauses
// sensing during a transfer regardless, and refuses a *new* suspension while
// one is running), and the post-OTA reboot clears any suspension anyway.
//
// What this catches is the interrupted case. rtcRelayMagic survives a reset, so
// it is set exactly while a relay is unfinished — and that is when the counter
// most needs to be awake, advertising and reachable for the retry, rather than
// suspended by a HiveHub that is about to try again. Conservative on purpose:
// it is also set for a HiveInside relay, and one extra cycle of counting is not
// worth distinguishing them.
bool relayInFlight() {
    return rtcRelayMagic != 0;
}

// Assemble the night-mode configuration the dashboard delivered.
nightmode::Config nightConfig() {
    nightmode::Config c;
    c.enabled      = nightModeEnabled;
    c.start_minute = nightStartMinute;
    c.end_minute   = nightEndMinute;
    c.max_traffic  = nightMaxTraffic;
    return c;
}

// Crossings on hive slot `h` since the previous cycle, from the totals kept in
// RTC memory across HiveHub's own deep sleep. `known` is false until this slot
// has been read at least once — the gate must not treat "never seen" as "quiet".
nightmode::Traffic trafficSince(uint8_t h, const Snapshot& snap) {
    nightmode::Traffic t;
    if (h >= MAX_HIVES || !snap.present) return t;
    if (rtcCounterTotalsValid & (1UL << h)) {
        t.known = true;
        t.crossings =
            nightmode::crossingsBetween(rtcCounterTotalIn[h], snap.total_in) +
            nightmode::crossingsBetween(rtcCounterTotalOut[h], snap.total_out);
    }
    return t;
}

// Remember this cycle's totals as the next cycle's baseline.
void rememberTotals(uint8_t h, const Snapshot& snap) {
    if (h >= MAX_HIVES || !snap.present) return;
    rtcCounterTotalIn[h]  = snap.total_in;
    rtcCounterTotalOut[h] = snap.total_out;
    rtcCounterTotalsValid |= (1UL << h);
}

// Connect to `mac`, read the measurement characteristic once, parse it into
// `out`, then decide and apply night mode on the same connection.
//
// The ordering is forced and worth stating: the night-mode traffic gate asks
// how busy this hive was during the cycle that just ended, which is this
// cycle's fresh totals minus the previous cycle's — so the decision cannot be
// made until the read has happened. Doing it here rather than in the caller is
// what keeps it to ONE connection per counter per cycle.
//
// Tries public then random address type (HiveTraffic advertises with the
// ESP32's default random static address, but seeded MACs may be either).
//
// `slot` is the hive index, for the RTC-held totals baseline. A grant of 0 is a
// real instruction (resume now), not a no-op: it is how a counter suspended by
// a previous cycle is released the moment the window ends or the beekeeper
// turns the feature off.
bool readTrafficSlot(const String& mac, Snapshot& out, uint8_t slot,
                     const NightContext& night) {
    if (mac.length() == 0) return false;

    NimBLEClient* client = NimBLEDevice::createClient();
    client->setConnectTimeout((uint32_t)BEECOUNTER_GATT_CONNECT_TIMEOUT_S * 1000UL);

    const uint8_t addrTypes[2] = { BLE_ADDR_PUBLIC, BLE_ADDR_RANDOM };
    bool connected = false;
    for (int t = 0; t < 2 && !connected; t++) {
        NimBLEAddress addr(std::string(mac.c_str()), addrTypes[t]);
        Serial.printf("[TRAFFIC] connecting to %s (addr type %u) ...\n",
                      mac.c_str(), addrTypes[t]);
        if (client->connect(addr)) connected = true;
    }
    if (!connected) {
        Serial.printf("[TRAFFIC] connect failed for %s\n", mac.c_str());
        NimBLEDevice::deleteClient(client);
        return false;
    }

    bool ok = false;
    NimBLERemoteService* svc = client->getService(NimBLEUUID(BEECOUNTER_GATT_SERVICE_UUID));
    if (!svc) {
        Serial.println("[TRAFFIC] service not found");
    } else {
        NimBLERemoteCharacteristic* chr =
            svc->getCharacteristic(NimBLEUUID(BEECOUNTER_GATT_CHAR_UUID));
        if (!chr || !chr->canRead()) {
            Serial.println("[TRAFFIC] measurement characteristic unreadable");
        } else {
            std::string v = chr->readValue();
            if (v.empty()) {
                Serial.println("[TRAFFIC] empty characteristic read");
            } else {
                ok = parseTrafficJson(v.c_str(), v.size(), out);
                if (ok) {
                    // The wire revision is logged too: a counter still on fw=2
                    // is one the OTA relay has not reached yet, and that is the
                    // difference between "old firmware" and "broken link".
                    Serial.printf("[TRAFFIC] %s: fw=%s wire=v%u in=%lu out=%lu "
                                  "uptime=%lus mcps=%u/3 status=0x%02X "
                                  "banks=0x%02X\n",
                                  mac.c_str(),
                                  out.version[0] ? out.version : "?",
                                  (unsigned)out.fw_version,
                                  (unsigned long)out.total_in,
                                  (unsigned long)out.total_out,
                                  (unsigned long)out.uptime_s,
                                  (unsigned)out.mcps_healthy,
                                  out.status_flags,
                                  (unsigned)out.bank_mask);
                }
            }
        }

        // Re-assert the emitter-bank mask on the connection we already have.
        // Ordered before the night-mode grant only because it is the cheaper
        // failure: the two are independent, and a counter can perfectly well be
        // running one bank AND suspended.
        //
        // Gated on the counter having reported a revision that understands the
        // opcode. A failed read leaves out.fw_version at 0, which correctly
        // skips the write — we have no idea what we are talking to.
        if (out.present && out.fw_version >= wire::REV_LED_BANKS &&
            out.bank_mask != beeBankMask) {
            if (writeBankMask(svc, beeBankMask)) {
                Serial.printf("[TRAFFIC] %s: emitter banks 0x%02X -> 0x%02X\n",
                              mac.c_str(), (unsigned)out.bank_mask,
                              (unsigned)beeBankMask);
            } else {
                Serial.printf("[TRAFFIC] %s: emitter bank write refused "
                              "(wanted 0x%02X)\n",
                              mac.c_str(), (unsigned)beeBankMask);
            }
        }

        // Now that the totals are in hand, decide — and apply on the
        // connection we already have: no extra scan, no extra connect, one
        // 5-byte write. Doing it after the read also means a counter about to
        // be suspended still reports everything it counted up to this moment;
        // suspending first would freeze the totals a fraction of a second
        // before we sampled them.
        //
        // A failed read leaves out.present false, so trafficSince() reports no
        // baseline and the gate (if configured) postpones — which is the right
        // answer: we have no idea how busy this hive is.
        const nightmode::Decision decision =
            nightmode::decide(night.cfg, night.minute, trafficSince(slot, out),
                              night.cycle_s, night.relay_pending);
        if (night.cfg.enabled) {
            if (writeIdleGrant(svc, decision.duration_s)) {
                Serial.printf("[TRAFFIC] %s: night mode %s (%lus) — %s\n",
                              mac.c_str(),
                              decision.suspend ? "armed" : "cleared",
                              (unsigned long)decision.duration_s,
                              nightmode::reasonName(decision.reason));
            } else if (decision.suspend) {
                // Only worth a line when we wanted something to happen. A
                // pre-v4 counter has no control characteristic at all, so a
                // failed "resume" write is its expected steady state and would
                // otherwise print every cycle, forever.
                Serial.printf("[TRAFFIC] %s: night mode not applied "
                              "(counter too old, or write refused)\n",
                              mac.c_str());
            }
        }
        // The baseline for the next cycle's gate. Only advanced on a successful
        // read (rememberTotals guards on present), so an unreachable cycle does
        // not turn into a fabricated interval next time.
        rememberTotals(slot, out);
    }

    // HiveTraffic stays connectable; close the link deterministically and wait
    // for it to drop before freeing the client so nothing is left for the
    // deinit() in bleRunCycleRegistry() to trip over.
    if (client->isConnected()) client->disconnect();
    uint32_t deadline = millis() + BEECOUNTER_GATT_DISCONNECT_TIMEOUT_MS;
    while (client->isConnected() && (int32_t)(deadline - millis()) > 0) delay(20);
    NimBLEDevice::deleteClient(client);
    return ok;
}

}  // namespace

void bleRunCycleRegistry(Snapshot* out, uint8_t cap) {
    for (uint8_t h = 0; h < cap; h++) out[h] = Snapshot{};

    // Nothing to do unless at least one hive has a paired HiveTraffic counter.
    bool anyPaired = false;
    for (uint8_t h = 0; h < hivecfg::gHiveCount && h < cap; h++) {
        const hivecfg::Hive& hive = hivecfg::gHives[h];
        for (uint8_t b = 0; b < hive.bleCount; b++)
            if (hive.ble[b].type == "beecounter" && hive.ble[b].mac.length()) anyPaired = true;
    }
    if (!anyPaired) return;

    NightContext night;
    night.cfg           = nightConfig();
    night.minute        = localMinuteOfDay();
    night.cycle_s       = (uint32_t)(sendIntervalMs / 1000UL);
    night.relay_pending = relayInFlight();

    blestack::acquire();
    uint8_t readAttempts = 0;
    for (uint8_t h = 0; h < hivecfg::gHiveCount && h < cap; h++) {
        const hivecfg::Hive& hive = hivecfg::gHives[h];
        for (uint8_t b = 0; b < hive.bleCount; b++) {
            const hivecfg::BlePairing& p = hive.ble[b];
            if (p.type != "beecounter" || p.mac.length() == 0) continue;

            if (readAttempts >= MAX_GATT_READS_PER_CYCLE) {
                Serial.printf("[TRAFFIC] GATT read budget exhausted (%u); skipping hive %u %s\n",
                              (unsigned)MAX_GATT_READS_PER_CYCLE, hive.index, p.mac.c_str());
                break;
            }
            readAttempts++;

            // readTrafficSlot reads, then decides, then applies — all on one
            // connection. The decision has to follow the read because the
            // traffic gate is about the cycle that just ended, and it has to
            // precede the disconnect because the alternative is a second
            // connection per counter per cycle.
            (void)readTrafficSlot(p.mac, out[h], h, night);
            break;  // at most one HiveTraffic counter per hive
        }
    }
    // Counter reads only ever connect, so they are safe in any port lifetime.
    // See ble_stack.h for the teardown rule. Controller is still freed.
    blestack::release();
}

#endif  // ENABLE_WIRELESS_BEECOUNTER

}  // namespace beecnt
