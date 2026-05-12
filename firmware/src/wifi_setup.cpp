#include "wifi_setup.h"
#include <WiFi.h>
#include "config.h"

// Permanent APSTA: AP is always reachable (rescue path), STA gets LAN IP.
// Single radio so both share STA's channel — accepted tradeoff.
void setupWifi() {
    WiFi.mode(WIFI_AP_STA);

#if defined(WIFI_AP_SSID) && defined(WIFI_AP_PASS)
    WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASS);
#endif

#if defined(WIFI_STA_SSID) && defined(WIFI_STA_PASS)
    WiFi.begin(WIFI_STA_SSID, WIFI_STA_PASS);
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
        delay(500);
        attempts++;
    }
#endif
}
