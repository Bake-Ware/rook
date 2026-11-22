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
    printf("%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
