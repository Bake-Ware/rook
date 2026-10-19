#include "hid.h"
#include "device_mode.h"
#include <string.h>

USBHIDKeyboard Keyboard;
USBHIDConsumerControl Consumer;
RookMouse Mouse;

static_assert(sizeof(RookMouseReport) == 7, "RookMouseReport must be 7 bytes");

// 8-button relative mouse: buttons(8x1b) + X/Y(16b rel) + wheel(8b) + AC Pan(8b).
// Hand-rolled because TUD_HID_REPORT_DESC_MOUSE carries only 5 buttons and no
// independent horizontal wheel.
static const uint8_t rook_mouse_desc[] = {
    0x05, 0x01,               // Usage Page (Generic Desktop)
    0x09, 0x02,               // Usage (Mouse)
    0xA1, 0x01,               // Collection (Application)
    0x85, ROOK_HID_RPT_MOUSE, //   Report ID (2)
    0x09, 0x01,               //   Usage (Pointer)
    0xA1, 0x00,               //   Collection (Physical)
    0x05, 0x09,               //     Usage Page (Button)
    0x19, 0x01,               //     Usage Minimum (Button 1)
    0x29, 0x08,               //     Usage Maximum (Button 8)
    0x15, 0x00,               //     Logical Minimum (0)
    0x25, 0x01,               //     Logical Maximum (1)
    0x95, 0x08,               //     Report Count (8)
    0x75, 0x01,               //     Report Size (1)
    0x81, 0x02,               //     Input (Data,Var,Abs)
    0x05, 0x01,               //     Usage Page (Generic Desktop)
    0x09, 0x30,               //     Usage (X)
    0x09, 0x31,               //     Usage (Y)
    0x16, 0x01, 0x80,         //     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,         //     Logical Maximum (32767)
    0x75, 0x10,               //     Report Size (16)
    0x95, 0x02,               //     Report Count (2)
    0x81, 0x06,               //     Input (Data,Var,Rel)
    0x09, 0x38,               //     Usage (Wheel)
    0x15, 0x81,               //     Logical Minimum (-127)
    0x25, 0x7F,               //     Logical Maximum (127)
    0x75, 0x08,               //     Report Size (8)
    0x95, 0x01,               //     Report Count (1)
    0x81, 0x06,               //     Input (Data,Var,Rel)
    0x05, 0x0C,               //     Usage Page (Consumer)
    0x0A, 0x38, 0x02,         //     Usage (AC Pan)
    0x15, 0x81,               //     Logical Minimum (-127)
    0x25, 0x7F,               //     Logical Maximum (127)
    0x75, 0x08,               //     Report Size (8)
    0x95, 0x01,               //     Report Count (1)
    0x81, 0x06,               //     Input (Data,Var,Rel)
    0xC0,                     //   End Collection
    0xC0,                     // End Collection
};

RookMouse::RookMouse() : hid() {
    static bool initialized = false;
    if (!initialized) {
        initialized = true;
        hid.addDevice(this, sizeof(rook_mouse_desc));
    }
}

uint16_t RookMouse::_onGetDescriptor(uint8_t* dst) {
    memcpy(dst, rook_mouse_desc, sizeof(rook_mouse_desc));
    return sizeof(rook_mouse_desc);
}

void RookMouse::begin() { hid.begin(); }

bool RookMouse::sendRaw(const uint8_t* data, size_t len) {
    if (len != sizeof(RookMouseReport)) return false;
    return hid.SendReport(ROOK_HID_RPT_MOUSE, data, len);
}

bool RookMouse::sendReportId(uint8_t report_id, const void* data, size_t len) {
    return hid.SendReport(report_id, data, len);
}

bool RookMouse::move(uint8_t buttons, int16_t dx, int16_t dy, int8_t wheel, int8_t pan) {
    RookMouseReport r;
    r.buttons = buttons; r.x = dx; r.y = dy; r.wheel = wheel; r.pan = pan;
    return hid.SendReport(ROOK_HID_RPT_MOUSE, &r, sizeof(r));
}

struct KeyMapping { const char* name; uint8_t code; };
struct ConsumerMapping { const char* name; uint16_t code; };

static const KeyMapping SPECIAL_KEYS[] = {
    {"enter",KEY_RETURN},{"return",KEY_RETURN},{"tab",KEY_TAB},
    {"escape",KEY_ESC},{"esc",KEY_ESC},{"backspace",KEY_BACKSPACE},
    {"delete",KEY_DELETE},{"insert",KEY_INSERT},{"home",KEY_HOME},
    {"end",KEY_END},{"pageup",KEY_PAGE_UP},{"pagedown",KEY_PAGE_DOWN},
    {"up",KEY_UP_ARROW},{"down",KEY_DOWN_ARROW},
    {"left",KEY_LEFT_ARROW},{"right",KEY_RIGHT_ARROW},
    {"capslock",KEY_CAPS_LOCK},
    {"f1",KEY_F1},{"f2",KEY_F2},{"f3",KEY_F3},{"f4",KEY_F4},
    {"f5",KEY_F5},{"f6",KEY_F6},{"f7",KEY_F7},{"f8",KEY_F8},
    {"f9",KEY_F9},{"f10",KEY_F10},{"f11",KEY_F11},{"f12",KEY_F12},
    {nullptr,0}
};

static const KeyMapping MODIFIER_KEYS[] = {
    {"ctrl",KEY_LEFT_CTRL},{"control",KEY_LEFT_CTRL},
    {"shift",KEY_LEFT_SHIFT},{"alt",KEY_LEFT_ALT},
    {"gui",KEY_LEFT_GUI},{"win",KEY_LEFT_GUI},
    {"meta",KEY_LEFT_GUI},{"super",KEY_LEFT_GUI},
    {nullptr,0}
};

// HID Consumer Control usage codes (Usage Page 0x0C).
static const ConsumerMapping CONSUMER_KEYS[] = {
    {"volume_up",0x00E9},{"vol_up",0x00E9},{"volumeup",0x00E9},
    {"volume_down",0x00EA},{"vol_down",0x00EA},{"volumedown",0x00EA},
    {"mute",0x00E2},
    {"play_pause",0x00CD},{"playpause",0x00CD},{"play",0x00CD},
    {"next",0x00B5},{"next_track",0x00B5},
    {"prev",0x00B6},{"previous",0x00B6},{"prev_track",0x00B6},
    {"stop",0x00B7},
    {"power",0x0030},
    {"brightness_up",0x006F},{"brightness_down",0x0070},
    {"home",0x0223},{"ac_home",0x0223},
    {"back",0x0224},{"ac_back",0x0224},
    {nullptr,0}
};

static uint8_t lookup(const KeyMapping* table, const char* name) {
    for (int i = 0; table[i].name; i++)
        if (strcasecmp(table[i].name, name) == 0) return table[i].code;
    return 0;
}

uint8_t lookupSpecialKey(const char* name) { return lookup(SPECIAL_KEYS, name); }
uint8_t lookupModifierKey(const char* name) { return lookup(MODIFIER_KEYS, name); }

uint16_t lookupConsumerKey(const char* name) {
    if (!name) return 0;
    for (int i = 0; CONSUMER_KEYS[i].name; i++)
        if (strcasecmp(CONSUMER_KEYS[i].name, name) == 0) return CONSUMER_KEYS[i].code;
    return 0;
}

// ---- realtime raw report streaming -------------------------------------

bool hidStreamReport(uint8_t report_id, const uint8_t* data, size_t len) {
    if (!hidEnabled()) return false;
    switch (report_id) {
        case ROOK_HID_RPT_KEYBOARD:
            if (len != 8) return false;
            Keyboard.sendReport((KeyReport*)data);  // 8-byte boot report
            return true;
        case ROOK_HID_RPT_MOUSE:
            return Mouse.sendRaw(data, len);        // 7-byte RookMouseReport
        case ROOK_HID_RPT_CONSUMER:
            if (len != 2) return false;
            return Mouse.sendReportId(ROOK_HID_RPT_CONSUMER, data, len);
        default:
            return false;
    }
}

void hidReleaseAll() {
    uint8_t zero_kb[8] = {0};
    RookMouseReport zero_m = {0, 0, 0, 0, 0};
    Keyboard.sendReport((KeyReport*)zero_kb);
    Mouse.sendRaw((const uint8_t*)&zero_m, sizeof(zero_m));
}
