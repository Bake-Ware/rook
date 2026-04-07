#include "hid.h"

USBHIDKeyboard Keyboard;

struct KeyMapping { const char* name; uint8_t code; };

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

static uint8_t lookup(const KeyMapping* table, const char* name) {
    for (int i = 0; table[i].name; i++)
        if (strcasecmp(table[i].name, name) == 0) return table[i].code;
    return 0;
}

uint8_t lookupSpecialKey(const char* name) { return lookup(SPECIAL_KEYS, name); }
uint8_t lookupModifierKey(const char* name) { return lookup(MODIFIER_KEYS, name); }
