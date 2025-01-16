
#pragma once

#include <stdint.h>
#include "platforms/esp/esp_version.h"

#if __has_include(<hal/cpu_hal.h>)
// esp-idf v5.0.0+
#include <hal/cpu_hal.h>
#define __cpu_hal_get_cycle_count esp_cpu_get_cycle_count
#elif __has_include(<hal/cpu_ll.h>)
  // esp-idf v4.3.0+
#include <hal/cpu_ll.h>
inline uint32_t __cpu_hal_get_cycle_count() {
  return static_cast<uint32_t>(cpu_ll_get_cycle_count());
}
#else  // Fallback to, if this fails then please file a bug at github.com/fastled/FastLED/issues and let us know what board you are using.
#include <esp_cpu.h>
inline uint32_t __cpu_hal_get_cycle_count() {
  return static_cast<uint32>(esp_cpu_get_cycle_count());
}
#endif  // ESP_IDF_VERSION


__attribute__ ((always_inline)) inline static uint32_t __clock_cycles() {
  uint32_t cyc;
#ifdef FASTLED_XTENSA
  __asm__ __volatile__ ("rsr %0,ccount":"=a" (cyc));
#else
  cyc = __cpu_hal_get_cycle_count();
#endif
  return cyc;
}