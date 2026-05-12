#include "wifi_setup.h"
#include <WiFi.h>
#include "settings.h"

// Permanent APSTA: AP is always reachable (rescue path), STA gets LAN IP.
// Single radio so both share STA's channel — accepted tradeoff.
void setupWifi() {
    const auto& s = getSettings();
    WiFi.mode(WIFI_AP_STA);

    if (s.ap_ssid.length()) {
        if (s.ap_pass.length() >= 8)
            WiFi.softAP(s.ap_ssid.c_str(), s.ap_pass.c_str());
        else
            WiFi.softAP(s.ap_ssid.c_str());  // open AP if pw too short for WPA2
    }

    if (s.sta_ssid.length()) {
        WiFi.begin(s.sta_ssid.c_str(), s.sta_pass.c_str());
        int attempts = 0;
        while (WiFi.status() != WL_CONNECTED && attempts < 30) {
            delay(500);
            attempts++;
        }
    }
}
