// Button scanner. A button may be wired any of three ways, and each scan
// looks for all of them:
//   "P3"   an expander (PCF8574) line to ground
//   "G2"   a XIAO GPIO to ground (internal pull-up)
//   "0-2"  between two expander lines (seen from both ends)
// A button is known by that key; keys are numbered in the order they are
// first pressed (kept in NVS), or set outright with set_layout().
#include "buttons.h"
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Wire.h>

namespace {

#ifdef ROOK_BRIDGE
const int EXPECTED = 0;       // a bridge has no buttons of its own
#else
const int EXPECTED = 11;
#endif
const int DOWN_SCANS = 5;       // consecutive scans before it counts as down
const int UP_SCANS = 8;         // consecutive scans absent before it counts as up
const uint32_t LONG_MS = 600;
const int GP[] = {1, 2, 3, 4, 7, 8, 9, 43, 44};   // free XIAO pins (5/6 are I2C)
const int NG = sizeof(GP) / sizeof(GP[0]);

uint8_t addr = 0x20;
Preferences prefs;
std::vector<std::string> pairs;            // index -> key
std::vector<std::string> names;
std::function<void(const ButtonEvent&)> cb;
uint32_t seq = 0;
bool ident = false;

struct State {
    int seen = 0, missing = 0;
    bool down = false, long_sent = false;
    uint32_t since = 0;
};
State st_pair[8][8], st_line[8], st_gpio[NG];

void save() {
    JsonDocument d;
    JsonArray p = d["pairs"].to<JsonArray>();
    for (auto& s : pairs) p.add(s);
    JsonArray n = d["names"].to<JsonArray>();
    for (auto& s : names) n.add(s);
    std::string j;
    serializeJson(d, j);
    prefs.putString("layout", j.c_str());
}

void load() {
    pairs.clear();
    names.clear();
    JsonDocument d;
    if (deserializeJson(d, prefs.getString("layout", "{}").c_str())) return;
    for (JsonVariantConst v : d["pairs"].as<JsonArrayConst>()) pairs.push_back(v | "");
    for (JsonVariantConst v : d["names"].as<JsonArrayConst>()) names.push_back(v | "");
}

int index_of(const std::string& k, bool learn) {
    for (size_t i = 0; i < pairs.size(); i++) if (pairs[i] == k) return (int)i;
    if (!learn) return -1;
    pairs.push_back(k);
    save();
    return (int)pairs.size() - 1;
}

void fire(const std::string& key, const char* action, uint32_t held) {
    // Once every button is known, anything else on the lines is noise (the
    // radio's bursts look like a press of two lines at once): never a new button.
    if ((int)pairs.size() > EXPECTED) {
        pairs.resize(EXPECTED);
        if ((int)names.size() > EXPECTED) names.resize(EXPECTED);
        save();
    }
    int i = index_of(key, !ident && (int)pairs.size() < EXPECTED);
    if (i < 0 && !ident) return;
    ButtonEvent e;
    e.seq = ++seq;
    e.index = i;
    e.name = buttons::name(i);
    e.lines = key;
    e.action = action;
    e.held_ms = held;
    e.at_ms = millis();
    if (cb) cb(e);
}

// Debounce one input. Returns "press", "long" or nullptr; held gets the time down.
const char* step(State& s, bool active, uint32_t ms, uint32_t& held) {
    if (active) {
        s.missing = 0;
        if (!s.down && ++s.seen >= DOWN_SCANS) { s.down = true; s.since = ms; s.long_sent = false; }
        if (s.down && !s.long_sent && ms - s.since >= LONG_MS) {
            s.long_sent = true;
            held = ms - s.since;
            return "long";
        }
    } else {
        s.seen = 0;
        if (s.down && ++s.missing >= UP_SCANS) {
            s.down = false;
            held = ms - s.since;
            if (!s.long_sent) return "press";
        }
    }
    return nullptr;
}

std::string key_line(int i) { return "P" + std::to_string(i); }
std::string key_gpio(int i) { return "G" + std::to_string(GP[i]); }
std::string key_pair(int a, int b) { return std::to_string(a) + "-" + std::to_string(b); }

bool write(uint8_t v) {
    Wire.beginTransmission(addr);
    Wire.write(v);
    return Wire.endTransmission() == 0;
}

int read() {
    if (Wire.requestFrom(addr, (uint8_t)1) != 1) return -1;
    return Wire.read();
}

}  // namespace

namespace buttons {

bool begin(uint8_t pcf) {
    addr = pcf;
#ifdef ROOK_BRIDGE
    return true;
#endif
    prefs.begin("rook-buttons", false);
    load();
    for (int i = 0; i < NG; i++) pinMode(GP[i], INPUT_PULLUP);
    return write(0xFF);
}

std::function<void(const ButtonEvent&)>& on_event() { return cb; }

void poll() {
#ifdef ROOK_BRIDGE
    return;
#endif
    // 1. Lines held low with every line released: buttons to ground.
    if (!write(0xFF)) return;
    delayMicroseconds(40);
    int idle = read();
    if (idle < 0) return;
    uint8_t gnd = (uint8_t)~idle;
    // 2. Line to line: pull each free line low and see which others follow.
    // Counted only when seen from both ends, which rejects noise on the weak
    // PCF8574 pull-ups (Wi-Fi transmit bursts read as phantom pairs).
    bool seen[8][8] = {};
    for (int a = 0; a < 8; a++) {
        if (gnd & (1 << a)) continue;
        if (!write(0xFF & ~(1 << a))) { write(0xFF); return; }
        delayMicroseconds(40);
        int v = read();
        if (v < 0) { write(0xFF); return; }
        for (int b = 0; b < 8; b++)
            if (b != a && !(gnd & (1 << b)) && !(v & (1 << b))) seen[a][b] = true;
    }
    write(0xFF);
    uint32_t ms = millis(), held = 0;
    for (int i = 0; i < 8; i++)
        if (const char* act = step(st_line[i], gnd & (1 << i), ms, held)) fire(key_line(i), act, held);
    for (int i = 0; i < NG; i++)
        if (const char* act = step(st_gpio[i], !digitalRead(GP[i]), ms, held)) fire(key_gpio(i), act, held);
    for (int a = 0; a < 8; a++)
        for (int b = a + 1; b < 8; b++)
            if (const char* act = step(st_pair[a][b], seen[a][b] && seen[b][a], ms, held)) fire(key_pair(a, b), act, held);
}

int count() { return (int)pairs.size(); }

std::string name(int i) {
    if (i >= 0 && i < (int)names.size() && !names[i].empty()) return names[i];
    char b[8];
    snprintf(b, sizeof b, "b%d", i + 1);
    return b;
}

std::string pair(int i) { return i >= 0 && i < (int)pairs.size() ? pairs[i] : ""; }

std::vector<int> held() {
    std::vector<int> out;
    auto add = [&](const State& s, const std::string& k) {
        if (!s.down) return;
        int i = index_of(k, false);
        if (i >= 0) out.push_back(i);
    };
    for (int i = 0; i < 8; i++) add(st_line[i], key_line(i));
    for (int i = 0; i < NG; i++) add(st_gpio[i], key_gpio(i));
    for (int a = 0; a < 8; a++)
        for (int b = a + 1; b < 8; b++) add(st_pair[a][b], key_pair(a, b));
    return out;
}

void set_names(const std::vector<std::string>& n) {
    names = n;
    save();
}

void forget_layout() {
    pairs.clear();
    save();
}

void set_identify(bool on) { ident = on; }
bool identify() { return ident; }

void set_layout(const std::vector<std::string>& p) {
    pairs = p;
    save();
}

bool learning() { return !ident && (int)pairs.size() < EXPECTED; }
int expected() { return EXPECTED; }

}  // namespace buttons
