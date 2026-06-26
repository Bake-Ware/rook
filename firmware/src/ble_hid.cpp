// BLE HID peripheral — wraps the T-vK/ESP32-BLE-Keyboard library so the
// dongle pairs to a phone/PC as a wireless keyboard. Capabilities on the
// Telesthete band let Claude type strings, fire key combos, and send
// consumer-control (media) reports that the bonded host receives as if
// they came from a real Bluetooth keyboard.
//
// This module owns the BLE radio. The earlier custom provisioning service
// is gone; WiFi creds are managed via the prioritized list in /config.

#include "ble_hid.h"
#include "config.h"
#include "settings.h"
#include <BleKeyboard.h>
#include <strings.h>

// Forward-declared instead of including hid.h to avoid colliding with
// USBHIDKeyboard's KeyReport typedef.
uint8_t lookupSpecialKey(const char* name);
uint8_t lookupModifierKey(const char* name);

static const char* MANUFACTURER = "Rook";
static BleKeyboard* g_kbd = nullptr;

struct MediaMap { const char* name; const MediaKeyReport* code; };

static const MediaMap MEDIA_KEYS[] = {
    {"volume_up",      &KEY_MEDIA_VOLUME_UP},
    {"vol_up",         &KEY_MEDIA_VOLUME_UP},
    {"volume_down",    &KEY_MEDIA_VOLUME_DOWN},
    {"vol_down",       &KEY_MEDIA_VOLUME_DOWN},
    {"mute",           &KEY_MEDIA_MUTE},
    {"play_pause",     &KEY_MEDIA_PLAY_PAUSE},
    {"playpause",      &KEY_MEDIA_PLAY_PAUSE},
    {"play",           &KEY_MEDIA_PLAY_PAUSE},
    {"next",           &KEY_MEDIA_NEXT_TRACK},
    {"next_track",     &KEY_MEDIA_NEXT_TRACK},
    {"prev",           &KEY_MEDIA_PREVIOUS_TRACK},
    {"previous",       &KEY_MEDIA_PREVIOUS_TRACK},
    {"prev_track",     &KEY_MEDIA_PREVIOUS_TRACK},
    {"stop",           &KEY_MEDIA_STOP},
    {"home",           &KEY_MEDIA_WWW_HOME},
    {"ac_home",        &KEY_MEDIA_WWW_HOME},
    {"back",           &KEY_MEDIA_WWW_BACK},
    {"ac_back",        &KEY_MEDIA_WWW_BACK},
    {nullptr,          nullptr},
};

static const MediaKeyReport* lookupMedia(const char* name) {
    if (!name) return nullptr;
    for (int i = 0; MEDIA_KEYS[i].name; i++)
        if (strcasecmp(MEDIA_KEYS[i].name, name) == 0) return MEDIA_KEYS[i].code;
    return nullptr;
}

void setupBleHid() {
    const auto& s = getSettings();
    String name = s.worker_name.length() ? s.worker_name : String("rook-kvm");
    // BleKeyboard manages BLEDevice::init internally and advertises as a
    // proper HOGP keyboard (appearance=0x03C1, HID service 0x1812). Phones
    // will list it under "pair new device" and bind cleanly.
    g_kbd = new BleKeyboard(name.c_str(), MANUFACTURER, 100);
    g_kbd->begin();
    Serial.printf("ble_hid: advertising as BLE HOGP keyboard '%s'\n", name.c_str());
}

bool bleHidConnected() {
    return g_kbd && g_kbd->isConnected();
}

size_t bleHidType(const char* text, int delay_ms) {
    if (!g_kbd || !g_kbd->isConnected() || !text) return 0;
    size_t len = strlen(text);
    for (size_t i = 0; i < len; i++) {
        g_kbd->write((uint8_t)text[i]);
        if (delay_ms > 0) delay(delay_ms);
    }
    return len;
}

bool bleHidKey(const char* key, const char* mods[], size_t mods_n) {
    if (!g_kbd || !g_kbd->isConnected()) return false;
    for (size_t i = 0; i < mods_n; i++) {
        uint8_t m = lookupModifierKey(mods[i]);
        if (m) g_kbd->press(m);
    }
    if (key && *key) {
        if (strlen(key) == 1) {
            g_kbd->press((uint8_t)key[0]);
        } else {
            uint8_t k = lookupSpecialKey(key);
            if (k) g_kbd->press(k);
        }
    }
    delay(50);
    g_kbd->releaseAll();
    return true;
}

bool bleHidConsumer(const char* name, uint16_t /*code_override*/) {
    if (!g_kbd || !g_kbd->isConnected()) return false;
    const MediaKeyReport* mk = lookupMedia(name);
    if (!mk) return false;
    g_kbd->write(*mk);
    return true;
}
