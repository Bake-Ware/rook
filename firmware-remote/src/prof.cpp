#include "prof.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <new>
#include <strings.h>
#include "ir.h"
#include "settings.h"

// A profile's blob (settings key irp<slot>):
//   1 (format), name length, name, link length, link, key count,
//   then a key each: key id, type, and
//     type 0 (NEC): address low, address high, command
//     type 1 (raw): carrier (varint, Hz), count (varint), durations (varints, us)
//     type 2 (lan): the key to send (length, name; blank: the key's own)
//     type 3 (cap): cap, worker, args (JSON), each length then text
//   then, when the profile has an icon, one byte: the icon. (Builds before
//   0.12.2 stop reading after the keys, so they still read a blob with it.)
namespace prof {

static const char* const ICON_NAMES[ICON_COUNT] = {"", "tv", "projector", "speaker", "streaming", "light", "phone",
                                                   "car", "game", "generic"};
static const char* const ICON_LABELS[ICON_COUNT] = {"", "TV", "Projector", "Speaker", "Streaming box", "Light",
                                                    "Phone", "Car", "Game controller", "Generic"};

const char* icon_name(int icon) { return icon > 0 && icon < ICON_COUNT ? ICON_NAMES[icon] : "generic"; }
const char* icon_label(int icon) { return icon > 0 && icon < ICON_COUNT ? ICON_LABELS[icon] : "Generic"; }

int icon_id(const std::string& in) {
    std::string n;
    for (char c : in) n += c == ' ' || c == '-' ? '_' : (char)tolower((unsigned char)c);
    if (n.empty() || n == "none") return ICON_NONE;
    if (n == "streaming_box" || n == "stream" || n == "box") return ICON_STREAMING;
    if (n == "game_controller" || n == "controller" || n == "console") return ICON_GAME;
    if (n == "television") return ICON_TV;
    if (n == "lamp" || n == "lights") return ICON_LIGHT;
    for (int i = 1; i < ICON_COUNT; i++)
        if (n == ICON_NAMES[i]) return i;
    return -1;
}

std::string icon_names() {
    std::string s;
    for (int i = 1; i < ICON_COUNT; i++) s += (i > 1 ? ", " : "") + std::string(ICON_NAMES[i]);
    return s;
}

static const char* const KEYS[] = {
    "power", "vol_up", "vol_down", "mute", "input", "up", "down", "left", "right", "ok", "back", "home",
    "menu", "play", "pause", "rewind", "forward", "ch_up", "ch_down", "info", "exit", "stop", "next", "prev",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"};
static const int NKEYS = sizeof KEYS / sizeof KEYS[0];

static SemaphoreHandle_t mu;
static std::vector<Info> idx;                  // under mu

// mu held for a scope: let go however the scope is left (an early return, or
// an exception, which would otherwise leave every later caller waiting).
struct Lock {
    Lock() { xSemaphoreTake(mu, portMAX_DELAY); }
    ~Lock() { xSemaphoreGive(mu); }
    Lock(const Lock&) = delete;
};

// Room to allocate n bytes in one piece, with some to spare: profile changes
// come from the band (sizes from outside), and the C3's heap is in pieces.
static bool room_for(size_t n) { return ESP.getMaxAllocHeap() > n + 2048; }

static const char* const OOM = "out of memory for that profile change; try fewer keys at once";

// f() with a failed allocation turned into false and err = OOM. err has room
// for its message reserved first, so saying so allocates nothing (when even
// that reserve fails, the answer is false with err blank).
template <class F>
static bool guarded(std::string& err, F f) {
    try {
        err.reserve(200);
    } catch (const std::bad_alloc&) {
        return false;
    }
    try {
        return f();
    } catch (const std::bad_alloc&) {
        err.clear();
        if (err.capacity() >= strlen(OOM)) err.assign(OOM);
        return false;
    }
}

struct Entry {
    uint8_t key;
    Code c;
};

static void slot_key(uint8_t slot, char out[8]) { snprintf(out, 8, "irp%u", (unsigned)slot); }

static bool same(const std::string& a, const std::string& b) {
    return a.size() == b.size() && strcasecmp(a.c_str(), b.c_str()) == 0;
}

int key_id(const std::string& name) {
    std::string n = name;
    if (n == "bt_menu" || n == "settings") n = "menu";
    else if (n == "select" || n == "center" || n == "enter") n = "ok";
    else if (n == "volume_up") n = "vol_up";
    else if (n == "volume_down") n = "vol_down";
    for (int i = 0; i < NKEYS; i++)
        if (n == KEYS[i]) return i;
    return -1;
}

const char* key_name(int id) { return id >= 0 && id < NKEYS ? KEYS[id] : ""; }

bool valid_key(const std::string& n) {
    if (key_id(n) >= 0) return true;
    if (n.empty() || n.size() > MAX_KEY) return false;
    for (char c : n)
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) return false;
    return true;
}

int key_of(const Info& p, const std::string& name) {
    int id = key_id(name);
    if (id >= 0) return id;
    for (size_t i = 0; i < p.custom.size(); i++)
        if (p.custom[i] == name) return CUSTOM + (int)i;
    return -1;
}

std::string key_label(const Info& p, int id) {
    if (id >= 0 && id < NKEYS) return KEYS[id];
    if (id >= CUSTOM && id - CUSTOM < (int)p.custom.size()) return p.custom[id - CUSTOM];
    return "";
}

std::string key_names() {
    std::string s;
    for (int i = 0; i < NKEYS; i++) s += (i ? ", " : "") + std::string(KEYS[i]);
    return s;
}

bool repeats(int key) {
    std::string n = key_name(key);
    for (const char* r : {"vol_up", "vol_down", "up", "down", "left", "right", "ch_up", "ch_down", "rewind", "forward"})
        if (n == r) return true;
    return false;
}

// ---- the blob ---------------------------------------------------------------------

static void put_var(std::string& o, uint32_t v) {
    while (v >= 0x80) {
        o += (char)((v & 0x7F) | 0x80);
        v >>= 7;
    }
    o += (char)v;
}

struct Reader {
    const uint8_t* p;
    size_t n, at = 0;
    bool bad = false;
    uint8_t u8() {
        if (at >= n) { bad = true; return 0; }
        return p[at++];
    }
    uint32_t var() {
        uint32_t v = 0;
        for (int sh = 0; sh < 35; sh += 7) {
            uint8_t b = u8();
            v |= (uint32_t)(b & 0x7F) << sh;
            if (!(b & 0x80)) return v;
        }
        bad = true;
        return 0;
    }
    std::string str() {
        size_t l = u8();
        if (bad || at + l > n) { bad = true; return ""; }
        std::string s((const char*)p + at, l);
        at += l;
        return s;
    }
};

// Reads the header into info (keys too). With want >= 0 and out: that key's
// code; with all: every entry. False: not a profile blob (or no such key).
static bool decode(const std::string& b, Info& info, int want, Code* out, std::vector<Entry>* all) {
    Reader r{(const uint8_t*)b.data(), b.size()};
    if (r.u8() != 1) return false;
    info.name = r.str();
    info.link = r.str();
    info.keys = info.lan = info.cap = info.rook = 0;
    info.custom.clear();
    info.ckeys = info.clan = info.ccap = info.crook = 0;
    info.bytes = (uint16_t)b.size();
    int n = r.u8();
    bool found = false;
    for (int i = 0; i < n && !r.bad; i++) {
        Entry e;
        e.key = r.u8();
        uint8_t type = r.u8();
        bool keep = all || (out && e.key == want);
        if (type == 0) {
            e.c.raw = false;
            e.c.address = r.u8();
            e.c.address |= (uint16_t)r.u8() << 8;
            e.c.command = r.u8();
        } else if (type == 1) {
            e.c.raw = true;
            e.c.carrier = r.var();
            uint32_t cnt = r.var();
            if (cnt > MAX_RAW) return false;
            if (keep) e.c.d.reserve(cnt);
            for (uint32_t k = 0; k < cnt && !r.bad; k++) {
                uint32_t v = r.var();
                if (keep) e.c.d.push_back(v);
            }
        } else if (type == LAN) {
            e.c.key = r.str();
        } else if (type == CAP) {
            e.c.cap = r.str();
            e.c.worker = r.str();
            e.c.args = r.str();
        } else {
            return false;
        }
        e.c.type = type == CAP && e.c.cap.empty() ? (uint8_t)ROOK : type;       // a cap with no name: rook
        if (r.bad) return false;
        if (e.key < CUSTOM) {
            info.keys |= 1ULL << e.key;
            if (e.c.type == LAN) info.lan |= 1ULL << e.key;
            if (e.c.type == CAP) info.cap |= 1ULL << e.key;
            if (e.c.type == ROOK) info.rook |= 1ULL << e.key;
        } else if (e.key < CUSTOM + MAX_CUSTOM) {       // its name comes in the table after the keys
            uint32_t b = 1u << (e.key - CUSTOM);
            info.ckeys |= b;
            if (e.c.type == LAN) info.clan |= b;
            if (e.c.type == CAP) info.ccap |= b;
            if (e.c.type == ROOK) info.crook |= b;
        }
        if (out && e.key == want) {
            *out = e.c;
            found = true;
        }
        if (all) all->push_back(std::move(e));
    }
    info.icon = ICON_NONE;
    if (!r.bad && r.at < r.n) {                // the icon, after the keys
        uint8_t ic = r.u8();
        if (ic < ICON_COUNT) info.icon = ic;
    }
    if (!r.bad && r.at < r.n) {                // the custom keys' names, after the icon (0.12.9; earlier builds stop before)
        int n = r.u8();
        for (int i = 0; i < n && i < MAX_CUSTOM && !r.bad; i++) info.custom.push_back(r.str());
        if (r.bad) info.custom.clear();
    }
    // a custom key with no name in the table is not one
    uint32_t named = info.custom.size() >= 32 ? 0xFFFFFFFFu : ((1u << info.custom.size()) - 1);
    info.ckeys &= named;
    info.clan &= named;
    info.ccap &= named;
    info.crook &= named;
    if (r.bad || info.name.empty()) return false;
    return want < 0 || found;
}

static void put_str(std::string& o, const std::string& v) {
    o += (char)v.size();
    o += v;
}

// One entry as stored (key id, type, its fields).
static void encode_entry(std::string& o, const Entry& e) {
    o += (char)e.key;
    if (e.c.type == LAN) {
        o += (char)LAN;
        put_str(o, e.c.key);
    } else if (e.c.type == CAP || e.c.type == ROOK) {   // rook: a cap with no name, worker or args
        o += (char)CAP;
        put_str(o, e.c.cap);
        put_str(o, e.c.worker);
        put_str(o, e.c.args);
    } else if (!e.c.raw) {
        o += (char)0;
        o += (char)(e.c.address & 0xFF);
        o += (char)(e.c.address >> 8);
        o += (char)e.c.command;
    } else {
        o += (char)1;
        put_var(o, e.c.carrier);
        put_var(o, (uint32_t)e.c.d.size());
        for (uint32_t v : e.c.d) put_var(o, v);
    }
}

// Past one stored entry, keeping nothing: its key id. False: not readable.
static bool skip_entry(Reader& r, uint8_t& key) {
    key = r.u8();
    uint8_t type = r.u8();
    if (type == 0) {
        r.u8(); r.u8(); r.u8();
    } else if (type == 1) {
        r.var();
        uint32_t cnt = r.var();
        if (cnt > MAX_RAW) return false;
        for (uint32_t k = 0; k < cnt && !r.bad; k++) r.var();
    } else if (type == LAN) {
        size_t l = r.u8();
        r.at += l;
    } else if (type == CAP) {
        for (int k = 0; k < 3 && !r.bad; k++) r.at += r.u8();
    } else {
        return false;
    }
    if (r.at > r.n) r.bad = true;
    return !r.bad;
}

static std::string encode(const std::string& name, const std::string& link, const std::vector<Entry>& es, uint8_t icon) {
    std::string o;
    o += (char)1;
    o += (char)name.size();
    o += name;
    o += (char)link.size();
    o += link;
    o += (char)es.size();
    for (auto& e : es) {
        o += (char)e.key;
        if (e.c.type == LAN) {
            o += (char)LAN;
            put_str(o, e.c.key);
        } else if (e.c.type == CAP || e.c.type == ROOK) {   // rook: a cap with no name, worker or args
            o += (char)CAP;
            put_str(o, e.c.cap);
            put_str(o, e.c.worker);
            put_str(o, e.c.args);
        } else if (!e.c.raw) {
            o += (char)0;
            o += (char)(e.c.address & 0xFF);
            o += (char)(e.c.address >> 8);
            o += (char)e.c.command;
        } else {
            o += (char)1;
            put_var(o, e.c.carrier);
            put_var(o, (uint32_t)e.c.d.size());
            for (uint32_t v : e.c.d) put_var(o, v);
        }
    }
    if (icon != ICON_NONE && icon < ICON_COUNT) o += (char)icon;
    return o;
}

// ---- the index ----------------------------------------------------------------------

void begin() {
    if (!mu) mu = xSemaphoreCreateMutex();
    std::vector<Info> got;
    for (int s = 0; s < MAX; s++) {
        char k[8];
        slot_key(s, k);
        std::string b;
        if (!settings::blob(k, b)) continue;
        Info i;
        if (!decode(b, i, -1, nullptr, nullptr)) continue;    // unreadable: its slot is reused at the next save
        i.slot = (uint8_t)s;
        got.push_back(i);
    }
    Lock l;
    idx = got;
}

std::vector<Info> list() {
    Lock l;
    return idx;
}

bool find(const std::string& name, Info& out) {
    if (name.empty()) return false;
    Lock l;
    for (auto& i : idx)
        if (same(i.name, name)) { out = i; return true; }
    return false;
}

bool linked_to(const std::string& lan_id, Info& out) {
    if (lan_id.empty()) return false;
    Lock l;
    for (auto& i : idx)
        if (!i.link.empty() && same(i.link, lan_id)) { out = i; return true; }
    return false;
}

size_t total_bytes() {
    Lock l;
    size_t t = 0;
    for (auto& i : idx) t += i.bytes;
    return t;
}

static bool code_read(const Info& p, int key, Code& out, std::string& err);

bool code(const Info& p, int key, Code& out, std::string& err) {
    return guarded(err, [&] { return code_read(p, key, out, err); });
}

// The blob of the profile the keys last came through, kept in RAM (at most
// MAX_BYTES), so a key press reads no flash: the settings' storage is shared
// with the Wi-Fi driver's, and a flash write stalls everything. Its own lock
// (cmu), held only while the cache is read or replaced; dropped when that
// profile is changed or deleted.
static SemaphoreHandle_t cmu;
static std::string cache_blob;
static int cache_slot = -1;
static uint16_t cache_bytes = 0;
static uint32_t cache_reads = 0;               // flash reads made for codes (the host test counts them)

static void cache_drop(int slot) {             // slot -1: whatever is cached
    if (!cmu) return;
    xSemaphoreTake(cmu, portMAX_DELAY);
    if (slot < 0 || slot == cache_slot) {
        cache_slot = -1;
        std::string().swap(cache_blob);
    }
    xSemaphoreGive(cmu);
}

// p's blob in the cache (read from flash when it is not). Under cmu.
static bool cache_fill(const Info& p, std::string& err) {
    if (cache_slot == p.slot && cache_bytes == p.bytes && !cache_blob.empty()) return true;
    if (!room_for(2 * (size_t)p.bytes)) { err = "out of memory to read " + p.name + "'s code"; return false; }
    char k[8];
    slot_key(p.slot, k);
    std::string b;
    cache_reads++;
    if (!settings::blob(k, b)) { err = "profile " + p.name + " could not be read back"; return false; }
    cache_blob.swap(b);
    cache_slot = p.slot;
    cache_bytes = (uint16_t)cache_blob.size();
    return true;
}

static bool code_read(const Info& p, int key, Code& out, std::string& err) {
    if (!has(p, key)) { err = p.name + " has nothing for " + key_label(p, key); return false; }
    if (!cmu) cmu = xSemaphoreCreateMutex();
    xSemaphoreTake(cmu, portMAX_DELAY);
    struct Give { ~Give() { xSemaphoreGive(cmu); } } give;
    if (!cache_fill(p, err)) return false;
    Info i;
    if (!decode(cache_blob, i, key, &out, nullptr)) {
        err = "profile " + p.name + " could not be read back";
        return false;
    }
    return true;
}

bool warm(const Info& p) {
    std::string err;
    return guarded(err, [&] {
        if (!cmu) cmu = xSemaphoreCreateMutex();
        xSemaphoreTake(cmu, portMAX_DELAY);
        struct Give { ~Give() { xSemaphoreGive(cmu); } } give;
        return cache_fill(p, err);
    });
}

uint32_t flash_reads() { return cache_reads; }

// ---- changes -------------------------------------------------------------------------

static bool printable(const std::string& s);

static bool parse_code(const std::string& key, JsonVariantConst v, Code& c, std::string& err) {
    JsonObjectConst o = v.as<JsonObjectConst>();
    if (o.isNull()) {
        err = "bad args: keys." + key + " is {protocol: nec, address, command}, {raw: [...], carrier}, {lan: true | key} or {cap, worker, args}";
        return false;
    }
    if (!o["lan"].isNull()) {                  // the key on the linked device: this one's own name, or another
        c.type = LAN;
        if (o["lan"].is<bool>()) {
            if (!o["lan"].as<bool>()) { err = "bad args: keys." + key + ".lan is true or a key name (null takes the key out)"; return false; }
        } else {
            c.key = o["lan"] | "";
            if (c.key.empty() || c.key.size() > 24 || !printable(c.key)) {
                err = "bad args: keys." + key + ".lan is true or a key name of 1..24 characters";
                return false;
            }
        }
        return true;
    }
    if (!o["rook"].isNull()) {                 // the press to the hub, nothing else
        if (!o["rook"].is<bool>() || !o["rook"].as<bool>()) {
            err = "bad args: keys." + key + ".rook is true (null takes the key out)";
            return false;
        }
        c.type = ROOK;
        return true;
    }
    if (!o["cap"].isNull()) {                  // a Rook cap call, for the hub to make
        c.type = CAP;
        c.cap = o["cap"] | "";
        c.worker = o["worker"] | "";
        size_t dot = c.cap.find('.');
        if (c.cap.size() > 48 || dot == std::string::npos || dot == 0 || dot + 1 == c.cap.size() || !printable(c.cap)) {
            err = "bad args: keys." + key + ".cap is a cap's name, plugin.verb (at most 48 characters)";
            return false;
        }
        if (c.worker.size() > 40 || !printable(c.worker)) { err = "bad args: keys." + key + ".worker is a worker's name or id, at most 40 characters"; return false; }
        if (!o["args"].isNull()) {
            if (!o["args"].is<JsonObjectConst>()) { err = "bad args: keys." + key + ".args is a map"; return false; }
            serializeJson(o["args"], c.args);
            if (c.args.size() > MAX_ARGS) {
                err = "bad args: keys." + key + ".args is " + std::to_string(c.args.size()) + " bytes as JSON (at most " + std::to_string(MAX_ARGS) + ")";
                return false;
            }
            if (c.args == "{}") c.args.clear();
        }
        return true;
    }
    if (!o["raw"].isNull()) {
        JsonArrayConst a = o["raw"].as<JsonArrayConst>();
        if (a.isNull() || a.size() == 0 || a.size() > MAX_RAW) {
            err = "bad args: keys." + key + ".raw is 1.." + std::to_string(MAX_RAW) + " durations in microseconds";
            return false;
        }
        c.raw = true;
        c.type = RAW;
        long long carrier = 38000;
        if (!o["carrier"].isNull()) carrier = o["carrier"].is<long long>() ? o["carrier"].as<long long>() : 0;
        if (carrier < 20000 || carrier > 100000) { err = "bad args: keys." + key + ".carrier is 20000..100000 Hz"; return false; }
        c.carrier = (uint32_t)carrier;
        uint64_t total = 0;
        size_t sent = a.size() % 2 == 0 ? a.size() - 1 : a.size();     // a trailing space is not sent
        c.d.reserve(a.size());
        for (size_t i = 0; i < a.size(); i++) {
            JsonVariantConst d = a[i];
            if (!d.is<long long>() || d.as<long long>() < 1 || d.as<long long>() > 1000000) {
                err = "bad args: keys." + key + ".raw durations are whole microseconds, 1..1000000";
                return false;
            }
            uint32_t us = (uint32_t)d.as<long long>();
            if (i < sent) {
                if (i % 2 == 0 && us > ir::MAX_MARK_US) {
                    err = "refused: keys." + key + " has a mark of " + std::to_string(us) + " us (max " +
                          std::to_string(ir::MAX_MARK_US) + ")";
                    return false;
                }
                total += us;
            }
            c.d.push_back(us);
        }
        if (total > ir::MAX_TOTAL_US) {
            err = "refused: keys." + key + " lasts " + std::to_string((uint32_t)total) + " us (max " +
                  std::to_string(ir::MAX_TOTAL_US) + ")";
            return false;
        }
        return true;
    }
    std::string proto = o["protocol"] | "nec";
    if (proto != "nec") { err = "bad args: keys." + key + ".protocol is nec (or give raw)"; return false; }
    if (!o["address"].is<int>() || !o["command"].is<int>()) {
        err = "bad args: keys." + key + " needs address (0..65535) and command (0..255)";
        return false;
    }
    int a = o["address"].as<int>(), cm = o["command"].as<int>();
    if (a < 0 || a > 0xFFFF || cm < 0 || cm > 0xFF) {
        err = "bad args: keys." + key + ": address is 0..65535, command 0..255";
        return false;
    }
    c.raw = false;
    c.type = NEC;
    c.address = (uint16_t)a;
    c.command = (uint8_t)cm;
    return true;
}

static bool printable(const std::string& s) {
    for (unsigned char ch : s)
        if (ch < 0x20 || ch == 0x7F) return false;
    return true;
}

std::string clean_name(const std::string& in) {
    std::string name = in;
    while (!name.empty() && name.back() == ' ') name.pop_back();
    while (!name.empty() && name.front() == ' ') name.erase(0, 1);
    if (name.empty() || name.size() > MAX_NAME || !printable(name)) return "";
    return name;
}



// Past a stored string (length, bytes).
static void skip_str(Reader& r) {
    size_t l = r.u8();
    if (r.bad || r.at + l > r.n) r.bad = true;
    else r.at += l;
}

// The change is made on the stored bytes: the entries kept are copied as they
// are (never decoded), the ones given are encoded once, and the new blob is
// put together in one allocation of its exact size, after checking there is
// a piece of memory that big (the old way decoded every key into a vector,
// several KB at once, and a failed allocation then killed the chip).
static bool save_bytes(const std::string& name_in, bool set_link, const std::string& link_in, JsonObjectConst keys,
                       bool merge, std::string& err, bool& created, int icon) {
    std::string name = clean_name(name_in);
    created = false;
    if (name.empty()) {
        err = "bad args: name is 1.." + std::to_string(MAX_NAME) + " characters";
        return false;
    }
    if (link_in.size() > MAX_LINK || !printable(link_in)) { err = "bad args: link is a device id, at most 40 characters"; return false; }
    if (icon >= ICON_COUNT) { err = "bad args: icon is one of " + icon_names(); return false; }
    // Everything given is parsed, checked and encoded before anything stored is touched.
    // A key given: a named one (key: its id) or a custom one (cname: its name,
    // its id set once the new name table is known).
    struct Given { uint8_t key; std::string enc, cname; };
    std::vector<Given> given;
    std::vector<uint8_t> drop;
    std::vector<std::string> drop_custom;
    size_t given_bytes = 0;
    if (!keys.isNull()) {
        if (keys.size() > (size_t)(NKEYS + MAX_CUSTOM)) {
            err = "bad args: at most " + std::to_string(NKEYS + MAX_CUSTOM) + " keys";
            return false;
        }
        if (!room_for(keys.size() * (sizeof(Given) + 16) + sizeof(Entry))) { err = OOM; return false; }
        given.reserve(keys.size());
        drop.reserve(keys.size());
        for (JsonPairConst kv : keys) {
            std::string k = kv.key().c_str();
            int id = key_id(k);
            if (id < 0 && !valid_key(k)) {
                err = "bad args: " + k + " is not a key name: a named key (" + key_names() +
                      ") or any name the remote reports, [a-z0-9_], at most " + std::to_string(MAX_KEY) + " characters";
                return false;
            }
            std::string cname = id < 0 ? k : "";
            for (auto& g : given)
                if (id >= 0 ? g.key == id && g.cname.empty() : g.cname == cname) { err = "bad args: " + k + " is given twice"; return false; }
            if (kv.value().isNull()) {
                if (id >= 0) drop.push_back((uint8_t)id);
                else drop_custom.push_back(cname);
                continue;
            }
            Entry e;
            e.key = id >= 0 ? (uint8_t)id : (uint8_t)CUSTOM;       // a custom key's id is set below
            if (!parse_code(k, kv.value(), e.c, err)) return false;
            Given g{e.key, "", cname};
            if (!room_for(16 + e.c.d.size() * 3 + e.c.args.size() + e.c.cap.size() + e.c.worker.size())) { err = OOM; return false; }
            encode_entry(g.enc, e);
            given_bytes += g.enc.size();
            given.push_back(std::move(g));
        }
    }

    Lock l;
    int at = -1;
    for (size_t i = 0; i < idx.size(); i++)
        if (same(idx[i].name, name)) at = (int)i;
    if (at < 0 && (int)idx.size() >= MAX) {
        err = "full: the remote keeps " + std::to_string(MAX) + " profiles; profile_delete one first";
        return false;
    }
    std::string link = set_link ? link_in : (at >= 0 ? idx[at].link : "");
    uint8_t ic = icon >= 0 ? (uint8_t)icon : at >= 0 ? idx[at].icon : (uint8_t)ICON_NONE;
    // The stored entries that stay (merging): where they are in the old blob,
    // and for a custom key its name (the old blob's table, after its keys).
    std::string old;
    struct Kept { uint16_t from, len; int8_t cust; };       // cust: index in names below, -1: a named key
    Kept keep[CUSTOM + MAX_CUSTOM];
    size_t nkeep = 0, keep_bytes = 0;
    std::vector<std::string> names;                          // the new table: the kept custom keys', then the given
    names.reserve(MAX_CUSTOM);
    if (at >= 0 && merge) {
        char k[8];
        slot_key(idx[at].slot, k);
        if (!room_for(idx[at].bytes)) { err = OOM; return false; }
        Info oi;
        if (settings::blob(k, old) && decode(old, oi, -1, nullptr, nullptr)) {
            Reader r{(const uint8_t*)old.data(), old.size()};
            bool ok = r.u8() == 1;
            skip_str(r);
            skip_str(r);
            int n = ok ? r.u8() : 0;
            for (int i = 0; i < n && ok && nkeep < CUSTOM + MAX_CUSTOM; i++) {
                size_t from = r.at;
                uint8_t key;
                if (!skip_entry(r, key)) { ok = false; break; }
                bool replaced = false;
                if (key < CUSTOM) {
                    for (auto& g : given) replaced = replaced || (g.cname.empty() && g.key == key);
                    for (uint8_t d : drop) replaced = replaced || d == key;
                    if (replaced) continue;
                    keep[nkeep++] = {(uint16_t)from, (uint16_t)(r.at - from), -1};
                } else {
                    if (key - CUSTOM >= (int)oi.custom.size()) continue;       // no name: not a key
                    const std::string& cn = oi.custom[key - CUSTOM];
                    for (auto& g : given) replaced = replaced || g.cname == cn;
                    for (auto& d : drop_custom) replaced = replaced || d == cn;
                    if (replaced) continue;
                    keep[nkeep++] = {(uint16_t)from, (uint16_t)(r.at - from), (int8_t)names.size()};
                    names.push_back(cn);
                }
                keep_bytes += r.at - from;
            }
            if (!ok || r.bad) {                                // unreadable: start over
                nkeep = keep_bytes = 0;
                names.clear();
            }
        }
    }
    // The given custom keys join the table; each one's id is its place in it.
    for (auto& g : given) {
        if (g.cname.empty()) continue;
        if ((int)names.size() >= MAX_CUSTOM) {
            err = "full: a profile has at most " + std::to_string(MAX_CUSTOM) + " keys besides the named ones";
            return false;
        }
        g.enc[0] = (char)(CUSTOM + names.size());
        names.push_back(g.cname);
    }
    size_t table = 0;
    for (auto& n : names) table += 1 + n.size();
    size_t count = nkeep + given.size();
    size_t total = 1 + 1 + name.size() + 1 + link.size() + 1 + keep_bytes + given_bytes +
                   (names.empty() ? (ic ? 1 : 0) : 1 + 1 + table);
    size_t others = 0;
    for (size_t i = 0; i < idx.size(); i++)
        if ((int)i != at) others += idx[i].bytes;
    if (count > 255 || total > MAX_BYTES || others + total > MAX_TOTAL) {
        err = "full: that profile would take " + std::to_string(total) + " bytes (at most " + std::to_string(MAX_BYTES) +
              " a profile, " + std::to_string(MAX_TOTAL) + " in all; the others use " + std::to_string(others) + ")";
        return false;
    }
    if (!room_for(total)) { err = OOM; return false; }
    std::string blob;
    blob.reserve(total);
    blob += (char)1;
    put_str(blob, name);
    put_str(blob, link);
    blob += (char)count;
    for (size_t i = 0; i < nkeep; i++) {
        size_t at0 = blob.size();
        blob.append(old, keep[i].from, keep[i].len);
        if (keep[i].cust >= 0) blob[at0] = (char)(CUSTOM + keep[i].cust);     // its place in the new table
    }
    for (auto& g : given) blob += g.enc;
    if (!names.empty()) {                    // the icon (none: 0), then the custom keys' names
        blob += (char)(ic < ICON_COUNT ? ic : ICON_NONE);
        blob += (char)names.size();
        for (auto& n : names) put_str(blob, n);
    } else if (ic != ICON_NONE && ic < ICON_COUNT) {
        blob += (char)ic;
    }
    std::string().swap(old);
    uint8_t slot = 0;
    if (at >= 0) {
        slot = idx[at].slot;
    } else {
        for (int s = 0; s < MAX; s++) {
            bool used = false;
            for (auto& i : idx) used = used || i.slot == s;
            if (!used) { slot = (uint8_t)s; break; }
        }
    }
    char k[8];
    slot_key(slot, k);
    Info i;
    if (!decode(blob, i, -1, nullptr, nullptr)) { err = "profile could not be put together"; return false; }
    if (!settings::set_blob(k, blob)) {
        err = "the remote's settings storage is full (" + std::to_string(blob.size()) + " bytes would not fit)";
        return false;
    }
    i.slot = slot;
    if (at >= 0) idx[at] = i;
    else idx.push_back(i);
    created = at < 0;
    cache_drop(slot);
    return true;
}

bool save(const std::string& name_in, bool set_link, const std::string& link_in, JsonObjectConst keys, bool merge,
          std::string& err, bool& created, int icon) {
    created = false;
    // (the sizes are checked before each allocation, so this is not expected; a throw unwinds since 0.12.6)
    return guarded(err, [&] { return save_bytes(name_in, set_link, link_in, keys, merge, err, created, icon); });
}

bool remove(const std::string& name, std::string& err) {
    Lock l;
    for (size_t i = 0; i < idx.size(); i++) {
        if (!same(idx[i].name, name)) continue;
        char k[8];
        slot_key(idx[i].slot, k);
        settings::remove(k);
        cache_drop(idx[i].slot);
        idx.erase(idx.begin() + i);
        return true;
    }
    err = "no profile called " + name;
    return false;
}

bool relink(const std::string& name, const std::string& link) {
    Info p;
    if (!find(name, p) || p.link == link) return false;
    std::string err;
    bool created;
    return save(p.name, true, link, JsonObjectConst(), true, err, created);
}

static bool rename_bytes(const std::string& name, const std::string& to_in, std::string& err);

bool rename(const std::string& name, const std::string& to_in, std::string& err) {
    return guarded(err, [&] { return rename_bytes(name, to_in, err); });
}

// The name in the stored bytes replaced; the rest is copied as it is.
static bool rename_bytes(const std::string& name, const std::string& to_in, std::string& err) {
    std::string to = clean_name(to_in);
    if (to.empty()) { err = "a name is 1.." + std::to_string(MAX_NAME) + " characters"; return false; }
    Lock l;
    int at = -1;
    bool taken = false;
    for (size_t i = 0; i < idx.size(); i++) {
        if (same(idx[i].name, name)) at = (int)i;
        else if (same(idx[i].name, to)) taken = true;
    }
    if (at < 0 || taken) {
        err = at < 0 ? "no profile called " + name : "there is already a profile called " + to;
        return false;
    }
    if (!room_for(2 * (size_t)idx[at].bytes + to.size())) { err = OOM; return false; }
    char k[8];
    slot_key(idx[at].slot, k);
    std::string b;
    if (!settings::blob(k, b) || b.size() < 2 || b[0] != 1 || 2 + (size_t)(uint8_t)b[1] > b.size()) {
        err = "profile " + name + " could not be read back";
        return false;
    }
    size_t tail = 2 + (uint8_t)b[1];             // past the old name: link, keys, icon
    std::string nb;
    nb.reserve(b.size() - tail + 2 + to.size());
    nb += (char)1;
    put_str(nb, to);
    nb.append(b, tail, std::string::npos);
    std::string().swap(b);
    Info i;
    if (!decode(nb, i, -1, nullptr, nullptr)) { err = "profile " + name + " could not be read back"; return false; }
    if (!settings::set_blob(k, nb)) {
        err = "the remote's settings storage is full";
        return false;
    }
    i.slot = idx[at].slot;
    idx[at] = i;
    cache_drop(i.slot);
    return true;
}

bool set_icon(const std::string& name, int icon, std::string& err) {
    Info p;
    if (!find(name, p)) { err = "no profile called " + name; return false; }
    if (icon < 0 || icon >= ICON_COUNT) { err = "bad args: icon is one of " + icon_names(); return false; }
    if (p.icon == icon) return true;
    bool created;
    return save(p.name, false, "", JsonObjectConst(), true, err, created, icon);
}

void fill(const Info& p, JsonObject o) {
    o["name"] = p.name;
    if (!p.link.empty()) o["link"] = p.link;
    o["icon"] = icon_name(p.icon);
    JsonArray k = o["keys"].to<JsonArray>();
    for (int i = 0; i < NKEYS; i++)
        if (has(p, i)) k.add(KEYS[i]);
    for (size_t i = 0; i < p.custom.size(); i++)
        if (p.ckeys >> i & 1) k.add(p.custom[i]);
    for (int t = 0; t < 3; t++) {               // which of them are lan, cap, rook (the rest are ir)
        uint64_t m = t == 0 ? p.lan : t == 1 ? p.cap : p.rook;
        uint32_t cm = t == 0 ? p.clan : t == 1 ? p.ccap : p.crook;
        if (!m && !cm) continue;
        JsonArray a = o[t == 0 ? "lan_keys" : t == 1 ? "cap_keys" : "rook_keys"].to<JsonArray>();
        for (int i = 0; i < NKEYS; i++)
            if (m >> i & 1) a.add(KEYS[i]);
        for (size_t i = 0; i < p.custom.size(); i++)
            if (cm >> i & 1) a.add(p.custom[i]);
    }
    o["bytes"] = p.bytes;
}

}  // namespace prof
