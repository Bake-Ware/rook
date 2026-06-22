#include "wifi_setup.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>
#include <algorithm>
#include "settings.h"
#include "config.h"  // ROOK_MDNS_HOST — mDNS hostname

// (Re)start mDNS so the dongle is reachable as ROOK_MDNS_HOST.local even when
// DHCP moves its IP. Safe to call on every (re)connect; ends a prior responder
// first so the STA address binding refreshes.
static void ensureMdns() {
    MDNS.end();
    if (MDNS.begin(ROOK_MDNS_HOST)) {
        MDNS.addService("http", "tcp", HTTP_PORT);
        Serial.printf("mDNS: %s.local -> %s\n",
                      ROOK_MDNS_HOST, WiFi.localIP().toString().c_str());
    } else {
        Serial.println("mDNS: begin failed");
    }
}

static const uint32_t PER_NET_WAIT_MS = 8000;
static const uint32_t MONITOR_PERIOD_MS = 45000;

static volatile bool g_kick = false;

static std::vector<WifiNet> parseJson(const String& json) {
    std::vector<WifiNet> out;
    if (!json.length()) return out;
    JsonDocument doc;
    if (deserializeJson(doc, json)) return out;
    if (!doc.is<JsonArray>()) return out;
    for (JsonObject o : doc.as<JsonArray>()) {
        WifiNet n;
        n.ssid = String(o["ssid"].as<const char*>() ? o["ssid"].as<const char*>() : "");
        n.pass = String(o["pass"].as<const char*>() ? o["pass"].as<const char*>() : "");
        n.priority = o["priority"] | 100;
        if (n.ssid.length()) out.push_back(n);
    }
    std::sort(out.begin(), out.end(),
              [](const WifiNet& a, const WifiNet& b) { return a.priority < b.priority; });
    return out;
}

static String toJson(const std::vector<WifiNet>& nets) {
    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    for (const auto& n : nets) {
        JsonObject o = arr.add<JsonObject>();
        o["ssid"] = n.ssid;
        o["pass"] = n.pass;
        o["priority"] = n.priority;
    }
    String out;
    serializeJson(doc, out);
    return out;
}

std::vector<WifiNet> wifiListNetworks() {
    return parseJson(getSettings().wifi_networks);
}

void wifiSaveNetworks(const std::vector<WifiNet>& nets) {
    DeviceSettings s = getSettings();
    s.wifi_networks = toJson(nets);
    updateSettings(s);
}

bool wifiAddOrUpdate(const String& ssid, const String& pass, int priority) {
    if (!ssid.length()) return false;
    auto nets = wifiListNetworks();
    for (auto& n : nets) {
        if (n.ssid == ssid) {
            n.pass = pass;
            n.priority = priority;
            wifiSaveNetworks(nets);
            return true;
        }
    }
    nets.push_back({ssid, pass, priority});
    wifiSaveNetworks(nets);
    return true;
}

bool wifiRemove(const String& ssid) {
    auto nets = wifiListNetworks();
    size_t before = nets.size();
    nets.erase(std::remove_if(nets.begin(), nets.end(),
                              [&](const WifiNet& n) { return n.ssid == ssid; }),
               nets.end());
    if (nets.size() == before) return false;
    wifiSaveNetworks(nets);
    return true;
}

void wifiForgetAll() {
    wifiSaveNetworks({});
}

void wifiKickReconnect() { g_kick = true; }

std::vector<String> wifiScanVisible() {
    std::vector<String> out;
    // softAP locks the radio to its channel, so STA scans only see that
    // channel. Drop to STA-only for the scan, then restore APSTA.
    wifi_mode_t prev = WiFi.getMode();
    bool had_ap = (prev == WIFI_AP_STA || prev == WIFI_AP);
    if (had_ap) WiFi.mode(WIFI_STA);
    int n = WiFi.scanNetworks(false, true);  // show_hidden=true
    for (int i = 0; i < n; i++) out.push_back(WiFi.SSID(i));
    WiFi.scanDelete();
    if (had_ap) {
        WiFi.mode(WIFI_AP_STA);
        const auto& s = getSettings();
        if (s.ap_ssid.length()) {
            if (s.ap_pass.length() >= 8)
                WiFi.softAP(s.ap_ssid.c_str(), s.ap_pass.c_str());
            else
                WiFi.softAP(s.ap_ssid.c_str());
        }
    }
    return out;
}

// Ensure firmware-shipped defaults exist in the saved list. Called every boot
// so newly-added defaults (like bifrost) reach dongles that already had a
// populated wifi_networks NVS blob from earlier firmware.
static void seedDefaults() {
    auto nets = parseJson(getSettings().wifi_networks);
    bool changed = false;
    auto ensure = [&](const char* ssid, const char* pass, int prio) {
        if (!ssid || !*ssid) return;
        for (auto& n : nets) {
            if (n.ssid == ssid) {
                if (n.priority > prio) { n.priority = prio; changed = true; }
                if (n.pass != pass)    { n.pass = pass;     changed = true; }
                return;
            }
        }
        nets.push_back({String(ssid), String(pass), prio});
        changed = true;
    };
    // SSIDs are case-sensitive on Wi-Fi — must match exactly.
    ensure("Bifrost", "1234567890", 1);

    // Also fold legacy single-STA + phone hotspot into the list if present.
    const auto& s = getSettings();
    if (s.sta_ssid.length())   ensure(s.sta_ssid.c_str(),   s.sta_pass.c_str(),   5);
    if (s.phone_ssid.length()) ensure(s.phone_ssid.c_str(), s.phone_pass.c_str(), 9);

    if (changed) wifiSaveNetworks(nets);
}

static bool tryConnect(const WifiNet& n) {
    Serial.printf("WiFi STA: trying p%d '%s'\n", n.priority, n.ssid.c_str());
    WiFi.disconnect(false, true);
    delay(150);
    WiFi.begin(n.ssid.c_str(), n.pass.c_str());
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - t0) < PER_NET_WAIT_MS) {
        delay(250);
    }
    return WiFi.status() == WL_CONNECTED;
}

// Scan once, then only attempt visible networks. Skips dead 8s waits on
// SSIDs that aren't broadcasting (e.g. phone hotspot off).
static void connectFromList() {
    auto nets = wifiListNetworks();
    Serial.printf("WiFi: %u remembered network(s)\n", (unsigned)nets.size());
    auto seen = wifiScanVisible();
    Serial.printf("WiFi: %u visible AP(s) in scan\n", (unsigned)seen.size());
    for (const auto& n : nets) {
        bool present = false;
        for (const auto& s : seen) if (s == n.ssid) { present = true; break; }
        if (!present) {
            Serial.printf("WiFi STA: p%d '%s' not visible, skip\n",
                          n.priority, n.ssid.c_str());
            continue;
        }
        if (tryConnect(n)) break;
        Serial.printf("WiFi STA: '%s' failed, next\n", n.ssid.c_str());
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi STA: connected ip=%s ssid=%s\n",
                      WiFi.localIP().toString().c_str(),
                      WiFi.SSID().c_str());
        ensureMdns();
    } else {
        Serial.println("WiFi STA: no network associated, AP-only (monitor will retry)");
    }
}

static int priorityOf(const String& ssid, const std::vector<WifiNet>& nets) {
    for (const auto& n : nets) if (n.ssid == ssid) return n.priority;
    return 999;
}

// Background watcher: every MONITOR_PERIOD_MS, if disconnected or on a
// lower-priority net, scan and try to upgrade. Also triggered on demand via
// wifiKickReconnect().
static void wifiMonitorTask(void*) {
    for (;;) {
        for (uint32_t waited = 0;
             waited < MONITOR_PERIOD_MS && !g_kick;
             waited += 500) {
            vTaskDelay(500 / portTICK_PERIOD_MS);
        }
        bool kicked = g_kick;
        g_kick = false;

        auto nets = wifiListNetworks();
        if (nets.empty()) continue;

        bool connected = (WiFi.status() == WL_CONNECTED);
        int  cur_prio  = connected ? priorityOf(WiFi.SSID(), nets) : 999;
        if (connected && cur_prio == nets.front().priority && !kicked) continue;

        std::vector<String> seen = wifiScanVisible();
        bool acted = false;
        for (const auto& n : nets) {
            if (n.priority >= cur_prio && connected) break;
            bool present = false;
            for (const auto& s : seen) if (s == n.ssid) { present = true; break; }
            if (!present) continue;
            Serial.printf("WiFi: %s to p%d '%s'\n",
                          connected ? "upgrading" : "connecting",
                          n.priority, n.ssid.c_str());
            if (tryConnect(n)) {
                Serial.printf("WiFi: now on %s ip=%s\n",
                              WiFi.SSID().c_str(),
                              WiFi.localIP().toString().c_str());
                ensureMdns();
                acted = true;
                break;
            }
        }

        if (!acted && !connected) {
            // Nothing visible from our list — try blind connects anyway in case
            // the AP doesn't beacon (hidden SSID).
            connectFromList();
        }
    }
}

void setupWifi() {
    seedDefaults();
    const auto& s = getSettings();
    WiFi.mode(WIFI_AP_STA);

    if (s.ap_ssid.length()) {
        if (s.ap_pass.length() >= 8)
            WiFi.softAP(s.ap_ssid.c_str(), s.ap_pass.c_str());
        else
            WiFi.softAP(s.ap_ssid.c_str());
    }

    connectFromList();

    xTaskCreate(wifiMonitorTask, "wifimon", 4096, NULL, 1, NULL);
}
