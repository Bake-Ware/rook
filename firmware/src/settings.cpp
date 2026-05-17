#include "settings.h"
#include "config.h"
#include <Preferences.h>
#include <WiFi.h>

#ifndef ADMIN_USER
#define ADMIN_USER "bake"
#endif
#ifndef ADMIN_PASS
#define ADMIN_PASS "poop"
#endif
#ifndef HUB_HOST
#define HUB_HOST "hub.bakeforge.com"
#endif
#ifndef HUB_PORT
#define HUB_PORT 7474
#endif
#ifndef BAND_PSK
#define BAND_PSK "rook-bakenet-default-2026"
#endif
#ifndef PHONE_SSID
#define PHONE_SSID ""
#endif
#ifndef PHONE_PASS
#define PHONE_PASS ""
#endif

static const char* NS = "rook";
static DeviceSettings g;

// MAC suffix gives every dongle a stable, unique-on-first-boot name.
static String defaultWorkerName() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char buf[24];
    snprintf(buf, sizeof(buf), "rook-kvm-%02x%02x%02x",
             mac[3], mac[4], mac[5]);
    return String(buf);
}

void initSettings() {
    Preferences p;
    p.begin(NS, true);  // read-only
    g.ap_ssid    = p.getString("ap_ssid",    WIFI_AP_SSID);
    g.ap_pass    = p.getString("ap_pass",    WIFI_AP_PASS);
    g.sta_ssid   = p.getString("sta_ssid",   WIFI_STA_SSID);
    g.sta_pass   = p.getString("sta_pass",   WIFI_STA_PASS);
    g.admin_user = p.getString("admin_user", ADMIN_USER);
    g.admin_pass = p.getString("admin_pass", ADMIN_PASS);
    g.hub_host    = p.getString("hub_host",    HUB_HOST);
    g.hub_port    = p.getUShort("hub_port",   HUB_PORT);
    g.band_psk    = p.getString("band_psk",   BAND_PSK);
    g.worker_name = p.getString("worker_name", defaultWorkerName());
    g.phone_ssid  = p.getString("phone_ssid", PHONE_SSID);
    g.phone_pass  = p.getString("phone_pass", PHONE_PASS);
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
    p.putString("hub_host",    s.hub_host);
    p.putUShort("hub_port",   s.hub_port);
    p.putString("band_psk",   s.band_psk);
    p.putString("worker_name", s.worker_name);
    p.putString("phone_ssid", s.phone_ssid);
    p.putString("phone_pass", s.phone_pass);
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
