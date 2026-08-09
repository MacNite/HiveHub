// heap_diag.h — heap health probes for bisecting memory corruption.
//
// Added after a field panic on the ESP32-C6: the hub claimed an
// `update_hiveinside` command, printed the download line, and then died in the
// allocator with a load access fault on a garbage pointer (MCAUSE 0x5,
// MTVAL 0x03cf5f1b) while servicing a 4000-byte request. An allocation that
// faults walking its own free list is the *victim* of corruption, not the
// cause — something scribbled over heap metadata earlier in the cycle, and the
// next unlucky malloc paid for it. A backtrace from the crash site therefore
// names the wrong code.
//
// These probes exist to find the real culprit by bisection: call checkIntegrity()
// at stage boundaries and the first stage that reports damage is the one that
// did it. logDiag() is the cheap companion — it tracks headroom over a cycle, so
// a slow leak or a fragmentation cliff shows up before it turns into a fault.
//
// Both are compiled out when ENABLE_HEAP_DIAG is 0 (config.h).
#pragma once

#include <Arduino.h>

#include "config.h"

namespace heapdiag {

// Print free / largest-contiguous / lowest-ever-free for the 8-bit-capable
// heap, tagged with `stage`. The largest *contiguous* block is the number that
// matters for TLS and BLE bring-up: a fragmented heap can report plenty free
// and still fail a 4 KB request.
void logDiag(const char* stage);

// Walk every heap block and validate the allocator's own structures. Returns
// true when the heap is intact; on damage it returns false after the ESP-IDF
// heap component has printed the offending block to the console.
//
// NOTE: with the default Arduino sdkconfig, poisoning is not comprehensive, so
// this cannot see a write that lands inside a live allocation without touching a
// block header or canary. It does validate the block structure the crash dump
// implicates, which is what this is here for.
bool checkIntegrity(const char* stage);

// logDiag() + checkIntegrity() in one call, for stage boundaries where both are
// wanted. Returns what checkIntegrity() returned.
bool probe(const char* stage);

}  // namespace heapdiag
