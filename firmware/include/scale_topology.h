// scale_topology.h — pure validation of the wired scale-channel topology, used
// by hive_config.cpp for both NVS-loaded and portal-submitted registries. The
// dropdown logic in the portal page is convenience only; THIS is the
// authoritative check, so a crafted /save request or a corrupt stored blob can
// never map two hives onto one physical channel or mix impossible topologies.
//
// Arduino-free (plain structs, no String) so the host test suite verifies the
// exact production rules: duplicate hive indices, out-of-range indices,
// duplicate physical channels, invalid mux (no & 0x07 masking!), invalid ADC
// channel, invalid HX711 index, direct-plus-muxed NAU7802 conflicts, and
// channel counts beyond the supported hardware.
#pragma once

#include <stdint.h>
#include <stddef.h>

namespace scaletopo {

enum class Backend : uint8_t { None = 0, HX711 = 1, NAU7802 = 2 };

// One wired channel as submitted/stored, with the hive it belongs to.
struct ChanSpec {
  uint8_t hiveIndex  = 0;      // owning hive's index (1-based)
  Backend backend    = Backend::None;
  uint8_t hxIndex    = 0;      // HX711: 0 or 1
  int16_t muxChannel = -1;     // NAU7802: -1 = direct on main bus, 0..7 = mux port
                               // (int16_t so overflowed/garbage values survive
                               //  to be REJECTED instead of masked)
  int16_t adcChannel = 1;      // NAU7802 input: 1 or 2
};

// Why a channel (or the whole set) was rejected. One flag word per channel.
enum ChanIssue : uint16_t {
  CHAN_OK                 = 0,
  CHAN_BAD_MUX            = 1 << 0,  // mux outside -1..7 (e.g. 8, -2, 300)
  CHAN_BAD_ADC            = 1 << 1,  // adc not 1 or 2
  CHAN_BAD_HX_INDEX       = 1 << 2,  // hx not 0 or 1
  CHAN_DUPLICATE          = 1 << 3,  // same physical channel already claimed
  CHAN_DIRECT_MUX_CONFLICT= 1 << 4,  // direct NAU7802 combined with muxed ones
  CHAN_BAD_BACKEND        = 1 << 5,  // backend value not in the enum
};

struct TopologyResult {
  bool    ok = true;             // no channel carries an issue flag
  uint8_t directNau  = 0;        // count of main-bus NAU7802 channels
  uint8_t muxedNau   = 0;        // count of muxed NAU7802 channels
  uint8_t hx711      = 0;
};

// Validate `n` channels; writes one ChanIssue flag word per channel into
// `issues` (same length). Later duplicates are flagged, the first claim wins.
// The direct-vs-mux conflict flags EVERY NAU7802 channel involved, because the
// topology as a whole is unreadable (all NAU7802s share address 0x2A; a
// main-bus chip stays on the bus while a mux port is open, so their reads
// collide) — no safe guess exists about which side the user meant.
inline TopologyResult validateChannels(const ChanSpec* chans, uint8_t n,
                                       uint16_t* issues) {
  TopologyResult res;
  for (uint8_t i = 0; i < n; i++) issues[i] = CHAN_OK;

  for (uint8_t i = 0; i < n; i++) {
    const ChanSpec& c = chans[i];
    switch (c.backend) {
      case Backend::None:
        break;
      case Backend::HX711:
        if (c.hxIndex > 1) issues[i] |= CHAN_BAD_HX_INDEX;
        break;
      case Backend::NAU7802:
        if (c.muxChannel < -1 || c.muxChannel > 7) issues[i] |= CHAN_BAD_MUX;
        if (c.adcChannel != 1 && c.adcChannel != 2) issues[i] |= CHAN_BAD_ADC;
        break;
      default:
        issues[i] |= CHAN_BAD_BACKEND;
        break;
    }
    if (issues[i]) continue;
    if (c.backend == Backend::NAU7802) {
      if (c.muxChannel < 0) res.directNau++; else res.muxedNau++;
    } else if (c.backend == Backend::HX711) {
      res.hx711++;
    }
  }

  // Duplicate physical channels (only among individually valid channels).
  for (uint8_t i = 0; i < n; i++) {
    if (issues[i] || chans[i].backend == Backend::None) continue;
    for (uint8_t j = 0; j < i; j++) {
      if (issues[j] || chans[j].backend != chans[i].backend) continue;
      bool same = false;
      if (chans[i].backend == Backend::HX711) {
        same = chans[i].hxIndex == chans[j].hxIndex;
      } else {
        same = chans[i].muxChannel == chans[j].muxChannel &&
               chans[i].adcChannel == chans[j].adcChannel;
      }
      if (same) { issues[i] |= CHAN_DUPLICATE; break; }
    }
  }

  // Direct + muxed NAU7802 cannot coexist (shared fixed address 0x2A).
  if (res.directNau > 0 && res.muxedNau > 0) {
    for (uint8_t i = 0; i < n; i++) {
      if (chans[i].backend == Backend::NAU7802 && !(issues[i] & (CHAN_BAD_MUX | CHAN_BAD_ADC)))
        issues[i] |= CHAN_DIRECT_MUX_CONFLICT;
    }
  }

  for (uint8_t i = 0; i < n; i++)
    if (issues[i]) { res.ok = false; break; }
  return res;
}

// Hive-index validation: every index in 1..maxHives, no duplicates.
// Writes 1 into bad[i] for each offending entry.
inline bool validateHiveIndices(const uint8_t* indices, uint8_t n,
                                uint8_t maxHives, uint8_t* bad) {
  bool ok = true;
  for (uint8_t i = 0; i < n; i++) {
    bad[i] = 0;
    if (indices[i] < 1 || indices[i] > maxHives) { bad[i] = 1; ok = false; continue; }
    for (uint8_t j = 0; j < i; j++) {
      if (indices[j] == indices[i]) { bad[i] = 1; ok = false; break; }
    }
  }
  return ok;
}

}  // namespace scaletopo
