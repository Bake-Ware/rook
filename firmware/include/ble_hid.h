#pragma once
#include <Arduino.h>

// Start BLE HID peripheral (keyboard + consumer control). Phones/PCs pair
// to this dongle as a normal BLE keyboard. Caps on the band let Claude
// send keystrokes that come out the BLE link to whatever host is bonded.
void setupBleHid();

// True while a BLE central is connected to our HID service.
bool bleHidConnected();

// ---- HID actions (called by the Telesthete cap dispatcher) ----
// Type a string of printable characters as keystrokes.
size_t bleHidType(const char* text, int delay_ms);
// Press+release a single key combo. `key` is either a single char or one
// of the special-key names from lookupSpecialKey(). `mods` is an array of
// modifier-key names (ctrl, shift, alt, gui).
bool bleHidKey(const char* key, const char* mods[], size_t mods_n);
// Press+release a consumer-control (media) key by name (volume_up, play, etc).
bool bleHidConsumer(const char* name, uint16_t code_override);
