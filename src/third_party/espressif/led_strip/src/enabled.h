#pragma once

#ifndef ESP32
// No led strip component when not in ESP32 mode.
 #define FASTLED_RMT5 0
 #define FASTLED_ESP32_HAS_RMT 0
 #define FASTLED_ESP32_HAS_RMT5 0
#else

#include "espdefs.h"

// FASTLED_ESP32_HAS_CLOCKLESS_SPI
// FASTLED_RMT5
// FASTLED_ESP32_HAS_RMT
// FASTLED_ESP32_HAS_RMT5

#if _CONFIG_TARGET_ESP32
#define FASTLED_ESP32_HAS_CLOCKLESS_SPI 0
#define FASTLED_ESP32_HAS_RMT 1
#define FASTLED_ESP32_HAS_RMT5 0
#elif _HAS_IDF5 && _HAS_RMT
#define FASTLED_ESP32_HAS_CLOCKLESS_SPI 1
#define FASTLED_ESP32_HAS_RMT 1
#define FASTLED_ESP32_HAS_RMT5 1
#elif _HAS_IDF5 && !defined(_HAS_RMT)
#define FASTLED_ESP32_HAS_CLOCKLESS_SPI 1
#define FASTLED_ESP32_HAS_RMT 0
#define FASTLED_ESP32_HAS_RMT5 0
#endif


#if FASTLED_ESP32_HAS_CLOCKLESS_SPI
#warning "HAS CLOCKLESS SPI"
#else
#warning "NO CLOCKLESS SPI"
#endif

#if FASTLED_ESP32_HAS_RMT5
#warning "HAS RMT5"
#else
#warning "NO RMT5"
#endif

#if FASTLED_ESP32_HAS_RMT
#warning "HAS RMT"
#else
#warning "NO RMT"
#endif

#if FASTLED_ESP32_HAS_RMT5
#define FASTLED_RMT5 1
#else
#define FASTLED_RMT5 0
#endif


#endif