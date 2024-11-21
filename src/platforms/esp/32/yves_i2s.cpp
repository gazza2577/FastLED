#if defined(ESP32) && defined(ENABLE_ESP32_I2S_YVEZ_DRIVER)

#include "platforms/esp/32/yvez_i2s.h"
#include "third_party/yvez/I2SClocklessLedDriver.h"
#include "namespace.h"

FASTLED_NAMESPACE_BEGIN

static_assert(NUM_LEDS_PER_STRIP == 256, "Only 256 supported");

class YvezI2SImpl : public I2SClocklessVirtualLedDriver {};


YvezI2S::YvezI2S(CRGBArray6Strips* leds, int clock_pin, int latch_pin,
        const Pins &pins = DefaultPins()) : mPins(pins){
    mDriver->initled(leds->get(), mPins.data(), clock_pin, latch_pin);
}

void YvezI2S::showPixels() { mDriver->showPixels(); }

FASTLED_NAMESPACE_END

#endif // ESP32
