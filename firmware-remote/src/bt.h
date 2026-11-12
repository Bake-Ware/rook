// A Bluetooth LE remote (HID over GATT: most Android TV remotes) bridged onto
// the band: this remote is its host. pair() scans for a HID device in pairing
// mode, bonds with it and remembers it; after that it is reconnected whenever
// it wakes and advertises. Its key presses come out of next_key() by name:
// the handheld's own names where the key means the same (up, ok, vol_up...),
// else bt_c<usage> / bt_k<code>, so every key is a button a job can use.
// The radio is only switched on once there is something to pair or reconnect.
#pragma once
#include <Arduino.h>
#include <functional>
#include <string>

namespace bt {

void begin(std::function<void(const char*)> log);
void pair(uint32_t seconds = 30);     // look for a remote in pairing mode and bond with it
void forget();                        // drop the remembered remote and its bond
bool saved();
std::string state();                  // off | idle | pairing | waiting | connected
std::string name();                   // the remembered remote
std::string address();
std::string last_report();            // the last raw HID report, hex (to map odd keys)
bool next_key(std::string& name);     // a key pressed since the last call

}  // namespace bt
