// Host test of the profile store (src/prof.cpp) under failing allocations: the
// save that killed the C3 in 0.12.5 (profile_save, merge, 8 NEC keys on a
// profile that has some already) is run again and again with the N-th
// allocation failing, for every N, and again with every allocation from the
// N-th on failing. Nothing may escape save/rename/code, nothing may be left
// half written, and the lock must be free afterwards (the next call works).
// And with no big piece of memory free, a change is refused before it is tried.
//   cd firmware-remote/test && g++ -m32 -g -fsanitize=address,undefined -fno-sanitize-recover=undefined -std=gnu++17 \
//     -Ihost -I../src -I../.pio/libdeps/bridge/ArduinoJson/src prof_test.cpp ../src/prof.cpp -o /tmp/prof_test \
//     && ASAN_OPTIONS=detect_leaks=0 /tmp/prof_test
#include "Arduino.h"
#include <ArduinoJson.h>
#include <stdio.h>
#include <map>
#include <new>
#include <string>
#include "prof.h"

HostEsp ESP;

// ---- allocations that fail: the N-th one (once) or every one from the N-th --------

static long alloc_n = 0, fail_at = -1;
static bool fail_on = false;            // from fail_at on, not just at it
static long failed = 0;
static bool exempt = false;             // the test's own bookkeeping
void* operator new(size_t n) {
    if (!exempt && fail_at >= 0) {
        long k = alloc_n++;
        if (k == fail_at || (fail_on && k > fail_at)) {
            failed++;
            throw std::bad_alloc();
        }
    }
    void* p = malloc(n ? n : 1);
    if (!p) throw std::bad_alloc();
    return p;
}
void operator delete(void* p) noexcept { free(p); }
void operator delete(void* p, size_t) noexcept { free(p); }
static void arm(long at, bool on) { alloc_n = 0; fail_at = at; fail_on = on; failed = 0; }
static void disarm() { fail_at = -1; }

// ---- the settings store (NVS on the chip: it fails by returning false, never throws) --

static std::map<std::string, std::string> nvs;
namespace settings {
bool blob(const char* key, std::string& out) {
    exempt = true;
    auto it = nvs.find(key);
    bool ok = it != nvs.end();
    out = ok ? it->second : std::string();
    exempt = false;
    return ok;
}
bool set_blob(const char* key, const std::string& value) {
    exempt = true;
    nvs[key] = value;
    exempt = false;
    return true;
}
void remove(const char* key) {
    exempt = true;
    nvs.erase(key);
    exempt = false;
}
}  // namespace settings

static int fails = 0, passes = 0;
static void expect(bool ok, const std::string& what) {
    if (ok) passes++;
    else fails++;
    printf("%s %s\n", ok ? "ok  " : "FAIL", what.c_str());
}

// The profile the save was made on: 6 keys, one of them raw, and a cap key.
static void base() {
    exempt = true;
    nvs.clear();
    exempt = false;
    prof::begin();
    JsonDocument k;
    for (const char* n : {"power", "vol_up", "vol_down", "mute"}) {
        k[n]["protocol"] = "nec";
        k[n]["address"] = 51178;
        k[n]["command"] = 3;
    }
    JsonArray raw = k["input"]["raw"].to<JsonArray>();
    for (int i = 0; i < 40; i++) raw.add(560 + i);
    k["ok"]["cap"] = "show.present";
    k["ok"]["args"]["what"] = "x";
    std::string err;
    bool created;
    if (!prof::save("TV", true, "lan:tv", k.as<JsonObjectConst>(), true, err, created, prof::ICON_TV))
        printf("base save failed: %s\n", err.c_str());
}

// The stored profile reads back whole, and the index agrees with it.
static bool consistent(int want_keys_min) {
    prof::Info p;
    if (!prof::find("TV", p)) return false;
    int n = 0;
    for (int i = 0; i < 64; i++) n += prof::has(p, i);
    if (n < want_keys_min || p.icon != prof::ICON_TV || p.link != "lan:tv") return false;
    for (int i = 0; i < 64; i++) {
        if (!prof::has(p, i)) continue;
        prof::Code c;
        std::string err;
        if (!prof::code(p, i, c, err)) return false;
    }
    return true;
}

int main() {
    // The save from the crash: merge 8 NEC keys into a profile that has 6.
    JsonDocument keys;
    for (const char* n : {"back", "home", "menu", "info", "play", "pause", "rewind", "forward"}) {
        keys[n]["protocol"] = "nec";
        keys[n]["address"] = 51178;
        keys[n]["command"] = 7;
    }
    JsonObjectConst ko = keys.as<JsonObjectConst>();

    base();
    expect(consistent(6), "the base profile: 6 keys, a raw one and a cap one, read back");
    long allocations = 0;
    {
        arm(1 << 30, false);                  // count, don't fail
        std::string err;
        bool created;
        bool ok = prof::save("TV", false, "", ko, true, err, created);
        allocations = alloc_n;
        disarm();
        expect(ok && consistent(14), "the save from the crash works with memory to spare (" + std::to_string(allocations) +
                                         " allocations)");
    }
    // Every allocation in turn fails (once), then every one from it on.
    for (int mode = 0; mode < 2; mode++) {
        int escaped = 0, half = 0, ok_count = 0, refused = 0, stuck = 0;
        for (long at = 0; at <= allocations + 2; at++) {
            base();
            std::string err;
            bool created = false, ok = false;
            arm(at, mode == 1);
            try {
                ok = prof::save("TV", false, "", ko, true, err, created);
            } catch (...) {
                escaped++;
            }
            disarm();
            if (ok) ok_count++;
            else refused++;
            if (!consistent(ok ? 14 : 6)) half++;
            // the lock is free and the store works after it
            std::string e2;
            bool c2;
            JsonDocument one;
            one["exit"]["protocol"] = "nec";
            one["exit"]["address"] = 1;
            one["exit"]["command"] = 2;
            if (!prof::save("TV", false, "", one.as<JsonObjectConst>(), true, e2, c2)) stuck++;
        }
        expect(escaped == 0, std::string(mode ? "every allocation from the N-th on failing" : "the N-th allocation failing") +
                                 ", for each N: nothing escapes save (" + std::to_string(ok_count) + " saved, " +
                                 std::to_string(refused) + " refused)");
        expect(half == 0, "  ...and the profile is either the old one or the new one, never half (" + std::to_string(half) + ")");
        expect(stuck == 0, "  ...and the next save works (the lock was let go) (" + std::to_string(stuck) + " stuck)");
    }
    // Rename, and reading a code, under the same.
    {
        int escaped = 0, half = 0;
        for (long at = 0; at < 40; at++) {
            base();
            std::string err;
            arm(at, true);
            bool ok = false;
            try {
                ok = prof::rename("TV", "Telly", err);
            } catch (...) {
                escaped++;
            }
            disarm();
            prof::Info p;
            bool old_there = prof::find("TV", p), new_there = prof::find("Telly", p);
            if (ok ? (!new_there || old_there) : (!old_there || new_there)) half++;
        }
        expect(escaped == 0 && half == 0, "rename with allocations failing: nothing escapes, old name or new, never both");
    }
    {
        base();
        int escaped = 0;
        prof::Info p;
        prof::find("TV", p);
        int raw = prof::key_id("input");
        for (long at = 0; at < 20; at++) {
            arm(at, true);
            try {
                prof::Code c;
                std::string err;
                prof::code(p, raw, c, err);
            } catch (...) {
                escaped++;
            }
            disarm();
        }
        expect(escaped == 0, "reading a raw code with allocations failing: nothing escapes");
    }
    // No piece of memory big enough: refused before anything is tried.
    {
        base();
        ESP.max_block = 2500;
        std::string err;
        bool created;
        arm(1 << 30, false);
        bool ok = prof::save("TV", false, "", ko, true, err, created);
        long n = alloc_n;
        disarm();
        ESP.max_block = 20000;
        expect(!ok && err.find("out of memory") != std::string::npos && consistent(6),
               "largest block 2.5 KB: the save is refused (" + err + "), the profile untouched, " + std::to_string(n) +
                   " allocations made");
    }
    // The codes of the profile picked come from RAM: keys read no flash. A change to it drops the copy.
    {
        base();
        prof::Info p;
        prof::find("TV", p);
        prof::warm(p);
        uint32_t reads = prof::flash_reads();
        int ok = 0;
        for (int n = 0; n < 50; n++) {
            prof::Code c;
            std::string err;
            ok += prof::code(p, prof::key_id(n % 2 ? "power" : "input"), c, err);
        }
        expect(ok == 50 && prof::flash_reads() == reads, "50 key presses after warm: every code found, " +
                                                             std::to_string(prof::flash_reads() - reads) + " flash reads");
        std::string err;
        bool created;
        prof::save("TV", false, "", ko, true, err, created);
        prof::find("TV", p);
        prof::Code c;
        bool got = prof::code(p, prof::key_id("back"), c, err);
        uint32_t after = prof::flash_reads();
        prof::code(p, prof::key_id("home"), c, err);
        expect(got && c.address == 51178 && after == reads + 1 && prof::flash_reads() == after,
               "after a save the new codes are read once, then from RAM again");
    }
    // rook: the press to the hub, nothing else; stored as a cap with no name (a build before 0.12.8 still reads it).
    {
        base();
        JsonDocument k;
        k["play"]["rook"] = true;
        k["pause"]["rook"] = true;
        std::string err;
        bool created;
        bool ok = prof::save("TV", false, "", k.as<JsonObjectConst>(), true, err, created);
        prof::Info p;
        prof::find("TV", p);
        prof::Code c;
        bool got = prof::code(p, prof::key_id("play"), c, err);
        JsonDocument f;
        prof::fill(p, f.to<JsonObject>());
        bool listed = f["rook_keys"].size() == 2 && f["cap_keys"].size() == 1;     // the base's ok key is a real cap
        exempt = true;
        std::string raw = nvs["irp0"];
        exempt = false;
        // find play's entry: key id, then type 3 (cap), then three empty strings
        uint8_t play = (uint8_t)prof::key_id("play");
        bool old_format = raw.find(std::string{(char)play, (char)3, 0, 0, 0}) != std::string::npos;
        JsonDocument bad;
        bad["play"]["rook"] = false;
        bool refused = !prof::save("TV", false, "", bad.as<JsonObjectConst>(), true, err, created);
        expect(ok && got && c.type == prof::ROOK && listed && old_format && refused,
               "rook keys: saved, read back as rook, listed in rook_keys (cap keys apart), stored as a nameless cap; rook false refused");
    }
    // Keys by any name the remote reports (bt_c77: the onn's YouTube key).
    {
        base();
        JsonDocument k;
        k["bt_c77"]["protocol"] = "nec";
        k["bt_c77"]["address"] = 4;
        k["bt_c77"]["command"] = 0x77;
        k["bt_c19c"]["rook"] = true;
        std::string err;
        bool created;
        bool ok = prof::save("TV", false, "", k.as<JsonObjectConst>(), true, err, created);
        prof::Info p;
        prof::find("TV", p);
        int yt = prof::key_of(p, "bt_c77"), pk = prof::key_of(p, "bt_c19c");
        prof::Code c, c2;
        bool got = prof::code(p, yt, c, err) && prof::code(p, pk, c2, err);
        JsonDocument f;
        prof::fill(p, f.to<JsonObject>());
        std::string keys;
        serializeJson(f["keys"], keys);
        bool listed = keys.find("bt_c77") != std::string::npos && keys.find("power") != std::string::npos &&
                      f["rook_keys"][0] == "bt_c19c";
        expect(ok && yt >= prof::CUSTOM && got && c.command == 0x77 && c2.type == prof::ROOK && listed && prof::has(p, prof::key_id("power")),
               "custom keys: bt_c77 (IR) and bt_c19c (rook) saved beside the named ones, read back, listed");
        // merge another custom key, drop one, keep the rest
        JsonDocument k2;
        k2["bt_c19c"] = nullptr;
        k2["bt_c22"]["lan"] = true;
        ok = prof::save("TV", false, "", k2.as<JsonObjectConst>(), true, err, created);
        prof::find("TV", p);
        bool merged = ok && prof::key_of(p, "bt_c19c") < 0 && prof::has(p, prof::key_of(p, "bt_c77")) &&
                      prof::has(p, prof::key_of(p, "bt_c22")) && prof::code(p, prof::key_of(p, "bt_c77"), c, err) &&
                      c.command == 0x77 && prof::has(p, prof::key_id("input"));
        expect(merged, "custom keys: a merge drops one and adds one; the others and their codes stay (the table renumbered)");
        // rename keeps them
        prof::rename("TV", "Telly", err);
        prof::find("Telly", p);
        expect(prof::has(p, prof::key_of(p, "bt_c77")) && prof::code(p, prof::key_of(p, "bt_c77"), c, err) && c.command == 0x77,
               "custom keys: kept through a rename");
        // bad names
        int refused = 0;
        for (const char* bad : {"BT_C77", "bt-c77", "a_name_much_too_long", ""}) {
            JsonDocument kb;
            kb[bad]["rook"] = true;
            if (!prof::save("Telly", false, "", kb.as<JsonObjectConst>(), true, err, created)) refused++;
        }
        expect(refused == 4 && prof::valid_key("bt_c77") && !prof::valid_key("Bad") && prof::valid_key("settings"),
               "key names: [a-z0-9_], 1..15 characters (upper case, dashes, too long, blank refused)");
        // a build before 0.12.9 stops reading after the icon: the profile reads, the named keys work, the custom ones are not there
        exempt = true;
        std::string raw = nvs["irp0"];
        exempt = false;
        prof::Info full;
        prof::find("Telly", full);
        size_t table = 1;
        for (auto& n : full.custom) table += 1 + n.size();
        std::string old_view = raw.substr(0, raw.size() - table);
        exempt = true;
        nvs["irp0"] = old_view;
        exempt = false;
        prof::begin();
        prof::Info ov;
        bool reads = prof::find("Telly", ov) && prof::has(ov, prof::key_id("power")) && prof::code(ov, prof::key_id("power"), c, err) &&
                     ov.custom.empty() && ov.icon == prof::ICON_TV;
        expect(reads, "a profile with custom keys, read as a build before 0.12.9 reads it: whole, named keys working, no custom ones");
    }
    // ...and under failing allocations
    {
        JsonDocument k;
        for (const char* n : {"bt_c77", "bt_c19c", "bt_c22", "back"}) k[n]["rook"] = true;
        int escaped = 0, stuck = 0;
        for (long at = 0; at < 40; at++) {
            base();
            std::string err;
            bool created;
            arm(at, true);
            try {
                prof::save("TV", false, "", k.as<JsonObjectConst>(), true, err, created);
            } catch (...) {
                escaped++;
            }
            disarm();
            JsonDocument one;
            one["bt_c77"]["rook"] = true;
            if (!prof::save("TV", false, "", one.as<JsonObjectConst>(), true, err, created)) stuck++;
        }
        expect(escaped == 0 && stuck == 0, "custom keys with allocations failing from each point on: nothing escapes, the next save works");
    }
    // The routes a profile's keys take, worked out when it is read or saved (Info::routes).
    {
        base();                                   // NEC and raw keys (IR), a cap key (Rook), linked (Wi-Fi)
        prof::Info p;
        prof::find("TV", p);
        bool all3 = p.routes == (prof::ROUTE_IR | prof::ROUTE_ROOK | prof::ROUTE_WIFI);
        JsonDocument k;
        k["bt_c77"]["rook"] = true;
        std::string err;
        bool created;
        prof::save("Deck", true, "", k.as<JsonObjectConst>(), false, err, created);
        prof::find("Deck", p);
        bool rook_only = p.routes == prof::ROUTE_ROOK;
        JsonDocument k2;
        k2["ok"]["protocol"] = "nec";
        k2["ok"]["address"] = 1;
        k2["ok"]["command"] = 2;
        prof::save("Deck", false, "", k2.as<JsonObjectConst>(), true, err, created);
        prof::find("Deck", p);
        bool plus_ir = p.routes == (prof::ROUTE_IR | prof::ROUTE_ROOK);
        prof::begin();                            // read back from the store
        prof::find("Deck", p);
        expect(all3 && rook_only && plus_ir && p.routes == (prof::ROUTE_IR | prof::ROUTE_ROOK),
               "routes: IR, Rook and Wi-Fi from the keys and the link, kept up to date by saves and when read back");
    }
    printf("%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
