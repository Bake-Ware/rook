#include "wifi_setup.h"
#include <WiFi.h>
#include <ArduinoJson.h>
#include <algorithm>
#include <vector>
#include "settings.h"

static const uint32_t PER_NET_WAIT_MS = 15000;

struct WifiNet {
    String ssid;
    String pass;
    int priority;
};

static std::vector<WifiNet> parseNetworks(const String& json) {
    std::vector<WifiNet> out;
    if (json.length() == 0) return out;
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json);
    if (err) {
        Serial.printf("wifi: wifi_networks parse failed: %s\n", err.c_str());
        return out;
    }
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

void setupWifi() {
    const auto& s = getSettings();
    WiFi.mode(WIFI_AP_STA);

    if (s.ap_ssid.length()) {
        if (s.ap_pass.length() >= 8)
            WiFi.softAP(s.ap_ssid.c_str(), s.ap_pass.c_str());
        else
            WiFi.softAP(s.ap_ssid.c_str());
    }

    auto nets = parseNetworks(s.wifi_networks);
    Serial.printf("WiFi: %u remembered network(s) to try\n", (unsigned)nets.size());

    for (const auto& n : nets) {
        Serial.printf("WiFi STA: trying p%d '%s'\n", n.priority, n.ssid.c_str());
        WiFi.disconnect(false, true);
        delay(150);
        WiFi.begin(n.ssid.c_str(), n.pass.c_str());
        uint32_t t0 = millis();
        while (WiFi.status() != WL_CONNECTED && (millis() - t0) < PER_NET_WAIT_MS) {
            delay(250);
        }
        if (WiFi.status() == WL_CONNECTED) break;
        Serial.printf("WiFi STA: '%s' failed, next\n", n.ssid.c_str());
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi STA: connected, ip=%s ssid=%s\n",
                      WiFi.localIP().toString().c_str(),
                      WiFi.SSID().c_str());
    } else {
        Serial.println("WiFi STA: no network associated, AP-only");
    }
}
