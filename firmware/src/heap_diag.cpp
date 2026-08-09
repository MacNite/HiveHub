// heap_diag.cpp — see heap_diag.h for why this exists.
#include "heap_diag.h"

#if ENABLE_HEAP_DIAG

#include <esp_heap_caps.h>

namespace heapdiag {

void logDiag(const char* stage) {
  // MALLOC_CAP_8BIT is the pool that TLS buffers, NimBLE and every String come
  // from, so it is the one whose exhaustion or fragmentation is felt first.
  const size_t freeNow = heap_caps_get_free_size(MALLOC_CAP_8BIT);
  const size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
  const size_t minEver = heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT);

  Serial.printf("[HEAP] %s: free=%u largest=%u min_ever=%u\n",
                stage ? stage : "?", (unsigned)freeNow, (unsigned)largest,
                (unsigned)minEver);
}

bool checkIntegrity(const char* stage) {
  // true = let the heap component print the offending block itself; its output
  // identifies the block far more precisely than anything we could reconstruct.
  if (heap_caps_check_integrity_all(true)) return true;

  Serial.printf("[HEAP] *** CORRUPTION DETECTED at stage: %s ***\n",
                stage ? stage : "?");
  return false;
}

bool probe(const char* stage) {
  logDiag(stage);
  return checkIntegrity(stage);
}

}  // namespace heapdiag

#else  // !ENABLE_HEAP_DIAG

namespace heapdiag {
void logDiag(const char*) {}
bool checkIntegrity(const char*) { return true; }
bool probe(const char*) { return true; }
}  // namespace heapdiag

#endif  // ENABLE_HEAP_DIAG
