#include "prof.h"

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
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
    info.keys = info.lan = info.cap = 0;
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
        e.c.type = type;
        if (r.bad) return false;
        if (e.key < 64) {
            info.keys |= 1ULL << e.key;
            if (type == LAN) info.lan |= 1ULL << e.key;
            if (type == CAP) info.cap |= 1ULL << e.key;
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
    if (r.bad || info.name.empty()) return false;
    return want < 0 || found;
}

static void put_str(std::string& o, const std::string& v) {
    o += (char)v.size();
    o += v;
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
        } else if (e.c.type == CAP) {
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
    xSemaphoreTake(mu, portMAX_DELAY);
    idx = got;
    xSemaphoreGive(mu);
}

std::vector<Info> list() {
    xSemaphoreTake(mu, portMAX_DELAY);
    auto out = idx;
    xSemaphoreGive(mu);
    return out;
}

bool find(const std::string& name, Info& out) {
    if (name.empty()) return false;
    xSemaphoreTake(mu, portMAX_DELAY);
    bool hit = false;
    for (auto& i : idx)
        if (same(i.name, name)) { out = i; hit = true; break; }
    xSemaphoreGive(mu);
    return hit;
}

bool linked_to(const std::string& lan_id, Info& out) {
    if (lan_id.empty()) return false;
    xSemaphoreTake(mu, portMAX_DELAY);
    bool hit = false;
    for (auto& i : idx)
        if (!i.link.empty() && same(i.link, lan_id)) { out = i; hit = true; break; }
    xSemaphoreGive(mu);
    return hit;
}

size_t total_bytes() {
    xSemaphoreTake(mu, portMAX_DELAY);
    size_t t = 0;
    for (auto& i : idx) t += i.bytes;
    xSemaphoreGive(mu);
    return t;
}

bool code(const Info& p, int key, Code& out, std::string& err) {
    if (!has(p, key)) { err = p.name + " has nothing for " + key_name(key); return false; }
    char k[8];
    slot_key(p.slot, k);
    std::string b;
    Info i;
    if (!settings::blob(k, b) || !decode(b, i, key, &out, nullptr)) {
        err = "profile " + p.name + " could not be read back";
        return false;
    }
    return true;
}

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

bool save(const std::string& name_in, bool set_link, const std::string& link_in, JsonObjectConst keys, bool merge,
          std::string& err, bool& created, int icon) {
    std::string name = clean_name(name_in);
    created = false;
    if (name.empty()) {
        err = "bad args: name is 1.." + std::to_string(MAX_NAME) + " characters";
        return false;
    }
    if (link_in.size() > MAX_LINK || !printable(link_in)) { err = "bad args: link is a device id, at most 40 characters"; return false; }
    if (icon >= ICON_COUNT) { err = "bad args: icon is one of " + icon_names(); return false; }
    // Everything given is parsed and checked before anything stored is touched.
    std::vector<Entry> given;
    std::vector<uint8_t> drop;
    if (!keys.isNull()) {
        for (JsonPairConst kv : keys) {
            std::string k = kv.key().c_str();
            int id = key_id(k);
            if (id < 0) { err = "bad args: no key called " + k + "; keys are " + key_names(); return false; }
            if (kv.value().isNull()) { drop.push_back((uint8_t)id); continue; }
            Entry e;
            e.key = (uint8_t)id;
            if (!parse_code(k, kv.value(), e.c, err)) return false;
            for (auto& g : given)
                if (g.key == e.key) { err = "bad args: " + k + " is given twice"; return false; }
            given.push_back(std::move(e));
        }
    }

    xSemaphoreTake(mu, portMAX_DELAY);
    int at = -1;
    for (size_t i = 0; i < idx.size(); i++)
        if (same(idx[i].name, name)) at = (int)i;
    std::vector<Entry> es;
    std::string link = set_link ? link_in : (at >= 0 ? idx[at].link : "");
    uint8_t ic = icon >= 0 ? (uint8_t)icon : at >= 0 ? idx[at].icon : (uint8_t)ICON_NONE;
    if (at >= 0) {
        if (merge) {
            char k[8];
            slot_key(idx[at].slot, k);
            std::string b;
            Info i;
            if (!settings::blob(k, b) || !decode(b, i, -1, nullptr, &es)) es.clear();   // unreadable: start over
        }
    } else if ((int)idx.size() >= MAX) {
        xSemaphoreGive(mu);
        err = "full: the remote keeps " + std::to_string(MAX) + " profiles; profile_delete one first";
        return false;
    }
    for (uint8_t d : drop)
        for (size_t i = 0; i < es.size(); i++)
            if (es[i].key == d) { es.erase(es.begin() + i); break; }
    for (auto& g : given) {
        bool replaced = false;
        for (auto& e : es)
            if (e.key == g.key) { e = std::move(g); replaced = true; break; }
        if (!replaced) es.push_back(std::move(g));
    }
    given.clear();
    std::string blob = encode(name, link, es, ic);
    es.clear();
    es.shrink_to_fit();
    size_t others = 0;
    for (size_t i = 0; i < idx.size(); i++)
        if ((int)i != at) others += idx[i].bytes;
    if (blob.size() > MAX_BYTES || others + blob.size() > MAX_TOTAL) {
        xSemaphoreGive(mu);
        err = "full: that profile would take " + std::to_string(blob.size()) + " bytes (at most " +
              std::to_string(MAX_BYTES) + " a profile, " + std::to_string(MAX_TOTAL) + " in all; the others use " +
              std::to_string(others) + ")";
        return false;
    }
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
    if (!settings::set_blob(k, blob)) {
        xSemaphoreGive(mu);
        err = "the remote's settings storage is full (" + std::to_string(blob.size()) + " bytes would not fit)";
        return false;
    }
    Info i;
    decode(blob, i, -1, nullptr, nullptr);
    i.slot = slot;
    if (at >= 0) idx[at] = i;
    else idx.push_back(i);
    created = at < 0;
    xSemaphoreGive(mu);
    return true;
}

bool remove(const std::string& name, std::string& err) {
    xSemaphoreTake(mu, portMAX_DELAY);
    for (size_t i = 0; i < idx.size(); i++) {
        if (!same(idx[i].name, name)) continue;
        char k[8];
        slot_key(idx[i].slot, k);
        settings::remove(k);
        idx.erase(idx.begin() + i);
        xSemaphoreGive(mu);
        return true;
    }
    xSemaphoreGive(mu);
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

bool rename(const std::string& name, const std::string& to_in, std::string& err) {
    std::string to = clean_name(to_in);
    if (to.empty()) { err = "a name is 1.." + std::to_string(MAX_NAME) + " characters"; return false; }
    xSemaphoreTake(mu, portMAX_DELAY);
    int at = -1;
    bool taken = false;
    for (size_t i = 0; i < idx.size(); i++) {
        if (same(idx[i].name, name)) at = (int)i;
        else if (same(idx[i].name, to)) taken = true;
    }
    std::vector<Entry> es;
    char k[8];
    std::string b;
    Info i;
    if (at < 0 || taken) {
        xSemaphoreGive(mu);
        err = at < 0 ? "no profile called " + name : "there is already a profile called " + to;
        return false;
    }
    slot_key(idx[at].slot, k);
    if (!settings::blob(k, b) || !decode(b, i, -1, nullptr, &es)) {
        xSemaphoreGive(mu);
        err = "profile " + name + " could not be read back";
        return false;
    }
    b = encode(to, i.link, es, i.icon);
    es.clear();
    if (!settings::set_blob(k, b)) {
        xSemaphoreGive(mu);
        err = "the remote's settings storage is full";
        return false;
    }
    decode(b, i, -1, nullptr, nullptr);
    i.slot = idx[at].slot;
    idx[at] = i;
    xSemaphoreGive(mu);
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
    for (int t = 0; t < 2; t++) {               // which of them are lan, cap (the rest are ir)
        uint64_t m = t ? p.cap : p.lan;
        if (!m) continue;
        JsonArray a = o[t ? "cap_keys" : "lan_keys"].to<JsonArray>();
        for (int i = 0; i < NKEYS; i++)
            if (m >> i & 1) a.add(KEYS[i]);
    }
    o["bytes"] = p.bytes;
}

}  // namespace prof
