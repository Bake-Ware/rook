// Rook remote: a XIAO ESP32-S3 handheld with 11 buttons (charlieplexed on a
// PCF8574) and a 128x32 OLED, joining the band as a core v2 worker.
//   I2C: SDA GPIO5, SCL GPIO6. PCF8574 0x20, SSD1306 0x3C.
// Button presses go to the hub as remote.button events; caps let agents show
// text, ask a question, wait for a press, read state and name the buttons.
// Setup is over USB serial (scripts/remote-enroll.py): Wi-Fi and enrollment.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <Wire.h>
#include <deque>
#include <time.h>
#include "buttons.h"
#include "rc_sig.h"
#include "rc_worker.h"
#include "remote_manifest.h"
#include "settings.h"
#include "ui.h"

#define FW_VERSION "0.1.0"
static const int PIN_SDA = 5, PIN_SCL = 6;
static const uint8_t PCF_ADDR = 0x20, OLED_ADDR = 0x3C;

static rc::Worker worker;
static bool worker_on = false;
static WiFiMulti wifi;
static SemaphoreHandle_t ev_mu;
static std::deque<ButtonEvent> recent;      // last presses, for state and wait
static volatile int ask_answer = -1;        // set by the button handler while asking
static std::string last_note;

static void logf(const char* fmt, ...) {
    char b[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    Serial.printf("# %s\n", b);
}

// ---- buttons ----------------------------------------------------------------

static bool is_name(const std::string& n, std::initializer_list<const char*> set) {
    for (const char* s : set) if (n == s) return true;
    return false;
}

static void on_button(const ButtonEvent& e) {
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    recent.push_back(e);
    while (recent.size() > 16) recent.pop_front();
    xSemaphoreGive(ev_mu);
    logf("button %s (%d, %s) %s %ums", e.name.c_str(), e.index, buttons::pair(e.index).c_str(), e.action, (unsigned)e.held_ms);
    if (buttons::identify()) {
        ui::big(e.lines);
        return;
    }
    if (ui::asking()) {
        // Named buttons navigate; otherwise tap = next option, hold = choose.
        if (is_name(e.name, {"up", "left", "prev"})) ui::ask_move(-1);
        else if (is_name(e.name, {"down", "right", "next"})) ui::ask_move(1);
        else if (is_name(e.name, {"ok", "select", "center", "enter"}) || !strcmp(e.action, "long")) ask_answer = ui::ask_selected();
        else ui::ask_move(1);
        return;
    }
    if (buttons::learning()) {
        char b[24];
        snprintf(b, sizeof b, "%s = %s", e.name.c_str(), buttons::pair(e.index).c_str());
        ui::flash(b);
    } else {
        ui::flash(e.name + (strcmp(e.action, "long") ? "" : " (hold)"));
    }
    if (worker_on) {
        JsonDocument p;
        p["button"] = e.name;
        p["action"] = e.action;
        p["index"] = e.index;
        p["held_ms"] = e.held_ms;
        worker.emit("remote.button", p.as<JsonObjectConst>());
    }
}

// ---- caps -------------------------------------------------------------------

static void fill_state(JsonObject r) {
    r["name"] = settings::name();
    r["version"] = FW_VERSION;
    JsonArray bs = r["buttons"].to<JsonArray>();
    for (int i = 0; i < buttons::count(); i++) {
        JsonObject b = bs.add<JsonObject>();
        b["index"] = i;
        b["name"] = buttons::name(i);
        b["lines"] = buttons::pair(i);
    }
    if (buttons::learning())
        r["learning"] = String("press the remaining buttons once each, in reading order (") +
                        buttons::count() + "/" + buttons::expected() + " known)";
    JsonArray h = r["held"].to<JsonArray>();
    for (int i : buttons::held()) h.add(buttons::name(i));
    JsonArray last = r["last"].to<JsonArray>();
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    uint32_t ms = millis();
    for (auto it = recent.rbegin(); it != recent.rend() && last.size() < 5; ++it) {
        JsonObject o = last.add<JsonObject>();
        o["button"] = it->name;
        o["action"] = it->action;
        o["held_ms"] = it->held_ms;
        o["age_s"] = (ms - it->at_ms) / 1000;
    }
    xSemaphoreGive(ev_mu);
    r["screen"] = ui::shown();
    r["wifi"]["ssid"] = WiFi.SSID();
    r["wifi"]["rssi"] = WiFi.RSSI();
    r["wifi"]["ip"] = WiFi.localIP().toString();
    r["hub_contact"] = worker.hub_ok();
    r["events_sent"] = worker.events_sent();
    r["events_queued"] = (unsigned)worker.events_queued();
    r["uptime_s"] = millis() / 1000;
}

static bool cap_state(JsonObjectConst, JsonObject r, std::string&) {
    fill_state(r);
    return true;
}

static bool cap_display(JsonObjectConst a, JsonObject r, std::string&) {
    ui::text(a["text"] | "", a["seconds"] | 0);
    r["screen"] = ui::shown();
    return true;
}

static bool cap_ask(JsonObjectConst a, JsonObject r, std::string& err) {
    std::vector<std::string> opts;
    for (JsonVariantConst o : a["options"].as<JsonArrayConst>()) opts.push_back(o | "");
    if (opts.empty()) opts = {"Yes", "No"};
    if (ui::asking()) { err = "the remote is already asking something"; return false; }
    ask_answer = -1;
    ui::ask_begin(a["prompt"] | "", opts);
    uint32_t deadline = millis() + (uint32_t)(a["timeout"] | 60) * 1000;
    while (ask_answer < 0 && (int32_t)(deadline - millis()) > 0) vTaskDelay(pdMS_TO_TICKS(20));
    int ans = ask_answer;
    ui::ask_end();
    if (ans < 0) {
        r["timeout"] = true;
        ui::flash("(no answer)");
        return true;
    }
    r["choice"] = opts[ans];
    r["index"] = ans;
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    if (!recent.empty()) r["button"] = recent.back().name;
    xSemaphoreGive(ev_mu);
    ui::flash(opts[ans]);
    return true;
}

static bool cap_wait(JsonObjectConst a, JsonObject r, std::string&) {
    std::vector<std::string> only;
    for (JsonVariantConst b : a["buttons"].as<JsonArrayConst>()) only.push_back(b | "");
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    uint32_t start = recent.empty() ? 0 : recent.back().seq;
    xSemaphoreGive(ev_mu);
    uint32_t deadline = millis() + (uint32_t)(a["timeout"] | 30) * 1000;
    while ((int32_t)(deadline - millis()) > 0) {
        xSemaphoreTake(ev_mu, portMAX_DELAY);
        for (auto& e : recent) {
            if (e.seq <= start) continue;
            bool want = only.empty();
            for (auto& n : only) if (n == e.name) want = true;
            if (!want) continue;
            r["button"] = e.name;
            r["action"] = e.action;
            r["held_ms"] = e.held_ms;
            r["index"] = e.index;
            xSemaphoreGive(ev_mu);
            return true;
        }
        xSemaphoreGive(ev_mu);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    r["timeout"] = true;
    return true;
}

static bool cap_map(JsonObjectConst a, JsonObject r, std::string&) {
    JsonArrayConst n = a["names"].as<JsonArrayConst>();
    if (!n.isNull() && n.size()) {
        std::vector<std::string> names;
        for (int i = 0; i < (int)n.size(); i++) {
            std::string s = n[i] | "";
            names.push_back(s.empty() ? buttons::name(i) : s);
        }
        buttons::set_names(names);
    }
    JsonArray bs = r["buttons"].to<JsonArray>();
    for (int i = 0; i < buttons::count(); i++) bs.add(buttons::name(i));
    return true;
}

// ---- serial setup (scripts/remote-enroll.py) ---------------------------------

static void reply(JsonDocument& d) {
    Serial.print("@ ");
    serializeJson(d, Serial);
    Serial.println();
}

static void handle_line(const std::string& line) {
    JsonDocument in, out;
    if (line.empty()) return;
    if (line[0] == '{') {
        if (deserializeJson(in, line)) { out["ok"] = false; out["error"] = "bad json"; reply(out); return; }
    } else {
        in["cmd"] = line.c_str();
    }
    std::string cmd = in["cmd"] | "";
    out["ok"] = true;
    const Enrollment& e = settings::enrollment();
    if (cmd == "status") {
        fill_state(out["state"].to<JsonObject>());
        out["enrolled"] = e.ok;
        out["worker_id"] = e.worker_id;
    } else if (cmd == "key") {
        uint8_t seed[32], sec[64], pub[32];
        settings::seed(seed);
        rc::key_from_seed(seed, sec, pub);
        out["key"] = rc::pub_text(pub);
        out["name"] = settings::name();
        out["enrolled"] = e.ok;
        out["worker_id"] = e.worker_id;
        out["mac"] = WiFi.macAddress();
    } else if (cmd == "wifi") {
        settings::wifi_add(in["ssid"] | "", in["pass"] | "");
        out["networks"] = (int)settings::wifi().size();
    } else if (cmd == "scan") {
        // What the radio hears (2.4 GHz only), and the saved networks' names.
        int n = WiFi.scanNetworks();
        JsonArray a = out["heard"].to<JsonArray>();
        for (int i = 0; i < n; i++) {
            JsonObject o = a.add<JsonObject>();
            o["ssid"] = WiFi.SSID(i);
            o["rssi"] = WiFi.RSSI(i);
            o["ch"] = WiFi.channel(i);
        }
        WiFi.scanDelete();
        JsonArray sv = out["saved"].to<JsonArray>();
        for (auto& w : settings::wifi()) sv.add(w.first);
        out["status"] = (int)WiFi.status();
    } else if (cmd == "wifi.clear") {
        settings::wifi_clear();
    } else if (cmd == "name") {
        settings::set_name(in["name"] | "remote");
    } else if (cmd == "enroll") {
        std::string err;
        if (!settings::set_enrollment(in["bundle"].as<JsonObjectConst>(), in["name"] | "", err)) {
            out["ok"] = false;
            out["error"] = err;
        }
    } else if (cmd == "unenroll") {
        settings::clear_enrollment();
    } else if (cmd == "show") {
        ui::text(in["text"] | "", in["seconds"] | 0);
    } else if (cmd == "names") {
        std::vector<std::string> n;
        for (JsonVariantConst v : in["names"].as<JsonArrayConst>()) n.push_back(v | "");
        buttons::set_names(n);
    } else if (cmd == "identify") {
        bool on = in["on"] | true;
        buttons::set_identify(on);
        if (on) ui::big("press"); else ui::text("", 0);
    } else if (cmd == "layout.set") {
        std::vector<std::string> p;
        for (JsonVariantConst v : in["pairs"].as<JsonArrayConst>()) p.push_back(v | "");
        buttons::set_layout(p);
        out["buttons"] = buttons::count();
    } else if (cmd == "layout.reset") {
        buttons::forget_layout();
        ui::flash("press each button in order");
    } else if (cmd == "reboot") {
        reply(out);
        delay(200);
        ESP.restart();
    } else if (cmd == "factory") {
        settings::factory_reset();
        buttons::forget_layout();
        reply(out);
        delay(200);
        ESP.restart();
    } else {
        out["ok"] = false;
        out["error"] = "commands: status key wifi wifi.clear name enroll unenroll show names layout.reset reboot factory";
    }
    reply(out);
}

static void poll_serial() {
    static std::string buf;
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (!buf.empty()) handle_line(buf);
            buf.clear();
        } else if (buf.size() < 4096) {
            buf += c;
        }
    }
}

// ---- tasks ------------------------------------------------------------------

static void rook_task(void*) {
    for (;;) {
        if (WiFi.status() == WL_CONNECTED) worker.loop();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// Joining Wi-Fi blocks for seconds; keep it off the button/serial loop.
static void wifi_task(void*) {
    for (;;) {
        if (WiFi.status() != WL_CONNECTED && !settings::wifi().empty()) {
            // Full power fails to authenticate next to an AP (reasons 2/202 at
            // -25 dBm) and its bursts upset the PCF8574 lines; 8.5 dBm is plenty.
            WiFi.setTxPower(WIFI_POWER_8_5dBm);
            wifi.run(8000);
            WiFi.setTxPower(WIFI_POWER_8_5dBm);
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

static std::string replay_blob(const rc::Gate& g) {
    std::string s;
    for (auto& r : g.replay) s += r.nonce + ":" + std::to_string((long long)r.until) + ",";
    return s;
}

static void load_replay(rc::Gate& g, const std::string& s) {
    size_t pos = 0;
    while (pos < s.size()) {
        size_t c = s.find(',', pos);
        std::string item = s.substr(pos, c - pos);
        size_t k = item.find(':');
        if (k != std::string::npos) g.replay.push_back({item.substr(0, k), atoll(item.c_str() + k + 1)});
        if (c == std::string::npos) break;
        pos = c + 1;
    }
}

void setup() {
    Serial.setRxBufferSize(4096);   // the enrollment bundle arrives as one ~1 KB line
    Serial.begin(115200);
    ev_mu = xSemaphoreCreateMutex();
    settings::begin();
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    ui::begin(OLED_ADDR);
    if (!buttons::begin(PCF_ADDR)) logf("PCF8574 not answering at 0x%02x", PCF_ADDR);
    buttons::on_event() = on_button;
    ui::status_line(0, settings::name() + "  starting");
    ui::tick();

    WiFi.onEvent([](WiFiEvent_t ev, WiFiEventInfo_t info) {
        if (ev == ARDUINO_EVENT_WIFI_STA_DISCONNECTED)
            logf("wifi: disconnected from %.32s, reason %d", (const char*)info.wifi_sta_disconnected.ssid,
                 info.wifi_sta_disconnected.reason);
        else if (ev == ARDUINO_EVENT_WIFI_STA_GOT_IP)
            logf("wifi: connected to %s, ip %s", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
    });
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(settings::name().c_str());
    for (auto& n : settings::wifi()) wifi.addAP(n.first.c_str(), n.second.c_str());
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");

    const Enrollment& e = settings::enrollment();
    if (e.ok) {
        rc::WorkerConfig c;
        c.worker_id = e.worker_id;
        c.name = settings::name();
        c.description = "Rook remote: a handheld with 11 buttons and a small screen";
        c.version = std::string(FW_VERSION "+remote.") + REMOTE_MANIFEST_VERSION;
        c.psk = e.psk;
        c.relay = e.relay;
        c.hub = e.hub;
        c.root = e.root;
        settings::seed(c.seed);
        worker.log = [](const char* m) { logf("rook: %s", m); };
        worker.status = [](JsonObject s) {
            s["buttons"] = buttons::count();
            s["held"] = (int)buttons::held().size();
        };
        if (worker.begin(c, REMOTE_MANIFEST_JSON, "remote")) {
            load_replay(worker.gate, settings::replay());
            worker.gate.on_replay_change = [] { settings::set_replay(replay_blob(worker.gate)); };
            worker.on("remote.state", cap_state);
            worker.on("remote.display", cap_display);
            worker.on("remote.map", cap_map);
            worker.on("remote.ask", cap_ask, true);
            worker.on("remote.wait", cap_wait, true);
            worker_on = true;
        } else {
            logf("enrollment is unusable (hub root key or manifest); enroll again");
        }
    }
    xTaskCreatePinnedToCore(rook_task, "rook", 16384, nullptr, 1, nullptr, 1);
    xTaskCreatePinnedToCore(wifi_task, "wifi", 4096, nullptr, 1, nullptr, 0);
    logf("Rook remote %s ready; worker %s", FW_VERSION, worker_on ? "on" : "off (not enrolled)");
}

void loop() {
    static uint32_t next_status = 0, next_scan = 0;
    uint32_t ms = millis();
    if ((int32_t)(ms - next_scan) >= 0) {
        next_scan = ms + 5;
        buttons::poll();
    }
    poll_serial();
    if ((int32_t)(ms - next_status) >= 0) {
        next_status = ms + 500;
        bool up = WiFi.status() == WL_CONNECTED;
        std::string s0 = settings::name() + "  ";
        if (!settings::enrollment().ok) s0 += "not enrolled";
        else if (!up) s0 += "no wifi";
        else if (!worker.clock_ok()) s0 += "clock...";
        else s0 += worker.hub_ok() ? "on band" : "no hub";
        ui::status_line(0, s0);
        ui::status_line(1, up ? std::string(WiFi.localIP().toString().c_str()) : (settings::wifi().empty() ? "set wifi over USB" : "joining wifi"));
        if (buttons::learning()) {
            char b[24];
            snprintf(b, sizeof b, "learn: %d/%d buttons", buttons::count(), buttons::expected());
            ui::status_line(2, b);
        } else {
            xSemaphoreTake(ev_mu, portMAX_DELAY);
            ui::status_line(2, recent.empty() ? "" : "last: " + recent.back().name);
            xSemaphoreGive(ev_mu);
        }
        char b[24];
        snprintf(b, sizeof b, "calls %u  events %u", (unsigned)worker.calls(), (unsigned)worker.events_sent());
        ui::status_line(3, worker_on ? b : "");
    }
    ui::tick();
    delay(1);
}
