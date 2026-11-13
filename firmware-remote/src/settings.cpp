#include "settings.h"
#include <Preferences.h>
#include <esp_random.h>
#include "rc_sig.h"
#include "rc_util.h"

static Preferences prefs;
static Enrollment enr;
static SemaphoreHandle_t mu;
static std::vector<std::string> tgts;
static int tgt = 0;

static std::string get(const char* k) {
    String s = prefs.getString(k, "");
    return std::string(s.c_str());
}

static void put(const char* k, const std::string& v) { prefs.putString(k, v.c_str()); }

static void load_targets() {
    tgts.clear();
    std::string s = get("targets");
    if (s.empty()) s = "office-tv,living-room-tv";
    size_t pos = 0;
    while (pos <= s.size()) {
        size_t c = s.find(',', pos);
        std::string item = s.substr(pos, c == std::string::npos ? std::string::npos : c - pos);
        if (!item.empty()) tgts.push_back(item);
        if (c == std::string::npos) break;
        pos = c + 1;
    }
    tgt = prefs.getInt("target", 0);
    if (tgt < 0 || tgt >= (int)tgts.size()) tgt = 0;
}

static void load_enrollment() {
    enr = Enrollment();
    std::string j = get("enroll");
    if (j.empty()) return;
    JsonDocument d;
    if (deserializeJson(d, j)) return;
    enr.worker_id = d["worker_id"] | "";
    enr.hub = d["hub"] | "";
    enr.root = d["root"] | "";
    enr.psk = d["psk"] | "";
    enr.relay = d["relay"] | "";
    enr.owner = d["owner"] | "";
    enr.url = d["url"] | "";
    enr.ok = !enr.worker_id.empty() && !enr.psk.empty() && !enr.relay.empty() && !enr.root.empty();
}

namespace settings {

void begin() {
    mu = xSemaphoreCreateMutex();
    prefs.begin("rook-remote", false);
    load_enrollment();
    load_targets();
}

const Enrollment& enrollment() { return enr; }

bool set_enrollment(JsonObjectConst b, const char* nm, std::string& err) {
    // The bundle as the hub's /enroll/poll gives it (spec 12): hub {hub_id, root,
    // url}, band {id, psk, relay, ws_url}, worker_id, owner.
    const char* hub_id = b["hub"]["hub_id"];
    const char* root = b["hub"]["root"];
    const char* band = b["band"]["id"];
    const char* psk = b["band"]["psk"];
    const char* relay = b["band"]["relay"];
    const char* wid = b["worker_id"];
    if (!hub_id || !root || !band || !psk || !wid) { err = "bundle is missing hub, band or worker_id"; return false; }
    if (!relay || !*relay) { err = "the hub gives no UDP relay for devices (enroll.relay)"; return false; }
    uint8_t pub[32];
    if (!rc::parse_pub(root, pub) || rc::key_id(pub) != hub_id) { err = "hub id is not its root key's"; return false; }
    uint8_t h[32];
    rc::sha256(psk, strlen(psk), h);
    if (rc::hex(h, 16) != band) { err = "band key is for another band"; return false; }
    if (!rc::is_hex(wid, 32)) { err = "worker_id is not 32 hex"; return false; }
    JsonDocument d;
    d["worker_id"] = wid;
    d["hub"] = hub_id;
    d["root"] = root;
    d["psk"] = psk;
    d["relay"] = relay;
    d["owner"] = b["owner"] | "";
    d["url"] = b["hub"]["url"] | "";
    std::string j;
    serializeJson(d, j);
    xSemaphoreTake(mu, portMAX_DELAY);
    put("enroll", j);
    if (nm && *nm) put("name", nm);
    prefs.remove("replay");
    load_enrollment();
    xSemaphoreGive(mu);
    return true;
}

void clear_enrollment() {
    xSemaphoreTake(mu, portMAX_DELAY);
    prefs.remove("enroll");
    prefs.remove("replay");
    load_enrollment();
    xSemaphoreGive(mu);
}

std::string name() {
    std::string n = get("name");
    return n.empty() ? "remote" : n;
}

void set_name(const std::string& n) { put("name", n); }

std::vector<std::pair<std::string, std::string>> wifi() {
    std::vector<std::pair<std::string, std::string>> out;
    JsonDocument d;
    if (deserializeJson(d, get("wifi"))) return out;
    for (JsonObjectConst n : d.as<JsonArrayConst>()) out.push_back({n["ssid"] | "", n["pass"] | ""});
    return out;
}

void wifi_add(const std::string& ssid, const std::string& pass) {
    auto nets = wifi();
    JsonDocument d;
    JsonArray a = d.to<JsonArray>();
    JsonObject first = a.add<JsonObject>();
    first["ssid"] = ssid;
    first["pass"] = pass;
    for (auto& n : nets) {
        if (n.first == ssid) continue;
        JsonObject o = a.add<JsonObject>();
        o["ssid"] = n.first;
        o["pass"] = n.second;
    }
    std::string j;
    serializeJson(d, j);
    put("wifi", j);
}

void wifi_clear() { prefs.remove("wifi"); }

void seed(uint8_t out[32]) {
    if (prefs.getBytesLength("seed") == 32) {
        prefs.getBytes("seed", out, 32);
        return;
    }
    esp_fill_random(out, 32);
    prefs.putBytes("seed", out, 32);
}

std::string replay() { return get("replay"); }
void set_replay(const std::string& blob) { put("replay", blob); }

void factory_reset() {
    prefs.clear();
    load_enrollment();
}

std::vector<std::string> targets() {
    xSemaphoreTake(mu, portMAX_DELAY);
    auto out = tgts;
    xSemaphoreGive(mu);
    return out;
}

void set_targets(const std::vector<std::string>& t) {
    std::string joined;
    for (auto& n : t) {
        if (n.empty() || n.find(',') != std::string::npos) continue;
        joined += (joined.empty() ? "" : ",") + n;
    }
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string was = tgt < (int)tgts.size() ? tgts[tgt] : "";
    put("targets", joined);
    load_targets();
    tgt = 0;
    for (int k = 0; k < (int)tgts.size(); k++) if (tgts[k] == was) tgt = k;     // the chosen one stays chosen
    prefs.putInt("target", tgt);
    xSemaphoreGive(mu);
}

int target() { return tgt; }

void set_target(int i) {
    xSemaphoreTake(mu, portMAX_DELAY);
    if (i >= 0 && i < (int)tgts.size()) {
        tgt = i;
        prefs.putInt("target", i);
    }
    xSemaphoreGive(mu);
}

std::string target_name() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string n = tgt < (int)tgts.size() ? tgts[tgt] : "";
    xSemaphoreGive(mu);
    return n;
}

int rotation() { return prefs.getInt("rot", 3) & 3; }

void set_rotation(int r) { prefs.putInt("rot", r & 3); }

std::string wifi_last() { return get("wifi_last"); }

void set_wifi_last(const std::string& ssid) {
    if (get("wifi_last") != ssid) put("wifi_last", ssid);
}

std::string bt_names() { return get("bt_names"); }

void set_bt_names(const std::string& json) { put("bt_names", json); }

}  // namespace settings
