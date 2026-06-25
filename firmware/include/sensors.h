// sensors.h — time keeping, load-cell reads and assembly of the per-cycle
// measurement JSON payload (the heart of each upload cycle).
#pragma once

#include <Arduino.h>
#include "config.h"
#if ENABLE_HX711
#include <HX711.h>
#endif

String timestampNow();
void syncTime();
void initializeTime(bool wokeFromDeepSleep);

#if ENABLE_HX711
long readAverageRaw(HX711& scale, int samples = 15);
#endif
float weightFromRaw(long raw, long offset, float factor);

String createMeasurementJson();
