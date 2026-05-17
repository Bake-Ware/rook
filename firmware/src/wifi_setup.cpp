#include "wifi_setup.h"
#include <WiFi.h>
#include "settings.h"

static const uint32_t PRIMARY_WAIT_MS = 15000;

// Try primary STA. If it doesn't associate, fall back to the phone hotspot
// (if configured). AP stays up the whole time as a rescue path.
void setupWifi() {
    const auto& s = getSettings();
    WiFi.mode(WIFI_AP_STA);

    if (s.ap_ssid.length()) {
        if (s.ap_pass.length() >= 8)
            WiFi.softAP(s.ap_ssid.c_str(), s.ap_pass.c_str());
        else
            WiFi.softAP(s.ap_ssid.c_str());
    }

    if (s.sta_ssid.length()) {
        Serial.printf("WiFi STA: trying %s\n", s.sta_ssid.c_str());
        WiFi.begin(s.sta_ssid.c_str(), s.sta_pass.c_str());
        uint32_t t0 = millis();
        while (WiFi.status() != WL_CONNECTED && (millis() - t0) < PRIMARY_WAIT_MS) {
            delay(250);
        }
    }

    if (WiFi.status() != WL_CONNECTED && s.phone_ssid.length()) {
        Serial.printf("WiFi STA: primary failed, trying phone hotspot %s\n",
                      s.phone_ssid.c_str());
        WiFi.disconnect(false, true);
        delay(200);
        WiFi.begin(s.phone_ssid.c_str(), s.phone_pass.c_str());
        uint32_t t0 = millis();
        while (WiFi.status() != WL_CONNECTED && (millis() - t0) < PRIMARY_WAIT_MS) {
            delay(250);
        }
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi STA: connected, ip=%s ssid=%s\n",
                      WiFi.localIP().toString().c_str(),
                      WiFi.SSID().c_str());
    } else {
        Serial.println("WiFi STA: no connection, AP-only");
    }
}
