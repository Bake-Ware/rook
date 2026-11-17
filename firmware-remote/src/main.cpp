// Rook remote: a XIAO ESP32-S3 handheld with 11 buttons (on a PCF8574 and
// GPIOs) and a 128x32 OLED held upright, joining the band as a core v2 worker.
// The input button (the bridge: Guide) opens its own menu, in tabs: Devices
// (which device the buttons are for), Assistant (the conversation), Games, Settings.
//   I2C: SDA GPIO5, SCL GPIO6. PCF8574 0x20, SSD1306 0x3C.
// Button presses go to the hub as remote.button events; caps let agents show
// text, ask a question, wait for a press, read state and name the buttons.
// Setup is over USB serial (scripts/remote-enroll.py): Wi-Fi and enrollment.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <Wire.h>
#include <algorithm>
#include <deque>
#include <map>
#include <esp_random.h>
#include <esp_wifi.h>
#include <time.h>
#include "bt.h"
#include "buttons.h"
#include "chat.h"
#include "dev.h"
#include "lan.h"
#include "osk.h"
#include "ota.h"
#ifdef ROOK_GAMEPAD
#include <Preferences.h>
#include <Update.h>
#include <WebServer.h>
#include "pad.h"
#endif
#include "rc_sig.h"
#include "swarm.h"
#include "g2048.h"
#include "towers.h"
#include "rc_util.h"
#include "rc_worker.h"
#include "remote_manifest.h"
#include "settings.h"
#include "tetris.h"
#include "ui.h"

#define FW_VERSION "0.11.2"
#ifdef ROOK_BRIDGE
static const int PIN_SDA = 6, PIN_SCL = 7;      // XIAO ESP32-C3: D4, D5
#else
static const int PIN_SDA = 5, PIN_SCL = 6;      // XIAO ESP32-S3: D4, D5
#endif
#if CONFIG_FREERTOS_UNICORE
static const int APP_CORE = 0;
#else
static const int APP_CORE = 1;
#endif
static const uint8_t PCF_ADDR = 0x20, OLED_ADDR = 0x3C;

static rc::Worker worker;
static bool worker_on = false;
static WiFiMulti wifi;
static SemaphoreHandle_t ev_mu;
static std::deque<ButtonEvent> recent;      // last presses, for state and wait
static volatile int ask_answer = -1;        // set by the button handler while asking
static std::string last_note;
static volatile bool wifi_hold = false;     // the scan command has the radio: no joining meanwhile
// What the buttons do now: the TV's (events to the hub), the menu's (the input
// button opens it: pick the device the events are for, or the game), the game's.
// (The conversation is the menu's Assistant tab.)
enum Mode { REMOTE, MENU, GAME, TYPING };
// Left alone, the remote goes dark (DIM: the screen off) and then to sleep
// (ASLEEP: Wi-Fi off and the processor slowed too; Bluetooth stays, so a key
// of the paired remote still arrives). Any key wakes it, and still counts:
// what it sends waits the few seconds Wi-Fi takes to come back.
enum Power { AWAKE, DIM, ASLEEP };
static volatile Power power_state = AWAKE;
static volatile uint32_t last_active = 0;
static volatile bool wifi_sleep = false;    // the Wi-Fi task keeps the radio off while this is set
static uint32_t screen_off_s = 30, sleep_s = 300, cpu_full = 160;   // seconds left alone before each (0: never)
static volatile bool chat_show = false;     // a cap put something in the conversation: show it
static uint32_t thinking_until = 0;

static void active() { last_active = millis(); }
// The menu on screen: the main one (in tabs), the Wi-Fi networks heard, the
// saved ones to forget, or everything a device scan found.
enum MenuKind { MENU_MAIN, MENU_WIFI, MENU_FORGET, MENU_DEVS };
// The main menu's tabs, left to right (left and right step through them, round).
enum Tab { TAB_DEVICES, TAB_ASSIST, TAB_GAMES, TAB_SETTINGS, TAB_COUNT };
static const char* const TAB_NAMES[TAB_COUNT] = {"Devices", "Assistant", "Games", "Settings"};
static int menu_tab = TAB_DEVICES;          // the tab open
static int last_tab = -1;                   // the tab the person last went to (settings "tab"); -1: not read yet
static const uint32_t CHAT_IDLE_MS = 120000;   // the Assistant tab stays up longer than the rest
// A device on this network the buttons drive directly (dev.h), in place of a
// hub target: its id, and its name for the screen and the events.
static std::string local_id, local_name;
// Talking to the home agent through the paired remote's microphone: the audio
// leaves as remote.voice.part events while it is spoken, then remote.voice.
static uint32_t voice_id = 0, voice_seq = 0, voice_bytes = 0, voice_started = 0;
static bool voice_was_open = false, voice_hold = false;
static bool devs_pending = false;           // the menu waits for a device scan
static uint32_t devs_scans = 0;
static MenuKind menu_kind = MENU_MAIN;
static std::vector<std::string> menu_labels, menu_values;
static std::string typing_ssid;             // the network whose password is being typed
static uint32_t typing_until = 0;
// A scan asked of the Wi-Fi task (it owns the radio): 0 none, 1 wanted, 2 results in.
struct WifiHeard { std::string ssid; int rssi; bool open; };
static std::vector<WifiHeard> wifi_heard;
static SemaphoreHandle_t wifi_mu;
static volatile int wifi_scan_state = 0;
static volatile bool wifi_kick = false;     // drop the network in use and join again (a new one was chosen)
static bool wifi_pending = false;           // the menu waits for the scan
static volatile Mode mode = REMOTE;
static uint32_t menu_until = 0;             // the menu closes itself when left alone
static int game_kind = 0;                   // which of GAMES has the screen
// The games. flat: played with the screen as it sits (the others turn it a
// quarter turn, and the keys with it). draw null: ui draws it (Tetris).
struct GameDef {
    const char* label;
    bool flat;
    void (*start)(uint32_t);
    bool (*tick)(uint32_t, uint16_t);
    bool (*quit)();
    uint32_t (*score)();
    void (*draw)(Adafruit_GFX&);
};
static const GameDef GAMES[] = {
    {"Tetris", false, tetris::start, tetris::tick, tetris::quit, tetris::score, nullptr},
    {"Swarm", false, swarm::start, swarm::tick, swarm::quit, swarm::score, swarm::draw},
    {"2048", true, g2048::start, g2048::tick, g2048::quit, g2048::score, g2048::draw},
    {"Towers", true, towers::start, towers::tick, towers::quit, towers::score, towers::draw},
};
static bool swallow = false;                // drop the release of the press that left the game
static const uint32_t MENU_IDLE_MS = 20000;
static bool chat_up() { return mode == MENU && menu_kind == MENU_MAIN && menu_tab == TAB_ASSIST; }
static const uint32_t HOLD_MS = 600;        // a key down this long is held (the paired remote only says when it goes down)
// A paired remote's key going down that means one thing tapped and another
// held: the loop watches it (bt::held) until it is let go or HOLD_MS passes.
static std::string guide_key, ok_key;       // its raw name
static uint32_t guide_at = 0, ok_at = 0;    // when it went down (0: not watching)
static int ok_index = -1;                   // the menu row OK went down on
static uint32_t seq_all = 0;                // one count over the handheld's buttons and a paired remote's keys
#ifdef ROOK_BRIDGE
static const char* const BT_PAIR_TEXT = "Pair a remote:\n\nhold Back + Home on it\nuntil its light blinks";
#else
static const char* const BT_PAIR_TEXT = "BT\npair:\n\nput\nthe\nother\nremote\nin\npair\nmode";
#endif

#ifdef ROOK_GAMEPAD
// The gamepad build has no USB serial (its one port is the host for the pad):
// what would be said and typed there goes over a small web server on the
// device instead.  GET /log: what was logged.  POST /cmd: a serial command (a
// JSON line), answered as serial would.  POST /ota (a file upload): a build,
// for when the hub is not reached yet.  For the home network only: it asks
// for no password, as the serial port asks for none.
static WebServer devweb(80);
static std::string devweb_text, devweb_reply;
static bool devweb_capture = false;
static SemaphoreHandle_t devweb_mu;

static void devweb_log(const char* line) {
    if (!devweb_mu) return;
    xSemaphoreTake(devweb_mu, portMAX_DELAY);
    char t[16];
    snprintf(t, sizeof t, "%.1f ", millis() / 1000.0);
    devweb_text += t;
    devweb_text += line;
    devweb_text += '\n';
    if (devweb_text.size() > 12000) devweb_text.erase(0, devweb_text.size() - 10000);
    xSemaphoreGive(devweb_mu);
}
#endif

static void logf(const char* fmt, ...) {
    char b[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    Serial.printf("# %s\n", b);
#ifdef ROOK_GAMEPAD
    devweb_log(b);
#endif
}

// ---- buttons ----------------------------------------------------------------

static bool is_name(const std::string& n, std::initializer_list<const char*> set) {
    for (const char* s : set) if (n == s) return true;
    return false;
}

static void logf(const char* fmt, ...);

static void set_power(Power p) {
    Power was = power_state;
    if (p == was) return;
    power_state = p;
    if ((p == AWAKE) != (was == AWAKE)) ui::sleep(p != AWAKE);
    if ((p == ASLEEP) != (was == ASLEEP)) {
        wifi_sleep = p == ASLEEP;
        setCpuFrequencyMhz(p == ASLEEP ? 80 : cpu_full);
        logf(p == ASLEEP ? "power: asleep (Wi-Fi off)" : "power: awake");
    }
}

static void load_sleep() {
    std::string a = settings::kv("scr_off"), b = settings::kv("sleep");
    screen_off_s = a.empty() ? 30 : (uint32_t)atoi(a.c_str());
    sleep_s = b.empty() ? 300 : (uint32_t)atoi(b.c_str());
}

static void show_tab(int tab);

// The conversation with the house agent takes the screen (chat.h): the menu, open at its Assistant tab.
static void open_chat() { show_tab(TAB_ASSIST); }

static void close_chat() {
    if (!chat_up()) return;
    ui::menu_end();
    mode = REMOTE;
}

static void show_menu(MenuKind kind, const char* title, const std::vector<std::string>& labels,
                      const std::vector<std::string>& values, int selected) {
    menu_kind = kind;
    menu_labels = labels;
    menu_values = values;
    ui::menu_begin(title, labels, selected);
    menu_until = millis() + MENU_IDLE_MS;
    mode = MENU;
}

static void set_local(const std::string& id) {
    lan::Device d;
    if (id.empty() || !lan::find(id, d)) {
        local_id.clear();
        local_name.clear();
    } else {
        local_id = d.id;
        local_name = d.name;
    }
    settings::set_kv("local", local_id);
}

// The game called label takes the screen. False: there is none by that name.
static bool start_game(const std::string& label) {
    for (size_t i = 0; i < sizeof GAMES / sizeof GAMES[0]; i++) {
        if (label != GAMES[i].label) continue;
        game_kind = (int)i;
        GAMES[i].start(millis());
        if (GAMES[i].draw) ui::canvas(GAMES[i].draw, GAMES[i].label);
        else ui::game(true);
        mode = GAME;
        return true;
    }
    return false;
}

// A tab's rows. Devices: the hub's targets (values t:<index>), then the
// devices found on this network that the remote can drive itself (lan:<id>).
// Games: the games (the handheld's narrow screen has Tetris only). Settings:
// Wi-Fi, a device scan, Update, Bluetooth.
static void tab_rows(int tab, std::vector<std::string>& labels, std::vector<std::string>& values, int& selected) {
    selected = 0;
    if (tab == TAB_DEVICES) {
        auto ts = settings::targets();
        for (size_t i = 0; i < ts.size(); i++) {
            labels.push_back(ts[i]);
            values.push_back("t:" + std::to_string(i));
        }
        selected = settings::target();
        for (auto& d : lan::devices()) {
            if (d.driver.empty()) continue;
            if (d.id == local_id) selected = (int)labels.size();
            labels.push_back(d.name);
            values.push_back("lan:" + d.id);
        }
        if (labels.empty()) {                // nothing yet: look for some
            labels.push_back("Find devs");
            values.push_back("Find devs");
        }
    } else if (tab == TAB_GAMES) {
        for (auto& g : GAMES) {
#ifndef ROOK_BRIDGE
            if (strcmp(g.label, "Tetris")) continue;      // the others are drawn for the bridge's wide screen
#endif
            labels.push_back(g.label);
            values.push_back(g.label);
        }
    } else if (tab == TAB_SETTINGS) {
        for (const char* s : {"Wi-Fi", "Find devs", "Update", "Pair BT"}) {
            labels.push_back(s);
            values.push_back(s);
        }
        if (bt::saved()) {
            labels.push_back("Forget BT");
            values.push_back("Forget BT");
        }
    }
}

// The main menu, open at tab. The Assistant tab is the conversation itself.
static void show_tab(int tab) {
    std::vector<std::string> labels, values;
    std::vector<std::string> names(TAB_NAMES, TAB_NAMES + TAB_COUNT);
    int selected = 0;
    menu_tab = tab;
    menu_kind = MENU_MAIN;
    if (tab != TAB_ASSIST) tab_rows(tab, labels, values, selected);
    menu_labels = labels;
    menu_values = values;
    ui::menu_tabs(names, tab, labels, selected, tab == TAB_ASSIST ? chat::draw : nullptr);
    menu_until = millis() + (tab == TAB_ASSIST ? CHAT_IDLE_MS : MENU_IDLE_MS);
    mode = MENU;
}

static int saved_tab() {
    if (last_tab < 0) {
        std::string t = settings::kv("tab");
        last_tab = t.empty() ? TAB_DEVICES : std::max(0, std::min((int)TAB_COUNT - 1, atoi(t.c_str())));
    }
    return last_tab;
}

// The person went to tab: the menu opens there next time.
static void remember_tab(int tab) {
    if (saved_tab() == tab) return;
    last_tab = tab;
    settings::set_kv("tab", std::to_string(tab));
}

// The menu key: the menu, at the tab last used.
static void open_menu() { show_tab(saved_tab()); }

// The Assistant key, or OK on the Assistant tab: the paired remote's
// microphone opens (tap to start, tap again to stop).
static void assistant_key() {
    if (!bt::voice_ready()) ui::flash("no\nmic", 1500, mode == MENU);
    else if (!bt::voice_open()) bt::voice_start();
    else if (millis() - voice_started > 700) bt::voice_stop();
}

// Everything the last scan found: what the remote can drive first, the rest
// with what they answered as.
static void open_devs_menu() {
    std::vector<std::string> labels, values;
    for (auto& d : lan::devices()) {
        labels.push_back(d.driver.empty() ? d.name + " (" + d.kinds[0] + ")" : d.name);
        values.push_back((d.driver.empty() ? "x:" : "lan:") + d.id);
    }
    labels.push_back("Rescan");
    values.push_back("");
    show_menu(MENU_DEVS, "DEVICES", labels, values, 0);
}

static void start_dev_scan() {
    std::string err;
    if (WiFi.status() != WL_CONNECTED) {
        ui::flash("no\nWi-Fi");
        return;
    }
    devs_scans = lan::scans();
    if (!dev::run("scan", "", "", 0, err)) {
        ui::flash("busy");
        return;
    }
    devs_pending = true;
    mode = REMOTE;
    ui::card("DEVICES", "scanning", -2);
}

// The buttons are this device's from now on; an LG TV not yet paired asks its
// person to accept the remote first.
static void pick_device(const std::string& id) {
    lan::Device d;
    if (!lan::find(id, d)) return;
    if (d.driver.empty()) {
        ui::flash("no\ndriver");
        return;
    }
    lan::keep(d.id);
    set_local(d.id);
    std::string err;
    if (!dev::paired(d)) dev::run("pair", d.id, "", 0, err);
    else ui::flash(d.name);
}

// The networks heard (strongest first; * marks a saved one), then the saved ones to forget.
static void open_wifi_menu() {
    std::vector<std::string> labels, values;
    auto saved = settings::wifi();
    xSemaphoreTake(wifi_mu, portMAX_DELAY);
    for (auto& h : wifi_heard) {
        bool known = false;
        for (auto& n : saved) known = known || settings::wifi_same(n.first, h.ssid);
        labels.push_back((known ? "*" : "") + h.ssid);
        values.push_back(h.ssid);
    }
    xSemaphoreGive(wifi_mu);
    labels.push_back("Rescan");
    values.push_back("");
    if (!saved.empty()) {
        labels.push_back("Forget...");
        values.push_back("");
    }
    show_menu(MENU_WIFI, "WI-FI", labels, values, 0);
}

static void start_wifi_scan() {
    wifi_scan_state = 1;
    wifi_pending = true;
    mode = REMOTE;
    ui::card("WI-FI", "scanning", -2);
}

static void save_wifi(const std::string& ssid, const std::string& pass) {
    settings::wifi_add(ssid, pass);         // at the top: joined first from now on
    wifi_kick = true;
    ui::flash("joining\n" + ssid);
}

static bool wifi_saved(const std::string& ssid) {
    for (auto& n : settings::wifi())
        if (settings::wifi_same(n.first, ssid)) return true;
    return false;
}

// OK held on a saved network in the Wi-Fi menu: it goes to the top of the list (joined first).
static void wifi_prefer_row(int i) {
    if (i < 0 || i >= (int)menu_values.size() || !settings::wifi_prefer(menu_values[i])) return;
    menu_until = millis() + MENU_IDLE_MS;
    ui::flash("Preferred", 900, true);
}

// The devices the menu offers the buttons to: the hub's targets, then those on
// this network the remote drives itself. The profile key steps to the next.
static void next_device() {
    auto ts = settings::targets();
    std::vector<std::string> lans, names;
    for (auto& d : lan::devices()) {
        if (d.driver.empty()) continue;
        lans.push_back(d.id);
        names.push_back(d.name);
    }
    int n = (int)(ts.size() + lans.size());
    if (!n) {
        ui::flash("no devices", 1000, false);
        return;
    }
    int cur = settings::target();
    if (!local_id.empty())
        for (size_t i = 0; i < lans.size(); i++)
            if (lans[i] == local_id) cur = (int)(ts.size() + i);
    int next = ((cur < 0 ? -1 : cur) + 1) % n;
    close_chat();
    if (next < (int)ts.size()) {
        settings::set_target(next);
        set_local("");
        ui::flash(ts[next], 1000, false);
        return;
    }
    lan::Device d;
    if (!lan::find(lans[next - ts.size()], d)) return;
    lan::keep(d.id);
    set_local(d.id);
    std::string err;
    if (!dev::paired(d)) dev::run("pair", d.id, "", 0, err);
    else ui::flash(d.name, 1000, false);
}

static bool is_profile_key(const ButtonEvent& e) {
    std::string raw = e.lines.rfind("bt:", 0) == 0 ? e.lines.substr(3) : e.name;
    for (const char* n : {raw.c_str(), e.name.c_str()})
        if (!strcasecmp(n, "bt_c19c") || !strcasecmp(n, "bt_cc19c")) return true;
    return false;
}

static void menu_pick(int i) {
    std::string label = i >= 0 && i < (int)menu_labels.size() ? menu_labels[i] : "";
    std::string value = i >= 0 && i < (int)menu_values.size() ? menu_values[i] : "";
    MenuKind kind = menu_kind;
    ui::menu_end();
    mode = REMOTE;
    if (kind == MENU_WIFI) {
        if (label == "Rescan") {
            start_wifi_scan();
        } else if (label == "Forget...") {
            std::vector<std::string> names;
            for (auto& n : settings::wifi()) names.push_back(n.first);
            show_menu(MENU_FORGET, "FORGET", names, names, 0);
        } else if (!value.empty()) {
            bool open = false;
            xSemaphoreTake(wifi_mu, portMAX_DELAY);
            for (auto& h : wifi_heard) open = open || (h.ssid == value && h.open);
            xSemaphoreGive(wifi_mu);
            if (open) {
                save_wifi(value, "");
            } else {
                typing_ssid = value;
                typing_until = millis() + 120000;
                osk::begin(value, "");
                ui::canvas(osk::draw);
                mode = TYPING;
            }
        }
        return;
    }
    if (kind == MENU_FORGET) {
        if (!value.empty()) {
            settings::wifi_remove(value);
            ui::flash("forgot\n" + value);
        }
        return;
    }
    if (value.rfind("lan:", 0) == 0) {
        pick_device(value.substr(4));
        return;
    }
    if (kind == MENU_DEVS) {
        if (label == "Rescan") start_dev_scan();
        else ui::flash("no\ndriver");
        return;
    }
    auto ts = settings::targets();
    if (value.rfind("t:", 0) == 0) {
        int t = atoi(value.c_str() + 2);
        if (t < 0 || t >= (int)ts.size()) return;
        settings::set_target(t);
        set_local("");
        ui::flash(ts[t]);
    } else if (label == "Find devs") {
        start_dev_scan();
    } else if (label == "Update") {
        // Ask the hub whether it holds a newer build for this board. Its answer
        // comes over the band: remote.update with the build's address and hash,
        // or a word that this one is the newest.
        if (!worker_on || !worker.hub_ok()) {
            ui::flash("no hub");
        } else {
            JsonDocument p;
            p["worker"] = settings::name();
            p["version"] = FW_VERSION;
#ifdef ROOK_BRIDGE
            p["board"] = "bridge-c3";
#else
            p["board"] = "s3";
#endif
            worker.emit("remote.update.check", p.as<JsonObjectConst>());
            mode = REMOTE;
            ui::card("UPDATE", "checking", -2);
        }
    } else if (start_game(label)) {
    } else if (label == "Wi-Fi") {
        start_wifi_scan();
    } else if (label == "Pair BT") {
        bt::pair();
        ui::text(BT_PAIR_TEXT, 35);
    } else if (label == "Forget BT") {
        bt::forget();
        ui::flash("BT\ngone");
    }
}

// A press: one of the handheld's buttons, or (index -1, lines "bt") a key of
// the Bluetooth remote paired to it. Both do the same things.
static void on_button(const ButtonEvent& ev) {
    ButtonEvent e = ev;
    e.seq = ++seq_all;
    active();
    if (power_state != AWAKE) set_power(AWAKE);      // the key that wakes it still counts
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    recent.push_back(e);
    while (recent.size() > 16) recent.pop_front();
    xSemaphoreGive(ev_mu);
    logf("button %s (%d, %s) %s %ums", e.name.c_str(), e.index, e.lines.c_str(), e.action, (unsigned)e.held_ms);
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
    // The Assistant key: talk to the home agent. Tap to start and tap to stop,
    // or hold it down while talking. (A remote may also ask for its microphone itself.)
    if (is_name(e.name, {"assistant", "search", "voice"}) && mode != GAME && mode != TYPING) {
        if (!strcmp(e.action, "press")) assistant_key();
        return;
    }
    // While it listens, any key says "that's it" (some remotes don't report the Assistant key again).
    if (bt::voice_open() && e.index < 0) {
        if (!strcmp(e.action, "press") && millis() - voice_started > 400) bt::voice_stop();
        return;
    }
    if (swallow || mode == GAME) return;       // the game reads the held buttons itself (loop)
    if (mode == TYPING) {                      // a Wi-Fi password on the on-screen keyboard
        if (strcmp(e.action, "press")) return;
        typing_until = millis() + 120000;
        int done = osk::key(e.name);
        ui::redraw();
        if (done) {
            ui::canvas(nullptr);
            mode = REMOTE;
            if (done > 0) save_wifi(typing_ssid, osk::text());
            osk::begin("", "");              // don't keep the password around
        }
        return;
    }
    if (mode == MENU) {
        menu_until = millis() + (chat_up() ? CHAT_IDLE_MS : MENU_IDLE_MS);
        int sel = ui::menu_selected();
        if (e.name == "ok" && menu_kind == MENU_WIFI && sel < (int)menu_values.size() &&
            !menu_values[sel].empty() && wifi_saved(menu_values[sel])) {
            // A saved network: OK tapped joins it, held puts it first. The
            // handheld's buttons say "long" themselves; the paired remote's
            // only go down, so the loop times those.
            if (!strcmp(e.action, "long")) { wifi_prefer_row(sel); return; }
            if (!strcmp(e.action, "press") && e.lines.rfind("bt:", 0) == 0) {
                ok_key = e.lines.substr(3);
                ok_index = sel;
                ok_at = millis() | 1;
                return;
            }
        }
        if (strcmp(e.action, "press")) return;
        // The profile key steps to the next device from the menu too (the menu goes away).
        if (is_profile_key(e) && !buttons::learning()) {
            ui::menu_end();
            mode = REMOTE;
            next_device();
            return;
        }
        bool tabs = menu_kind == MENU_MAIN;
        if (tabs && (e.name == "left" || e.name == "right")) {
            int t = (menu_tab + (e.name == "left" ? TAB_COUNT - 1 : 1)) % TAB_COUNT;
            remember_tab(t);
            show_tab(t);
        } else if (chat_up() && (e.name == "up" || e.name == "down")) {
            // The conversation: up and down read back through it.
            chat::scroll(e.name == "up" ? -24 : 24);
            ui::redraw();
        } else if (chat_up() && e.name == "ok") {
            assistant_key();
        }
        else if (e.name == "up") ui::menu_move(-1);
        else if (e.name == "down") ui::menu_move(1);
        else if (e.name == "ok") menu_pick(sel);
#ifdef ROOK_BRIDGE
        else if (is_name(e.name, {"back", "home", "guide"})) { ui::menu_end(); mode = REMOTE; }
#else
        else if (is_name(e.name, {"back", "input", "home"})) { ui::menu_end(); mode = REMOTE; }
#endif
        return;
    }
    // A text too long for the screen (an agent's answer): up and down read it, back puts it away.
    if (!strcmp(e.action, "press") && is_name(e.name, {"up", "down", "back"}) && ui::text_scroll(0)) {
        if (e.name == "back") ui::text("", 0);
        else ui::text_scroll(e.name == "up" ? -4 : 4);
        return;
    }
    // The profile key: the buttons go to the next device the menu offers.
    if (is_profile_key(e) && !buttons::learning()) {
        if (!strcmp(e.action, "press")) next_device();
        return;
    }
#ifdef ROOK_BRIDGE
    // Guide tapped opens the menu; held, the menu at its Assistant tab (the conversation).
    // (Input and the remote's menu key go to the TV.)
    if (e.name == "guide" && !buttons::learning()) {
        if (!strcmp(e.action, "long")) { remember_tab(TAB_ASSIST); open_chat(); }
        else if (strcmp(e.action, "press")) return;
        else if (e.lines.rfind("bt:", 0) == 0) {
            guide_key = e.lines.substr(3);
            guide_at = millis() | 1;
        } else {
            open_menu();
        }
        return;
    }
#else
    if ((e.name == "input" || e.name == "bt_menu") && !buttons::learning()) {
        if (!strcmp(e.action, "press")) open_menu();
        return;
    }
#endif
    if (buttons::learning()) {
        char b[24];
        snprintf(b, sizeof b, "%s = %s", e.name.c_str(), e.lines.c_str());
        ui::flash(b);
    } else {
        ui::flash(e.name + (strcmp(e.action, "long") ? "" : " (hold)"));
    }
    // A device on this network picked in the menu: the remote presses its key itself.
    bool local = !local_id.empty();
    if (local && !strcmp(e.action, "press")) {
        std::string err;
        dev::run("key", local_id, e.name, 0, err);
    }
    if (worker_on) {
        JsonDocument p;
        p["button"] = e.name;
        p["action"] = e.action;
        p["index"] = e.index;
        p["held_ms"] = e.held_ms;
        if (local) {
            p["target"] = local_name;
            p["local"] = true;
        } else if (!settings::target_name().empty()) p["target"] = settings::target_name();
        if (e.index < 0) p["source"] = "bt";
        worker.emit("remote.button", p.as<JsonObjectConst>());
    }
}

// ---- caps -------------------------------------------------------------------

// The paired remote's keys by the names they are sent under (remote.bt names).
static std::map<std::string, std::string> bt_names;
static SemaphoreHandle_t bt_names_mu;

static void load_bt_names() {
    JsonDocument d;
    deserializeJson(d, settings::bt_names());
    xSemaphoreTake(bt_names_mu, portMAX_DELAY);
    bt_names.clear();
    for (JsonPairConst kv : d.as<JsonObjectConst>()) {
        std::string to = kv.value() | "";
        if (!to.empty()) bt_names[kv.key().c_str()] = to;
    }
    xSemaphoreGive(bt_names_mu);
}

static std::string bt_name_of(const std::string& raw) {
    xSemaphoreTake(bt_names_mu, portMAX_DELAY);
    auto it = bt_names.find(raw);
    std::string n = it == bt_names.end() ? raw : it->second;
    xSemaphoreGive(bt_names_mu);
    return n;
}

static void fill_bt(JsonObject o) {
    o["state"] = bt::state();
    o["voice"] = bt::voice_ready();
    if (bt::state() == "connected") {
        o["voice_info"] = bt::voice_info();
        o["services"] = bt::services();
        o["find"] = bt::find_ready();
        if (!bt::find_info().empty()) o["find_info"] = bt::find_info();
    }
    if (bt::saved()) {
        o["name"] = bt::name();
        o["address"] = bt::address();
    }
    if (!bt::last_report().empty()) o["last_report"] = bt::last_report();
    JsonObject names = o["names"].to<JsonObject>();
    xSemaphoreTake(bt_names_mu, portMAX_DELAY);
    for (auto& kv : bt_names) names[kv.first] = kv.second;
    xSemaphoreGive(bt_names_mu);
}

static void fill_state(JsonObject r) {
    r["name"] = settings::name();
    r["version"] = FW_VERSION;
    r["mode"] = mode == GAME ? "game" : chat_up() ? "chat" : mode == MENU ? "menu" : mode == TYPING ? "typing" : "remote";
    if (mode == MENU && menu_kind == MENU_MAIN) r["tab"] = TAB_NAMES[menu_tab];
    r["power"] = power_state == ASLEEP ? "asleep" : power_state == DIM ? "dim" : "awake";
    r["screen_off"] = screen_off_s;
    r["sleep"] = sleep_s;
    r["idle_s"] = (millis() - last_active) / 1000;
    fill_bt(r["bt"].to<JsonObject>());
    r["target"] = settings::target_name();
    JsonArray tg = r["targets"].to<JsonArray>();
    for (auto& t : settings::targets()) tg.add(t);
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
    if (!local_id.empty()) {
        r["local"]["id"] = local_id;
        r["local"]["name"] = local_name;
    }
    r["devices"] = (int)lan::devices().size();
    ota::fill(r["update"].to<JsonObject>());
    r["heap"] = (unsigned)ESP.getFreeHeap();
    r["hub_contact"] = worker.hub_ok();
    r["events_sent"] = worker.events_sent();
    r["events_queued"] = (unsigned)worker.events_queued();
    r["uptime_s"] = millis() / 1000;
}

static bool cap_state(JsonObjectConst, JsonObject r, std::string&) {
    fill_state(r);
    return true;
}

static bool cap_chat(JsonObjectConst a, JsonObject r, std::string&) {
    if (a["clear"] | false) chat::clear();
    std::string text = a["text"] | "";
    if (!text.empty()) {
        chat::add(a["who"] | "agent", text);
        chat_show = true;
        active();
    }
    if (!a["status"].isNull()) {
        chat::status(a["status"] | "");
        chat_show = true;
        active();
    }
    chat::fill(r["messages"].to<JsonArray>());
    return true;
}

// A key pressed from the band, as if on the paired remote: the loop takes it from here.
static std::deque<std::string> injected;
static std::string inject_held;             // held a moment, so the game sees it too
static uint32_t inject_until = 0;

static bool cap_press(JsonObjectConst a, JsonObject r, std::string& err) {
    std::string b = a["button"] | "";
    if (b.empty()) { err = "bad args: button is needed"; return false; }
    xSemaphoreTake(ev_mu, portMAX_DELAY);
    if (injected.size() < 8) injected.push_back(b);
    xSemaphoreGive(ev_mu);
    r["queued"] = b;
    return true;
}

static bool cap_screen(JsonObjectConst, JsonObject r, std::string& err) {
    std::string px;
    int w = 0, h = 0, rot = 0;
    bool dark = false;
    if (!ui::snapshot(px, w, h, rot, dark)) { err = "no screen"; return false; }
    r["width"] = w;
    r["height"] = h;
    r["rotation"] = rot;
    r["dark"] = dark;
    r["says"] = ui::shown();
    r["data"] = rc::b64((const uint8_t*)px.data(), px.size());
    return true;
}

static bool cap_display(JsonObjectConst a, JsonObject r, std::string&) {
    active();
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

static bool cap_bt(JsonObjectConst a, JsonObject r, std::string& err) {
    std::string action = a["action"] | "status";
    if (action == "pair") {
        bt::pair();
        ui::text(BT_PAIR_TEXT, 35);
    } else if (action == "forget") {
        bt::forget();
    } else if (action == "find" || action == "quiet") {
        if (!bt::find_ready()) { err = "the paired remote has no find-me service, or is not connected"; return false; }
        bt::find(action == "find" ? (int)(a["level"] | 3) : 0);
        delay(400);                  // long enough for its answer to be in the reply
    }
    JsonObjectConst names = a["names"].as<JsonObjectConst>();
    if (!names.isNull()) {
        std::string json;
        serializeJson(names, json);
        settings::set_bt_names(json);
        load_bt_names();
    }
    fill_bt(r);
    return true;
}

static bool cap_wifi(JsonObjectConst a, JsonObject r, std::string& err) {
    std::string action = a["action"] | "list";
    std::string ssid = a["ssid"] | "";
    if (action == "add" || action == "forget") {
        if (ssid.empty()) { err = "bad args: ssid is needed"; return false; }
        if (action == "add") settings::wifi_add(ssid, a["password"] | "");
        else settings::wifi_remove(ssid);
    }
    std::string prefer = a["prefer"] | "";
    if (!prefer.empty() && !settings::wifi_prefer(prefer)) {
        err = "not saved: prefer names one of the saved networks (" + prefer + ")";
        return false;
    }
    JsonArray saved = r["saved"].to<JsonArray>();
    for (auto& n : settings::wifi()) saved.add(n.first);
    r["in_use"] = WiFi.SSID();
    r["rssi"] = WiFi.RSSI();
    return true;
}

static void fill_devices(JsonObject r) {
    JsonArray a = r["devices"].to<JsonArray>();
    for (auto& d : lan::devices()) dev::fill(d, a.add<JsonObject>());
    r["network"] = WiFi.SSID();
    r["scans"] = lan::scans();
    if (!local_id.empty()) r["selected"] = local_id;
}

static bool cap_scan(JsonObjectConst, JsonObject r, std::string& err) {
    if (!dev::run("scan", "", "", 45000, err)) return false;
    fill_devices(r);
    return true;
}

static bool cap_devices(JsonObjectConst, JsonObject r, std::string&) {
    fill_devices(r);
    return true;
}

static bool cap_device(JsonObjectConst a, JsonObject r, std::string& err) {
    std::string device = a["device"] | "", action = a["action"] | "key";
    lan::Device d;
    if (!lan::find(device, d)) {
        err = "no device called " + device + ": remote.scan finds what is on this network";
        return false;
    }
    if (action == "select") {
        if (d.driver.empty()) { err = "the remote has no driver for " + d.name; return false; }
        lan::keep(d.id);
        set_local(d.id);
    } else if (action == "release") {
        if (local_id == d.id) set_local("");
    } else {
        std::string arg = action == "launch" ? (a["app"] | "") : (a["key"] | "");
        if ((action == "key" || action == "launch") && arg.empty()) {
            err = action == "key" ? "bad args: key is needed" : "bad args: app is needed";
            return false;
        }
        std::string out;
        if (!dev::run(action, d.id, arg, action == "pair" ? 70000 : 20000, err, &out)) return false;
        if (!out.empty()) r["did"] = out;
        if (action == "forget" && local_id == d.id) set_local("");
    }
    lan::Device now;
    if (lan::find(d.id, now)) dev::fill(now, r["device"].to<JsonObject>());
    if (!local_id.empty()) r["selected"] = local_id;
    return true;
}

static bool cap_update(JsonObjectConst a, JsonObject r, std::string& err) {
    std::string url = a["url"] | "";
    if (!url.empty()) {
        if (!ota::start(url, a["sha256"] | "", err)) return false;
        r["started"] = true;
    }
    r["version"] = FW_VERSION;
    ota::fill(r);
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
    JsonArrayConst tg = a["targets"].as<JsonArrayConst>();
    if (!tg.isNull() && tg.size()) {
        std::vector<std::string> ts;
        for (JsonVariantConst v : tg) ts.push_back(v | "");
        settings::set_targets(ts);
    }
    if (!a["screen_off"].isNull()) settings::set_kv("scr_off", std::to_string((int)(a["screen_off"] | 30)));
    if (!a["sleep"].isNull()) settings::set_kv("sleep", std::to_string((int)(a["sleep"] | 300)));
    if (!a["tz"].isNull()) {
        settings::set_kv("tz", a["tz"] | "");
        std::string tz = settings::kv("tz");
        setenv("TZ", tz.empty() ? "UTC0" : tz.c_str(), 1);
        tzset();
    }
    r["tz"] = settings::kv("tz");
    load_sleep();
    r["screen_off"] = screen_off_s;
    r["sleep"] = sleep_s;
    JsonArray bs = r["buttons"].to<JsonArray>();
    for (int i = 0; i < buttons::count(); i++) bs.add(buttons::name(i));
    JsonArray to = r["targets"].to<JsonArray>();
    for (auto& t : settings::targets()) to.add(t);
    r["target"] = settings::target_name();
    return true;
}

// ---- serial setup (scripts/remote-enroll.py) ---------------------------------

static void reply(JsonDocument& d) {
#ifdef ROOK_GAMEPAD
    if (devweb_capture) {
        std::string one;
        serializeJson(d, one);
        devweb_reply += one + "\n";
    }
#endif
    Serial.print("@ ");
    serializeJson(d, Serial);
    Serial.println();
}

static void handle_line(const std::string& line) {
    JsonDocument in, out;
    if (line.empty()) return;
    active();
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
        // A join in flight owns the radio (its own scan, up to 8 s): wait it out,
        // then scan alone at full power, hidden networks too, 300 ms a channel.
        bool joined = WiFi.status() == WL_CONNECTED;
        wifi_hold = true;
        if (!joined) {
            delay(9000);
            WiFi.disconnect();
            delay(200);
            WiFi.setTxPower(WIFI_POWER_19_5dBm);
        }
        int n = WiFi.scanNetworks(false, true, false, 300);
        out["count"] = n;                 // below 0: the scan itself failed
        out["tx_power"] = (int)WiFi.getTxPower();
        out["was_joined"] = joined;
        JsonArray a = out["heard"].to<JsonArray>();
        for (int i = 0; i < n; i++) {
            JsonObject o = a.add<JsonObject>();
            o["ssid"] = WiFi.SSID(i);
            o["rssi"] = WiFi.RSSI(i);
            o["ch"] = WiFi.channel(i);
        }
        WiFi.scanDelete();
        wifi_hold = false;
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
    } else if (cmd == "targets") {
        std::vector<std::string> ts;
        for (JsonVariantConst v : in["targets"].as<JsonArrayConst>()) ts.push_back(v | "");
        if (!ts.empty()) settings::set_targets(ts);
        JsonArray to = out["targets"].to<JsonArray>();
        for (auto& t : settings::targets()) to.add(t);
    } else if (cmd == "rotate") {
        settings::set_rotation(in["turns"] | 3);
        ui::set_rotation(settings::rotation());
        out["turns"] = settings::rotation();
    } else if (cmd == "bt" || cmd == "bt.pair" || cmd == "bt.forget") {
        if (cmd == "bt.pair") {
            bt::pair();
            ui::text(BT_PAIR_TEXT, 35);
        } else if (cmd == "bt.forget") {
            bt::forget();
        }
        fill_bt(out["bt"].to<JsonObject>());
    } else if (cmd == "dev.scan") {
        std::string err;
        out["queued"] = dev::run("scan", "", "", 0, err);
    } else if (cmd == "devices") {
        fill_devices(out.to<JsonObject>());
        out["ok"] = true;
    } else if (cmd == "dev") {
        std::string err, did;
        std::string action = in["action"] | "key";
        if (!dev::run(action, in["device"] | "", in["arg"] | "", action == "pair" ? 70000 : 20000, err, &did)) {
            out["ok"] = false;
            out["error"] = err;
        }
        if (!did.empty()) out["did"] = did;
    } else if (cmd == "update") {
        std::string err;
        if (!ota::start(in["url"] | "", in["sha256"] | "", err)) {
            out["ok"] = false;
            out["error"] = err;
        }
    } else if (cmd == "update.confirm") {
        ota::confirm();
        ota::fill(out["update"].to<JsonObject>());
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
        out["error"] = "commands: status key wifi wifi.clear scan name enroll unenroll show names identify layout.set layout.reset targets rotate bt bt.pair bt.forget dev.scan devices dev update update.confirm reboot factory";
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

#ifdef ROOK_GAMEPAD
static void devweb_begin() {
    devweb.on("/", [] { devweb.send(200, "text/plain", "rook remote (gamepad) " FW_VERSION ": GET /log, POST /cmd, POST /ota\n"); });
    devweb.on("/log", [] {
        xSemaphoreTake(devweb_mu, portMAX_DELAY);
        std::string s = devweb_text;
        xSemaphoreGive(devweb_mu);
        devweb.send(200, "text/plain", s.c_str());
    });
    devweb.on("/cmd", HTTP_POST, [] {
        devweb_reply.clear();
        devweb_capture = true;
        handle_line(std::string(devweb.arg("plain").c_str()));
        devweb_capture = false;
        devweb.send(200, "application/json", devweb_reply.c_str());
    });
    devweb.on("/ota", HTTP_POST, [] {
        bool ok = !Update.hasError();
        devweb.send(ok ? 200 : 500, "text/plain", ok ? "ok, restarting\n" : "failed\n");
        if (ok) { delay(400); ESP.restart(); }
    }, [] {
        HTTPUpload& up = devweb.upload();
        if (up.status == UPLOAD_FILE_START) Update.begin(UPDATE_SIZE_UNKNOWN);
        else if (up.status == UPLOAD_FILE_WRITE) Update.write(up.buf, up.currentSize);
        else if (up.status == UPLOAD_FILE_END) Update.end(true);
    });
    devweb.begin();
}

// The first time: the Wi-Fi networks the Rook dongle firmware kept on this device (NVS rook/wifi_nets).
static void seed_wifi() {
    if (!settings::wifi().empty()) return;
    Preferences p;
    p.begin("rook", true);
    String json = p.getString("wifi_nets", "");
    p.end();
    JsonDocument d;
    if (!json.length() || deserializeJson(d, json)) return;
    std::vector<std::pair<int, std::pair<std::string, std::string>>> nets;
    for (JsonObject o : d.as<JsonArray>()) {
        std::string ssid = o["ssid"] | "", pass = o["pass"] | "";
        if (!ssid.empty()) nets.push_back({o["priority"] | 100, {ssid, pass}});
    }
    std::stable_sort(nets.begin(), nets.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
    for (auto& n : nets) settings::wifi_add(n.second.first, n.second.second);
}
#endif

// ---- tasks ------------------------------------------------------------------

static void rook_task(void*) {
    for (;;) {
        if (WiFi.status() == WL_CONNECTED) worker.loop();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// Joining Wi-Fi blocks for seconds; keep it off the button/serial loop.
// One round: drop whatever the radio was trying (a scan fails while it is still
// connecting, which is how WiFiMulti went deaf away from the last network),
// scan once, then try each saved network heard, in the saved list's order (the
// person's order: the first is preferred), at its strongest access point,
// moving on when one won't have us. All failed: the next round does it again.
// - A WPA2/WPA3 network is joined as WPA2 (no PMF): the WPA3 handshake to the
//   house mesh never completes on this radio, and says nothing while it fails.
// - Transmit power: full power fails to authenticate right next to an AP
//   (reasons 2/202 at -25 dBm) and its bursts upset the PCF8574 lines, so a
//   strong network is joined at 8.5 dBm and a weaker one at full power; every
//   other round tries the other power.
struct WifiCandidate {
    std::string ssid, pass;
    int ch, rssi;
    uint8_t bssid[6];
};

static WifiCandidate wifi_quick;            // the access point that last let us in
static bool wifi_have_quick = false, wifi_quick_low = false;
static bool wifi_woke = false;              // the radio was off (asleep): a quick rejoin may do

static bool wifi_connect(const WifiCandidate& c, bool low, int tenths) {
    WiFi.begin(c.ssid.c_str(), c.pass.c_str(), c.ch, c.bssid, false);
    wifi_config_t conf;
    if (esp_wifi_get_config(WIFI_IF_STA, &conf) == ESP_OK) {
        conf.sta.pmf_cfg.capable = false;
        conf.sta.pmf_cfg.required = false;
        esp_wifi_set_config(WIFI_IF_STA, &conf);
    }
    WiFi.setTxPower(low ? WIFI_POWER_8_5dBm : WIFI_POWER_19_5dBm);
    esp_wifi_connect();
    for (int t = 0; t < tenths && WiFi.status() != WL_CONNECTED; t++) vTaskDelay(pdMS_TO_TICKS(100));
    return WiFi.status() == WL_CONNECTED;
}

// Something else wants the radio: leave the round.
static bool wifi_interrupted() { return wifi_kick || wifi_sleep || wifi_hold || wifi_scan_state == 1; }

static bool wifi_join(unsigned round) {
    if (WiFi.getMode() == WIFI_OFF) WiFi.mode(WIFI_STA);      // it was asleep
    WiFi.disconnect();
    vTaskDelay(pdMS_TO_TICKS(200));
    auto saved = settings::wifi();
    // Waking up where it fell asleep: straight back to the same access point,
    // no scan, when that network is still the first on the list.
    if (wifi_woke && wifi_have_quick && !saved.empty() && settings::wifi_same(saved[0].first, wifi_quick.ssid)) {
        wifi_woke = false;
        wifi_quick.pass = saved[0].second;
        logf("wifi: back to %s (ch %d)", wifi_quick.ssid.c_str(), wifi_quick.ch);
        if (wifi_connect(wifi_quick, wifi_quick_low, 50)) return true;
        WiFi.disconnect();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    wifi_woke = false;
    int n = WiFi.scanNetworks(false, true, false, 300);
    if (n < 0) {
        logf("wifi: scan failed (%d)", n);
        return false;
    }
    std::vector<WifiCandidate> heard;
    for (auto& net : saved) {
        int best = -1;
        for (int i = 0; i < n; i++)
            if (settings::wifi_same(net.first, WiFi.SSID(i).c_str()) && (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best))) best = i;
        if (best < 0) continue;
        WifiCandidate c{WiFi.SSID(best).c_str(), net.second, (int)WiFi.channel(best), (int)WiFi.RSSI(best), {}};
        memcpy(c.bssid, WiFi.BSSID(best), 6);
        heard.push_back(c);
    }
    WiFi.scanDelete();
    if (heard.empty()) {
        logf("wifi: heard %d networks, none of them saved", n);
        return false;
    }
    for (auto& c : heard) {
        if (wifi_interrupted()) return false;
        bool low = (c.rssi > -45) != ((round & 1) != 0);
        logf("wifi: joining %s (ch %d, %d dBm) at %s power", c.ssid.c_str(), c.ch, c.rssi, low ? "low" : "full");
        if (wifi_connect(c, low, 150)) {
            wifi_quick = c;
            wifi_quick_low = low;
            wifi_have_quick = true;
            return true;
        }
        logf("wifi: %s failed (status %d)", c.ssid.c_str(), (int)WiFi.status());
        WiFi.disconnect();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    return false;
}

static void wifi_scan() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.disconnect();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    int n = WiFi.scanNetworks(false, false, false, 300);
    std::vector<WifiHeard> heard;
    for (int i = 0; i < n; i++) {
        std::string ssid = WiFi.SSID(i).c_str();
        if (ssid.empty()) continue;
        WifiHeard* same = nullptr;
        for (auto& h : heard)
            if (h.ssid == ssid) same = &h;
        if (same) {
            if (WiFi.RSSI(i) > same->rssi) same->rssi = WiFi.RSSI(i);
        } else {
            heard.push_back({ssid, (int)WiFi.RSSI(i), WiFi.encryptionType(i) == WIFI_AUTH_OPEN});
        }
    }
    if (n >= 0) WiFi.scanDelete();
    std::sort(heard.begin(), heard.end(), [](const WifiHeard& x, const WifiHeard& y) { return x.rssi > y.rssi; });
    if (heard.size() > 12) heard.resize(12);
    xSemaphoreTake(wifi_mu, portMAX_DELAY);
    wifi_heard = heard;
    xSemaphoreGive(wifi_mu);
    logf("wifi: scan heard %d networks", (int)heard.size());
}

static void wifi_task(void*) {
    unsigned round = 0;
    for (;;) {
        if (wifi_scan_state == 1) {
            wifi_scan();
            wifi_scan_state = 2;
        }
        if (wifi_sleep) {                 // asleep: the radio stays off until a key wakes the remote
            if (WiFi.getMode() != WIFI_OFF) {
                WiFi.disconnect(true);
                wifi_woke = true;
                logf("wifi: off");
            }
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }
        if (wifi_kick) {
            wifi_kick = false;
            WiFi.disconnect();
            vTaskDelay(pdMS_TO_TICKS(300));
            round = 0;
        }
        if (!wifi_hold && WiFi.status() != WL_CONNECTED && !settings::wifi().empty()) {
            if (wifi_join(round)) {
                round = 0;
            } else {
                round++;
                logf("wifi: no saved network let us in (round %u)", round);
            }
        }
        for (int t = 0; t < 10 && wifi_scan_state != 1 && !wifi_kick; t++) vTaskDelay(pdMS_TO_TICKS(500));
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
    wifi_mu = xSemaphoreCreateMutex();
    settings::begin();
#ifdef ROOK_GAMEPAD
    devweb_mu = xSemaphoreCreateMutex();
    seed_wifi();
#endif
    settings::wifi_dedupe();
#ifndef ROOK_GAMEPAD
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
#endif
    ui::begin(OLED_ADDR, settings::rotation());
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
    WiFi.setAutoReconnect(false);     // wifi_task rejoins: it picks the strongest network each time
    WiFi.setHostname(settings::name().c_str());
#ifdef ROOK_GAMEPAD
    devweb_begin();
#endif
    for (auto& n : settings::wifi()) wifi.addAP(n.first.c_str(), n.second.c_str());
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
    {
        std::string tz = settings::kv("tz");       // POSIX, e.g. CST6CDT,M3.2.0,M11.1.0; blank: UTC
        setenv("TZ", tz.empty() ? "UTC0" : tz.c_str(), 1);
        tzset();
    }

    chat::begin();
    load_sleep();
    cpu_full = getCpuFrequencyMhz();
    active();
    ota::begin([](const char* m) { logf("%s", m); }, [](const std::string& t) {
        active();
        if (t.empty()) { ui::text("", 0); return; }
        size_t nl = t.find('\n');
        std::string body = nl == std::string::npos ? t : t.substr(nl + 1);
        std::replace(body.begin(), body.end(), '\n', ' ');
        ui::card("UPDATE", body, !body.empty() && body.back() == '%' ? atoi(body.c_str()) : -1);
    });
    lan::begin([](const char* m) { logf("%s", m); });
    dev::begin([](const char* m) { logf("%s", m); }, [](const std::string& t) { ui::flash(t); });
    set_local(settings::kv("local"));

    const Enrollment& e = settings::enrollment();
    if (e.ok) {
        rc::WorkerConfig c;
        c.worker_id = e.worker_id;
        c.name = settings::name();
#ifdef ROOK_GAMEPAD
        c.description = "Rook remote in a game controller: its keys on the band, a small screen, games";
#elif defined(ROOK_BRIDGE)
        c.description = "Rook remote bridge: puts a Bluetooth remote's keys on the band, with a small screen";
#else
        c.description = "Rook remote: a handheld with 11 buttons and a small screen";
#endif
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
            worker.on("remote.chat", cap_chat);
            worker.on("remote.screen", cap_screen);
            worker.on("remote.press", cap_press);
            worker.on("remote.map", cap_map);
            worker.on("remote.ask", cap_ask, true);
            worker.on("remote.wait", cap_wait, true);
            worker.on("remote.bt", cap_bt);
            worker.on("remote.wifi", cap_wifi);
            worker.on("remote.scan", cap_scan, true);
            worker.on("remote.devices", cap_devices);
            worker.on("remote.device", cap_device, true);
            worker.on("remote.update", cap_update);
            worker_on = true;
        } else {
            logf("enrollment is unusable (hub root key or manifest); enroll again");
        }
    }
    xTaskCreatePinnedToCore(rook_task, "rook", 16384, nullptr, 1, nullptr, APP_CORE);
    xTaskCreatePinnedToCore(wifi_task, "wifi", 4096, nullptr, 1, nullptr, 0);
    bt_names_mu = xSemaphoreCreateMutex();
    load_bt_names();
    bt::begin([](const char* m) { logf("%s", m); });
    logf("Rook remote %s ready; worker %s", FW_VERSION, worker_on ? "on" : "off (not enrolled)");
}

// What a held button (the handheld's, or the paired remote's) does in the game.
static uint16_t game_key(const std::string& n) {
#ifdef ROOK_BRIDGE
    if (GAMES[game_kind].flat)       // the screen as it sits: the keys mean what they say (ROTATE is up)
        return n == "left" ? tetris::LEFT : n == "right" ? tetris::RIGHT : n == "down" ? tetris::DOWN :
               n == "up" ? tetris::ROTATE : (n == "vol_up" || n == "vol_down") ? tetris::ROTATE_BACK :
               n == "ok" ? tetris::DROP : n == "home" ? tetris::HOLD : (n == "mute" || n == "play") ? tetris::PAUSE :
               n == "back" ? tetris::QUIT : 0;
    // The game is drawn a quarter turn clockwise, so the remote is held with its
    // top to the left and its keys turn with it: its up is the game's left.
    // Volume turns the piece (up: left, down: right), home puts it aside.
    return n == "up" ? tetris::LEFT : n == "down" ? tetris::RIGHT : n == "left" ? tetris::DOWN :
           n == "right" ? tetris::ROTATE : n == "vol_up" ? tetris::ROTATE_BACK : n == "vol_down" ? tetris::ROTATE :
           n == "ok" ? tetris::DROP : n == "home" ? tetris::HOLD : (n == "mute" || n == "play") ? tetris::PAUSE :
           n == "back" ? tetris::QUIT : 0;
#endif
    return n == "left" ? tetris::LEFT : n == "right" ? tetris::RIGHT : n == "down" ? tetris::DOWN :
           n == "up" ? tetris::ROTATE : n == "ok" ? tetris::DROP : n == "home" ? tetris::PAUSE :
           n == "back" ? tetris::QUIT : 0;
}

void loop() {
    static uint32_t next_status = 0, next_scan = 0;
    uint32_t ms = millis();
    if ((int32_t)(ms - next_scan) >= 0) {
        next_scan = ms + 5;
        buttons::poll();
        if (swallow && buttons::held().empty() && bt::held().empty()) swallow = false;
        if (mode == GAME && !ui::asking()) {
            uint16_t keys = 0;
            for (int i : buttons::held()) {
                std::string n = buttons::name(i);
                keys |= game_key(n);
            }
#ifdef ROOK_GAMEPAD
            keys |= pad::game_keys(GAMES[game_kind].flat);       // a pad holds several at once
#else
            std::string bt_held = bt::held();
            if (!bt_held.empty()) keys |= game_key(bt_name_of(bt_held));
#endif
            if (!inject_held.empty() && (int32_t)(ms - inject_until) < 0) keys |= game_key(inject_held);
            const GameDef& gd = GAMES[game_kind];
            if (gd.tick(ms, keys)) ui::redraw();
            if (gd.quit()) {
                if (gd.draw) ui::canvas(nullptr);
                else ui::game(false);
                mode = REMOTE;
                swallow = true;
                ui::flash("score\n" + std::to_string(gd.score()));
            }
        }
    }
    // A key pressed from the band (remote.press).
    {
        std::string name;
        xSemaphoreTake(ev_mu, portMAX_DELAY);
        if (!injected.empty() && (int32_t)(ms - inject_until) >= 60) {
            name = injected.front();
            injected.pop_front();
        }
        xSemaphoreGive(ev_mu);
        if (!name.empty()) {
            inject_held = name;
            inject_until = ms + 120;
            ButtonEvent e{0, -1, name, "band", "press", 0, ms};
            on_button(e);
        }
    }
    // The paired Bluetooth remote's keys, and what became of it.
    std::string bt_key;
    while (bt::next_key(bt_key)) {
        ButtonEvent e{0, -1, bt_name_of(bt_key), "bt:" + bt_key, "press", 0, ms};
        on_button(e);
    }
    // A key of the paired remote that means more when held: let go, or still down after HOLD_MS?
    if (guide_at) {
        if (mode != REMOTE) {
            guide_at = 0;
        } else if (bt::held() != guide_key) {
            guide_at = 0;
            open_menu();
        } else if ((int32_t)(millis() - guide_at) >= (int32_t)HOLD_MS) {
            guide_at = 0;
            remember_tab(TAB_ASSIST);
            open_chat();
        }
    }
    if (ok_at) {
        if (mode != MENU || menu_kind != MENU_WIFI) {
            ok_at = 0;
        } else if (bt::held() != ok_key) {
            ok_at = 0;
            menu_pick(ok_index);
        } else if ((int32_t)(millis() - ok_at) >= (int32_t)HOLD_MS) {
            ok_at = 0;
            wifi_prefer_row(ok_index);
        }
    }
    static std::string bt_was = "off";
    std::string bt_now = bt::state();
#ifdef ROOK_BRIDGE
    // Nothing to press here: keep looking for a remote to pair until there is one.
    static uint32_t next_pair = 4000;
    if (!bt::saved() && bt_now != "pairing" && (int32_t)(ms - next_pair) >= 0) {
        next_pair = ms + 40000;
        bt::pair(30);
        ui::text(BT_PAIR_TEXT, 30);
        bt_now = bt::state();
    }
#endif
    if (bt_now != bt_was) {
        if (bt_was == "pairing") ui::text("", 0);
        if (bt_now == "connected") ui::flash("BT on\n" + bt::name());
        else if (bt_was == "pairing" && bt_now != "pairing") ui::flash("BT:\nnone\nfound");
        else if (bt_was == "connected") ui::flash("BT\nsleep");
        bt_was = bt_now;
    }
    if (wifi_pending && wifi_scan_state == 2) {
        wifi_pending = false;
        wifi_scan_state = 0;
        ui::text("", 0);
        if (mode == REMOTE) open_wifi_menu();
    }
    // The microphone: say so on the screen, and send the audio on as it comes.
    bool voice_open = bt::voice_open();
    if (voice_open && !voice_was_open) {
        voice_id = esp_random() | 1;
        voice_seq = voice_bytes = 0;
        voice_started = ms;
        voice_hold = false;
        active();
        // The conversation comes up (the menu's Assistant tab), unless a game,
        // the keyboard or a Wi-Fi or device list has the screen.
        if (mode == REMOTE || (mode == MENU && menu_kind == MENU_MAIN)) {
            if (!chat_up()) open_chat();
            chat::status("Listening (any key)");
            ui::redraw();
        } else {
            ui::text("Listening...", 20);
        }
    }
    if (voice_open) {
        std::string held = bt_name_of(bt::held());
        bool down = is_name(held, {"assistant", "search", "voice"});
        if (down && ms - voice_started > 600) voice_hold = true;      // held while talking: letting go ends it
        if (voice_hold && !down) bt::voice_stop();
        if (worker_on && worker.events_queued() > 16) bt::voice_stop();   // the hub is not keeping up
    }
    voice_was_open = voice_open;
    // Listening and thinking move on the screen: the meter, the dots, the clock.
    static uint32_t voice_frame = 0;
    if (chat_up() && (voice_open || thinking_until)) menu_until = ms + CHAT_IDLE_MS;      // not while it is going on
    if (chat_up() && (voice_open || thinking_until) && ms - voice_frame >= 70) {
        voice_frame = ms;
        if (voice_open) chat::level(bt::voice_level());
        ui::redraw();
    }
    {
        std::string audio;
        bool done = false;
        if (voice_id && bt::voice_take(audio, done, 1800)) {
            char id[12];
            snprintf(id, sizeof id, "%08x", (unsigned)voice_id);
            if (!audio.empty() && worker_on) {
                JsonDocument p;
                p["id"] = id;
                p["seq"] = voice_seq++;
                p["data"] = rc::b64((const uint8_t*)audio.data(), audio.size());
                worker.emit("remote.voice.part", p.as<JsonObjectConst>());
                voice_bytes += audio.size();
            }
            if (done) {
                if (voice_bytes && worker_on) {      // even when it heard no speech itself: the hub's ears are better
                    JsonDocument p;
                    p["id"] = id;
                    p["parts"] = voice_seq;
                    p["bytes"] = voice_bytes;
                    p["codec"] = bt::voice_codec() == 2 ? "adpcm16k" : "adpcm8k";
                    p["worker"] = settings::name();
                    p["ms"] = ms - voice_started;
                    worker.emit("remote.voice", p.as<JsonObjectConst>());
                    if (chat_up()) {
                        chat::status("Thinking...");
                        thinking_until = ms + 75000;
                        menu_until = ms + CHAT_IDLE_MS;
                        ui::redraw();
                    } else {
                        ui::text("Thinking...", 45);
                    }
                } else {
                    chat::status("");
                    ui::redraw();
                    ui::text("", 0);
                    ui::flash(worker_on ? "heard\nnothing" : "no hub");
                }
                voice_id = 0;
            }
        }
    }
    // Something new in the conversation: show it (unless the menu, the game or the keyboard has the screen).
    if (chat_show) {
        chat_show = false;
        thinking_until = 0;
        if (mode == REMOTE) open_chat();
        if (chat_up()) menu_until = ms + CHAT_IDLE_MS;
        ui::redraw();
    }
    if (thinking_until && (int32_t)(ms - thinking_until) >= 0) {       // no answer came
        thinking_until = 0;
        chat::status("");
        ui::redraw();
    }
    static uint32_t renames_seen = 0;
    if (lan::renames() != renames_seen) {            // a device got its MAC id: follow it (found by address)
        renames_seen = lan::renames();
        lan::Device d;
        if (!local_id.empty() && lan::find(local_id, d) && d.id != local_id) set_local(d.id);
    }
    if (devs_pending && lan::scans() != devs_scans) {
        devs_pending = false;
        ui::text("", 0);
        if (mode == REMOTE) open_devs_menu();
    }
    // A look around each network joined, once it has settled, so the menu has its devices.
    static uint32_t up_since = 0;
    static bool looked = false;
    if (WiFi.status() != WL_CONNECTED) {
        up_since = 0;
        looked = false;
    } else if (!up_since) {
        up_since = ms | 1;
    } else if (!looked && ms - up_since > 8000 && !ota::busy()) {
        std::string err;
        looked = dev::run("scan", "", "", 0, err);
    }
    if (mode == TYPING && (int32_t)(ms - typing_until) >= 0) {
        ui::canvas(nullptr);
        osk::begin("", "");
        mode = REMOTE;
    }
    if (mode == MENU && (int32_t)(ms - menu_until) >= 0) {
        ui::menu_end();
        mode = REMOTE;
    }
    poll_serial();
#ifdef ROOK_GAMEPAD
    devweb.handleClient();
    pad::tick();
#endif
    if ((int32_t)(ms - next_status) >= 0) {
        next_status = ms + 500;
        bool up = WiFi.status() == WL_CONNECTED;
        std::string s0;
        if (!settings::enrollment().ok) s0 = "setup";
        else if (!up) s0 = settings::wifi().empty() ? "wifi?" : "wifi-";
        else if (!worker.clock_ok()) s0 = "clock";
        else s0 = worker.hub_ok() ? "band" : "hub?";
        ui::status_line(0, s0);
#ifdef ROOK_GAMEPAD
        if (s0 == "band") pad::light(0, 0, 28);              // the light bar: on the band, or not
        else pad::light(40, 12, 0);
#endif
        ui::network(up ? std::string(WiFi.SSID().c_str()) : std::string());
        ui::status_line(1, bt::state() == "connected" ? "TV: B" : "TV:");
        ui::status_line(2, local_id.empty() ? settings::target_name() : local_name);
        ota::tick(worker_on && worker.hub_ok());
        // Left alone: the screen goes dark, then Wi-Fi goes off too. Not while something is going on.
        if (mode == GAME || mode == TYPING || bt::voice_open() || ota::busy() || ui::asking() ||
            bt::state() == "pairing" || buttons::learning() || thinking_until)
            active();
        // millis() again: active() may have just moved last_active past this pass's ms,
        // and the difference, unsigned, would read as weeks of idling.
        int32_t since = (int32_t)(millis() - last_active);
        uint32_t idle = since > 0 ? (uint32_t)since : 0;
        set_power(sleep_s && idle > sleep_s * 1000u ? ASLEEP : screen_off_s && idle > screen_off_s * 1000u ? DIM : AWAKE);
        if (buttons::learning()) {
            char b[24];
            snprintf(b, sizeof b, "L%d/%d", buttons::count(), buttons::expected());
            ui::status_line(3, b);
        } else {
            xSemaphoreTake(ev_mu, portMAX_DELAY);
            ui::status_line(3, recent.empty() ? "" : recent.back().name);
            xSemaphoreGive(ev_mu);
        }
    }
    ui::tick();
    delay(1);
}
