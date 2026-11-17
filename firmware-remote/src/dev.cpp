#include "dev.h"
#include <HTTPClient.h>
#include <WiFi.h>
#include <map>
#include "settings.h"
#include "ws.h"

namespace dev {

struct Job {
    std::string action, device, arg, error, out;
    bool ok = false, abandoned = false;
    SemaphoreHandle_t done = nullptr;      // the caller waits on it; null: nobody does
};

static QueueHandle_t q;
static SemaphoreHandle_t jobs_mu;
static std::function<void(const char*)> log_;
static std::function<void(const std::string&)> note_;

static void say(const char* fmt, ...) {
    char b[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (log_) log_(b);
}

static std::string lower(std::string s) {
    for (auto& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

// ---- the keys LG TVs gave when they were paired --------------------------------

static std::string lg_key(const std::string& id) {
    JsonDocument d;
    if (deserializeJson(d, settings::kv("lan_keys"))) return "";
    return d[id] | "";
}

// Move a key filed under one id (a TV's address, before its MAC was known) to another.
static void move_lg_key(const std::string& from, const std::string& to) {
    if (from == to) return;
    JsonDocument d;
    if (deserializeJson(d, settings::kv("lan_keys")) || !d.is<JsonObject>()) return;
    std::string k = d[from] | "";
    if (k.empty()) return;
    d.remove(from);
    if (std::string(d[to] | "").empty()) d[to] = k;
    std::string json;
    serializeJson(d, json);
    settings::set_kv("lan_keys", json);
}

// The device's key: under its id, else under its address (paired while known by that).
static std::string lg_key(const lan::Device& d) {
    std::string k = lg_key(d.id);
    if (k.empty() && !d.ip.empty() && d.ip != d.id) k = lg_key(d.ip);
    return k;
}

static void set_lg_key(const std::string& id, const std::string& key) {
    JsonDocument d;
    deserializeJson(d, settings::kv("lan_keys"));
    if (!d.is<JsonObject>()) d.to<JsonObject>();
    if (key.empty()) d.remove(id);
    else d[id] = key;
    std::string json;
    serializeJson(d, json);
    settings::set_kv("lan_keys", json);
}

bool paired(const lan::Device& d) {
    return d.driver == "roku" || (d.driver == "webos" && !lg_key(d).empty());
}

std::vector<std::string> supports(const lan::Device& d) {
    if (d.driver == "roku") return {"key", "launch", "power"};
    if (d.driver == "webos") return {"key", "launch", "power", "pair"};
    return {};
}

void fill(const lan::Device& d, JsonObject o) {
    lan::fill(d, o);
    JsonArray s = o["supports"].to<JsonArray>();
    for (auto& a : supports(d)) s.add(a);
    if (!d.driver.empty()) o["paired"] = paired(d);
}

// The apps the remote's own buttons (and plain names) launch.
static bool is_app(const std::string& name) {
    for (const char* a : {"youtube", "netflix", "prime", "disney", "hulu", "appletv", "plex"})
        if (name == a) return true;
    return false;
}

// ---- Roku (ECP) --------------------------------------------------------------

static bool roku_http(const lan::Device& d, bool post, const std::string& path, std::string* body, std::string& err) {
    WiFiClient c;
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(2500);
    if (!http.begin(c, d.ip.c_str(), d.port ? d.port : 8060, path.c_str())) { err = "bad address"; return false; }
    int code = post ? http.POST("") : http.GET();
    if (code > 0 && code < 300 && body) {
        WiFiClient* in = http.getStreamPtr();
        uint32_t end = millis() + 3000;
        while (body->size() < 24000 && (int32_t)(end - millis()) > 0) {
            int n = in->available();
            if (n <= 0) {
                if (!http.connected()) break;
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            uint8_t buf[256];
            n = in->read(buf, std::min<int>(n, sizeof buf));
            if (n > 0) body->append((const char*)buf, n);
        }
    }
    http.end();
    if (code < 0) { err = "no answer from " + d.name + " (off, or not on this network)"; return false; }
    if (code >= 300) { err = d.name + " answered http " + std::to_string(code); return false; }
    return true;
}

static bool roku_key(const lan::Device& d, const std::string& name, std::string& err) {
    static const std::map<std::string, std::string> KEYS = {
        {"up", "Up"}, {"down", "Down"}, {"left", "Left"}, {"right", "Right"}, {"ok", "Select"},
        {"back", "Back"}, {"home", "Home"}, {"menu", "Info"}, {"info", "Info"}, {"vol_up", "VolumeUp"},
        {"vol_down", "VolumeDown"}, {"mute", "VolumeMute"}, {"power", "Power"}, {"play", "Play"},
        {"pause", "Play"}, {"rewind", "Rev"}, {"forward", "Fwd"}, {"replay", "InstantReplay"},
        {"ch_up", "ChannelUp"}, {"ch_down", "ChannelDown"}};
    auto it = KEYS.find(name);
    std::string key = it != KEYS.end() ? it->second : name;
    if (it == KEYS.end() && (name.empty() || !isupper((unsigned char)name[0]))) {
        err = "a Roku has no key called " + name;
        return false;
    }
    return roku_http(d, true, "/keypress/" + key, nullptr, err);
}

static bool roku_launch(const lan::Device& d, const std::string& app, std::string& err) {
    static const std::map<std::string, std::string> IDS = {
        {"youtube", "837"}, {"netflix", "12"}, {"prime", "13"}, {"hulu", "2285"},
        {"disney", "291097"}, {"plex", "13535"}, {"appletv", "551012"}};
    std::string want = lower(app), id;
    bool digits = !want.empty();
    for (char c : want) digits = digits && isdigit((unsigned char)c);
    if (digits) id = want;
    else if (IDS.count(want)) id = IDS.at(want);
    else {
        // <app id="12" ...>Netflix</app>: the first whose name holds what was asked for
        std::string xml;
        if (!roku_http(d, false, "/query/apps", &xml, err)) return false;
        for (size_t p = 0; (p = xml.find("<app id=\"", p)) != std::string::npos;) {
            p += 9;
            size_t q1 = xml.find('"', p), gt = xml.find('>', p), end = xml.find("</app>", p);
            if (q1 == std::string::npos || gt == std::string::npos || end == std::string::npos) break;
            if (lower(xml.substr(gt + 1, end - gt - 1)).find(want) != std::string::npos) {
                id = xml.substr(p, q1 - p);
                break;
            }
            p = end;
        }
        if (id.empty()) { err = d.name + " has no app called " + app; return false; }
    }
    return roku_http(d, true, "/launch/" + id, nullptr, err);
}

// ---- LG webOS (SSAP) ------------------------------------------------------------

static struct {
    std::string id;
    WsClient main, ptr;                   // the session, and its socket for button presses
    uint32_t used = 0;
    int next = 1;
} lg;

// The permission list the open-source clients send (aiowebostv, Apache-2.0); the hub's tv plugin sends the same.
static const char* const LG_PERMS[] = {
    "APP_TO_APP", "CLOSE", "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
    "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_TEXT", "CONTROL_INPUT_TV",
    "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_POWER", "CONTROL_TV_SCREEN", "LAUNCH", "LAUNCH_WEBAPP",
    "READ_APP_STATUS", "READ_COUNTRY_INFO", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
    "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_LGE_TV_INPUT_EVENTS", "READ_NETWORK_STATE",
    "READ_NOTIFICATIONS", "READ_POWER_STATE", "READ_RUNNING_APPS", "READ_SETTINGS", "READ_TV_CHANNEL_LIST",
    "READ_TV_CURRENT_TIME", "READ_UPDATE_INFO", "SEARCH", "TEST_OPEN", "TEST_PROTECTED", "TEST_SECURE",
    "UPDATE_FROM_REMOTE_APP", "WRITE_NOTIFICATION_ALERT", "WRITE_NOTIFICATION_TOAST", "WRITE_SETTINGS"};

static void lg_close() {
    lg.ptr.close();
    lg.main.close();
    lg.id.clear();
}

// Open and sign in. Without a key, prompt waits a minute for a person to accept on the TV.
static bool lg_open(const lan::Device& d, bool prompt, std::string& err) {
    if (lg.id == d.id && lg.main.connected()) {
        lg.used = millis();
        return true;
    }
    lg_close();
    std::string key = lg_key(d);
    if (!key.empty() && lg_key(d.id).empty()) move_lg_key(d.ip, d.id);
    if (key.empty() && !prompt) {
        err = d.name + " is not paired: pick it in the remote's menu, or remote.device action pair";
        return false;
    }
    uint32_t t0 = millis();
    bool up = lg.main.open(d.ip, 3000, "/", false, 2000);
    // Refused at once: newer TVs only take TLS on 3001. Timed out: it is off, and 3001 would time out too.
    if (!up && millis() - t0 < 1500) up = lg.main.open(d.ip, 3001, "/", true, 6000);
    if (!up) {
        err = "no answer from " + d.name + " (off, or not on this network)";
        return false;
    }
    JsonDocument reg;
    reg["type"] = "register";
    reg["id"] = "register_0";
    JsonObject p = reg["payload"].to<JsonObject>();
    p["forcePairing"] = false;
    p["pairingType"] = "PROMPT";
    if (!key.empty()) p["client-key"] = key;
    JsonObject m = p["manifest"].to<JsonObject>();
    m["appVersion"] = "1.1";
    m["manifestVersion"] = 1;
    JsonArray perms = m["permissions"].to<JsonArray>();
    for (const char* s : LG_PERMS) perms.add(s);
    std::string msg;
    serializeJson(reg, msg);
    if (!lg.main.send(msg)) {
        err = d.name + " closed the connection";
        lg_close();
        return false;
    }
    uint32_t end = millis() + (key.empty() ? 60000 : 8000);
    for (;;) {
        int32_t left = (int32_t)(end - millis());
        if (left <= 0) {
            err = key.empty() ? "nobody accepted the prompt on " + d.name : d.name + " did not answer the sign-in";
            lg_close();
            return false;
        }
        std::string in;
        int r = lg.main.recv(in, left);
        if (r < 0) {
            err = d.name + " closed the connection";
            lg_close();
            return false;
        }
        if (r == 0) continue;
        JsonDocument a;
        if (deserializeJson(a, in) || std::string(a["id"] | "") != "register_0") continue;
        std::string type = a["type"] | "";
        if (type == "registered") {
            std::string k = a["payload"]["client-key"] | "";
            if (!k.empty() && k != key) set_lg_key(d.id, k);
            break;
        }
        if (type == "error") {
            err = d.name + " refused: " + std::string(a["error"] | "sign-in failed");
            lg_close();
            return false;
        }
        // {"type":"response","payload":{"pairingType":"PROMPT"}}: waiting for a person
    }
    lg.id = d.id;
    lg.used = millis();
    lan::learn_mac(d.id);
    return true;
}

static bool lg_request(const std::string& uri, JsonObjectConst payload, JsonDocument& out, std::string& err) {
    JsonDocument req;
    std::string id = std::to_string(lg.next++);
    req["type"] = "request";
    req["id"] = id;
    req["uri"] = "ssap://" + uri;
    if (payload.isNull()) req["payload"].to<JsonObject>();
    else req["payload"] = payload;
    std::string msg;
    serializeJson(req, msg);
    if (!lg.main.send(msg)) { err = "the TV closed the connection"; return false; }
    uint32_t end = millis() + 8000;
    for (;;) {
        int32_t left = (int32_t)(end - millis());
        if (left <= 0) { err = "the TV did not answer"; return false; }
        std::string in;
        int r = lg.main.recv(in, left);
        if (r < 0) { err = "the TV closed the connection"; return false; }
        if (r == 0) continue;
        JsonDocument a;
        if (deserializeJson(a, in)) continue;
        std::string got = a["id"].is<const char*>() ? std::string(a["id"].as<const char*>()) : std::to_string(a["id"] | -1);
        if (got != id) continue;
        if (std::string(a["type"] | "") == "error") {
            const char* e = a["error"] | (const char*)nullptr;
            if (!e) e = a["payload"]["errorText"] | "request failed";
            err = std::string("the TV said: ") + e;
            return false;
        }
        if (a["payload"]["returnValue"].is<bool>() && !a["payload"]["returnValue"].as<bool>()) {
            err = std::string("the TV said: ") + (a["payload"]["errorText"] | "request failed");
            return false;
        }
        out.set(a["payload"]);
        lg.used = millis();
        return true;
    }
}

static bool lg_button(const lan::Device& d, const std::string& name, std::string& err) {
    for (int attempt = 0; attempt < 2; attempt++) {
        if (!(lg.id == d.id && lg.ptr.connected())) {
            lg.ptr.close();
            if (!lg_open(d, false, err)) return false;
            JsonDocument out;
            if (!lg_request("com.webos.service.networkinput/getPointerInputSocket", JsonObjectConst(), out, err)) {
                lg_close();
                continue;
            }
            // ws[s]://host[:port]/path
            std::string url = out["socketPath"] | "";
            bool tls = url.rfind("wss://", 0) == 0;
            size_t h = url.find("://");
            if (h == std::string::npos) { err = "the TV gave no input socket"; lg_close(); continue; }
            h += 3;
            size_t slash = url.find('/', h);
            std::string hostport = url.substr(h, slash == std::string::npos ? std::string::npos : slash - h);
            std::string path = slash == std::string::npos ? "/" : url.substr(slash);
            size_t colon = hostport.rfind(':');
            std::string host = colon == std::string::npos ? hostport : hostport.substr(0, colon);
            uint16_t port = colon == std::string::npos ? (tls ? 443 : 80) : (uint16_t)atoi(hostport.c_str() + colon + 1);
            // Two TLS connections don't fit beside Bluetooth on the small chips: the session gives way.
            if (tls && ESP.getFreeHeap() < 70000) lg.main.close();
            if (!lg.ptr.open(host, port, path, tls, 6000)) {
                err = "the TV's input socket would not open";
                lg_close();
                continue;
            }
            err.clear();
        }
        if (lg.ptr.send("type:button\nname:" + name + "\n\n")) {
            lg.used = millis();
            return true;
        }
        lg.ptr.close();
        err = "the TV dropped the key";
    }
    return false;
}

static bool lg_key_press(const lan::Device& d, const std::string& name, std::string& err) {
    static const std::map<std::string, std::string> KEYS = {
        {"up", "UP"}, {"down", "DOWN"}, {"left", "LEFT"}, {"right", "RIGHT"}, {"ok", "ENTER"},
        {"back", "BACK"}, {"home", "HOME"}, {"menu", "MENU"}, {"info", "INFO"}, {"exit", "EXIT"},
        {"vol_up", "VOLUMEUP"}, {"vol_down", "VOLUMEDOWN"}, {"mute", "MUTE"}, {"play", "PLAY"},
        {"pause", "PAUSE"}, {"stop", "STOP"}, {"rewind", "REWIND"}, {"forward", "FASTFORWARD"},
        {"ch_up", "CHANNELUP"}, {"ch_down", "CHANNELDOWN"}};
    auto it = KEYS.find(name);
    if (it == KEYS.end() && (name.empty() || !isupper((unsigned char)name[0]))) {
        err = "an LG TV has no key called " + name;
        return false;
    }
    return lg_button(d, it != KEYS.end() ? it->second : name, err);
}

static bool lg_launch(const lan::Device& d, const std::string& app, std::string& err) {
    static const std::map<std::string, std::string> IDS = {
        {"youtube", "youtube.leanback.v4"}, {"netflix", "netflix"}, {"prime", "amazon"},
        {"disney", "com.disney.disneyplus-prod"}, {"hulu", "hulu"}, {"appletv", "com.apple.appletv"},
        {"plex", "cdp-30"}};
    std::string want = lower(app);
    if (!lg_open(d, false, err)) return false;
    JsonDocument p, out;
    p["id"] = IDS.count(want) ? IDS.at(want) : app;
    return lg_request("system.launcher/launch", p.as<JsonObjectConst>(), out, err);
}

// Off when it is on; Wake-on-LAN when it is off or does not answer.
static bool lg_power(const lan::Device& d, std::string& did, std::string& err) {
    std::string why;
    if (lg_key(d).empty()) { lg_open(d, false, err); return false; }
    if (lg_open(d, false, why)) {
        JsonDocument out;
        std::string state = "Active";
        if (lg_request("com.webos.service.tvpower/power/getPowerState", JsonObjectConst(), out, why))
            state = out["state"] | "Active";
        if (state == "Active" || state == "Screen Off" || state == "Screen Saver") {
            bool ok = lg_request("system/turnOff", JsonObjectConst(), out, err);
            lg_close();
            did = "off";
            return ok;
        }
        lg_close();                        // answering, but in standby: wake it
    }
    if (d.mac.empty()) {
        err = "can't wake " + d.name + ": its MAC address isn't known yet (turn it on once and scan)";
        return false;
    }
    lan::wake(d.mac);
    did = "wake sent";
    return true;
}

// ---- the task ------------------------------------------------------------------

static bool perform(Job& j) {
    for (int t = 0; t < 80 && WiFi.status() != WL_CONNECTED; t++) vTaskDelay(pdMS_TO_TICKS(100));   // it may be waking up
    if (WiFi.status() != WL_CONNECTED) { j.error = "no Wi-Fi"; return false; }
    if (j.action == "scan") {
        j.out = std::to_string(lan::scan());
        return true;
    }
    lan::Device d;
    if (!lan::find(j.device, d)) {
        j.error = "no device called " + j.device + ": remote.scan finds what is on this network";
        return false;
    }
    if (j.action == "forget") {
        if (lg.id == d.id) lg_close();
        set_lg_key(d.id, "");
        if (!d.ip.empty() && d.ip != d.id) set_lg_key(d.ip, "");     // one kept from before its MAC was known
        lan::forget(d.id);
        return true;
    }
    if (d.driver.empty()) {
        std::string kinds;
        for (auto& k : d.kinds) kinds += (kinds.empty() ? "" : ", ") + k;
        j.error = "the remote has no driver for " + d.name + " (" + kinds + ")";
        return false;
    }
    std::string action = j.action, arg = j.arg;
    if (action == "key" && arg == "power") action = "power";
    else if (action == "key" && is_app(arg)) action = "launch";
    if (d.driver == "roku") {
        if (action == "key") return roku_key(d, arg, j.error);
        if (action == "launch") return roku_launch(d, arg, j.error);
        if (action == "power") return roku_key(d, "power", j.error);
        if (action == "pair") { j.out = "a Roku needs no pairing"; return true; }
    } else if (d.driver == "webos") {
        if (action == "key") return lg_key_press(d, arg, j.error);
        if (action == "launch") return lg_launch(d, arg, j.error);
        if (action == "power") return lg_power(d, j.out, j.error);
        if (action == "pair") {
            if (lg_key(d).empty() && note_) note_("Accept\non the\nTV");
            if (lg.id == d.id) lg_close();
            bool ok = lg_open(d, true, j.error);
            if (ok) lan::keep(d.id);
            if (note_) note_(ok ? "paired" : "not\npaired");
            return ok;
        }
    }
    j.error = "bad args: action is key, launch, power, pair or forget";
    return false;
}

static void finish(Job* j) {
    xSemaphoreTake(jobs_mu, portMAX_DELAY);
    bool orphan = !j->done || j->abandoned;
    if (!orphan) xSemaphoreGive(j->done);
    xSemaphoreGive(jobs_mu);
    if (!orphan) return;                   // the waiting caller frees it
    if (!j->ok && !j->abandoned && j->action != "pair") {
        say("device: %s", j->error.c_str());
        bool unpaired = j->error.find("not paired") != std::string::npos;
        bool silent = j->error.find("no answer") != std::string::npos || j->error.find("closed") != std::string::npos ||
                      j->error.find("did not answer") != std::string::npos || j->error.find("would not open") != std::string::npos;
        if (note_ && (unpaired || silent)) note_(unpaired ? "not\npaired" : "no\nanswer");
    }
    if (j->done) vSemaphoreDelete(j->done);
    delete j;
}

static void task(void*) {
    for (;;) {
        Job* j = nullptr;
        if (xQueueReceive(q, &j, pdMS_TO_TICKS(1000)) != pdTRUE) {
            if (!lg.id.empty() && millis() - lg.used > 60000) lg_close();     // idle: let the TV go
            continue;
        }
        j->ok = perform(*j);
        finish(j);
    }
}

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> note) {
    log_ = log;
    note_ = note;
    // Runs on this task (scans and learn_mac happen here), like every other use of lg.
    lan::on_rekey([](const std::string& from, const std::string& to) {
        move_lg_key(from, to);
        if (lg.id == from) lg.id = to;
    });
    jobs_mu = xSemaphoreCreateMutex();
    q = xQueueCreate(8, sizeof(Job*));
    xTaskCreate(task, "dev", 12288, nullptr, 1, nullptr);
}

bool run(const std::string& action, const std::string& device, const std::string& arg, uint32_t wait_ms,
         std::string& err, std::string* out) {
    Job* j = new Job();
    j->action = action;
    j->device = device;
    j->arg = arg;
    if (wait_ms) j->done = xSemaphoreCreateBinary();
    if (xQueueSend(q, &j, 0) != pdTRUE) {
        if (j->done) vSemaphoreDelete(j->done);
        delete j;
        err = "busy: the remote is still on its last few actions";
        return false;
    }
    if (!wait_ms) return true;
    bool got = xSemaphoreTake(j->done, pdMS_TO_TICKS(wait_ms)) == pdTRUE;
    xSemaphoreTake(jobs_mu, portMAX_DELAY);
    if (!got) {
        if (xSemaphoreTake(j->done, 0) == pdTRUE) got = true;     // it finished just now
        else j->abandoned = true;                                  // the task frees it
    }
    xSemaphoreGive(jobs_mu);
    if (!got) {
        err = "still working on it";
        return false;
    }
    bool ok = j->ok;
    err = j->error;
    if (out) *out = j->out;
    vSemaphoreDelete(j->done);
    delete j;
    return ok;
}

}  // namespace dev
