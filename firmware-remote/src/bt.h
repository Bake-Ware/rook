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
std::string held();                   // the key held down now (its raw name), or blank

// The paired remote's microphone, when it has Google's voice service (ATVV:
// most Android TV remotes). The remote asks for it when its Assistant key is
// pressed; voice_start() asks it. Audio comes out of voice_take() as the
// remote sent it: ATVV frames of IMA ADPCM, decoded on the hub.
bool voice_ready();                   // the connected remote has the service
bool voice_open();                    // its microphone is open now
void voice_start();
void voice_stop();
int voice_codec();                    // 1: ADPCM 8 kHz, 2: ADPCM 16 kHz
// Audio since the last call, once there are min_bytes of it or the utterance
// ended. done: this was the last of an utterance. False: nothing to take.
bool voice_take(std::string& out, bool& done, size_t min_bytes);
std::string services();               // the connected remote's GATT services
std::string voice_info();             // its answer about its voice service, and its last control message (hex)

}  // namespace bt
