#include "button.h"
#include "config.h"
#include "device_mode.h"

static bool lastStable = true;            // released (HIGH) at boot
static bool lastReading = true;
static unsigned long lastChange = 0;
static unsigned long pressedAt = 0;
static bool armed = false;                // false until first observed RELEASED state

void setupButton() {
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    delay(5);  // give pull-up a moment to take effect
    lastStable = digitalRead(BUTTON_PIN);
    lastReading = lastStable;
    lastChange = millis();
    pressedAt = millis();
    armed = (lastStable == HIGH);  // if button is held at boot, ignore until first release
}

void pollButton() {
    bool reading = digitalRead(BUTTON_PIN);
    unsigned long now = millis();

    if (reading != lastReading) {
        lastChange = now;
        lastReading = reading;
    }

    if ((now - lastChange) < BUTTON_DEBOUNCE_MS) return;
    if (reading == lastStable) return;

    lastStable = reading;
    if (lastStable == LOW) {
        // Falling edge — pressed (only counts if we're armed)
        pressedAt = now;
    } else {
        // Rising edge — released
        if (!armed) {
            // First release after boot-time hold — arm now, don't fire
            armed = true;
            return;
        }
        unsigned long heldFor = now - pressedAt;
        if (heldFor < BUTTON_HOLD_IGNORE_MS) {
            StorageMode cur = currentStorageMode();
            StorageMode next = (cur == STORAGE_MODE_INTERNAL) ? STORAGE_MODE_MSC : STORAGE_MODE_INTERNAL;
            rebootIntoMode(next);  // does not return
        }
    }
}
