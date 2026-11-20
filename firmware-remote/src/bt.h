// A Bluetooth LE remote (HID over GATT: most Android TV remotes) bridged onto
// the band: this remote is its host. pair() scans for a HID device in pairing
// mode, bonds with it and remembers it; after that it is reconnected whenever
// it wakes and advertises. look() lists the HID remotes heard instead (Settings >
// Pair BT), and pair() with an address bonds with the one picked from it. Its key presses come out of next_key() by name:
// the handheld's own names where the key means the same (up, ok, vol_up...),
// else bt_c<usage> / bt_k<code>, so every key is a button a job can use.
// The radio is only switched on once there is something to pair or reconnect.
#pragma once
#include <Arduino.h>
#include <functional>
#include <string>
#include <vector>

namespace bt {

void begin(std::function<void(const char*)> log);
// Look for a remote in pairing mode and bond with it: the first one heard, or
// the one at address. seconds 0: until one is found. need_mode: only one that
// says it is in pairing mode (discoverable) is taken (the bridge pairing on its
// own; by hand any HID remote heard is).
void pair(uint32_t seconds = 30, const std::string& address = "", bool need_mode = false);
std::string pair_error();             // why the last pair() did not bond (blank: it did, or still going)
// The HID remotes heard advertising, for a list to pick from: on, the radio
// listens for them (actively, for their names) and bonds with none by itself.
struct Heard { std::string address, name; int rssi; bool pairing; uint32_t ago_ms; uint8_t type; };   // pairing: it says it is in pairing mode
void look(bool on);
bool looking();
std::vector<Heard> heard();           // in the order first heard
// No scanning while on (a firmware download wants the radio); a remote already
// connected stays connected.
void pause(bool on);
// With more than one remote bonded: keep listening (passively) while one is
// connected, so another that wakes takes over. Off by default since 0.12.5
// (the radio then does Wi-Fi, a BLE connection and a scan at once); off, a
// bonded remote takes over when the one connected has gone to sleep. Kept.
void set_switching(bool on);
bool switching();
void forget();                        // drop every remote bonded with
// Up to 3 remotes stay bonded (a new one past that pushes out the one used
// least lately); one is connected at a time, and another bonded remote that
// wakes takes over from it.
struct BondInfo { std::string address, name; bool connected; };
std::vector<BondInfo> bonded();       // most recently used first
bool forget(const std::string& address);   // drop that one; false: not bonded
bool bonds_full();                    // a new one would push one out
std::string oldest_bond();            // the one it would push out
bool saved();                         // any remote bonded
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
int voice_level();                    // how loud it is hearing now, 0..100 (for a meter)
int voice_codec();                    // 1: ADPCM 8 kHz, 2: ADPCM 16 kHz
bool voice_spoke();                   // the utterance (open or just ended) had speech in it
// The Assistant key starts an utterance and any key ends it (voice_stop);
// when none does, two seconds of quiet after speech do. The remote itself
// closes its microphone after about 15 s.
// Audio since the last call, once there are min_bytes of it or the utterance
// ended. done: this was the last of an utterance. False: nothing to take.
bool voice_take(std::string& out, bool& done, size_t min_bytes);
std::string services();               // the connected remote's GATT services

// Google's Find My Remote service, on remotes with a buzzer: the host makes
// the remote beep so it can be found.
bool find_ready();                    // the connected remote has the service
void find(int level);                 // 1..3: beep (quiet to loud); 0: stop
std::string find_info();              // what was written and what the remote said back (hex)
std::string voice_info();             // its answer about its voice service, and its last control message (hex)

// Raw access to the remote's IR-over-BLE service (D343BFC0-...; the onn
// remote has it), to find its code format by experiment. Characteristics by
// their last two hex digits: c1 IR_PROG_CONTROL (01 start, 00 finish), c2
// IR_KEY_ID (Android keycode, 2 bytes little-endian), c3 IR_CODE (the code,
// split over writes), c4 IR_SUPPRESS (keycodes that stay BLE), c5 IR_KEY_EVENT
// (notifies 00 down / 01 up, then the keycode). Every write is with response
// and runs on the Bluetooth task; each answers one line per write,
// "<char> <hex>: ok" or the error (att 0x.. from the remote, err N locally).
// The key events also come out of next_key() as ir_<keycode hex>, but only
// when no HID key arrived with them.
struct IrEvent { uint32_t ago_ms; bool down; int key; std::string raw; };
bool ir_ready();                                  // the connected remote has the service
std::vector<std::string> ir_write(const std::string& suffix, const std::string& bytes);   // chunked at the MTU
std::vector<std::string> ir_program(uint16_t keycode, const std::string& code);           // start, key, code (20-byte chunks), finish
std::vector<std::string> ir_suppress(const std::vector<uint16_t>& keys);
std::vector<IrEvent> ir_events(uint32_t within_ms);   // IR_KEY_EVENT notifications seen lately, oldest first
void ir_forget_events();

}  // namespace bt
