#include "wifi_setup.h"
#include <WiFi.h>
#include "config.h"

void setupWifi() {
#if defined(WIFI_STA_SSID) && defined(WIFI_STA_PASS)
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_STA_SSID, WIFI_STA_PASS);
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
        delay(500);
        attempts++;
    }
    if (WiFi.status() == WL_CONNECTED) return;
#endif
    WiFi.mode(WIFI_AP);
    WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASS);
}
