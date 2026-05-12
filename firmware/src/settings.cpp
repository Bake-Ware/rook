#include "settings.h"
#include "config.h"
#include <Preferences.h>

#ifndef ADMIN_USER
#define ADMIN_USER "bake"
#endif
#ifndef ADMIN_PASS
#define ADMIN_PASS "poop"
#endif

static const char* NS = "rook";
static DeviceSettings g;

void initSettings() {
    Preferences p;
    p.begin(NS, true);  // read-only
    g.ap_ssid    = p.getString("ap_ssid",    WIFI_AP_SSID);
    g.ap_pass    = p.getString("ap_pass",    WIFI_AP_PASS);
    g.sta_ssid   = p.getString("sta_ssid",   WIFI_STA_SSID);
    g.sta_pass   = p.getString("sta_pass",   WIFI_STA_PASS);
    g.admin_user = p.getString("admin_user", ADMIN_USER);
    g.admin_pass = p.getString("admin_pass", ADMIN_PASS);
    p.end();
}

const DeviceSettings& getSettings() { return g; }

void updateSettings(const DeviceSettings& s) {
    Preferences p;
    p.begin(NS, false);
    p.putString("ap_ssid",    s.ap_ssid);
    p.putString("ap_pass",    s.ap_pass);
    p.putString("sta_ssid",   s.sta_ssid);
    p.putString("sta_pass",   s.sta_pass);
    p.putString("admin_user", s.admin_user);
    p.putString("admin_pass", s.admin_pass);
    p.end();
    g = s;
}

void factoryResetSettings() {
    Preferences p;
    p.begin(NS, false);
    p.clear();
    p.end();
    initSettings();
}
