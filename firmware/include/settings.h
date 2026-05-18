#pragma once
#include <Arduino.h>

struct DeviceSettings {
    String ap_ssid;
    String ap_pass;
    String sta_ssid;   // legacy single STA — superseded by wifi_networks
    String sta_pass;
    String admin_user;
    String admin_pass;

    // Telesthete hub + band
    String hub_host;
    uint16_t hub_port;
    String band_psk;
    String worker_name;

    // Phone hotspot fallback (legacy — also folded into wifi_networks).
    String phone_ssid;
    String phone_pass;

    // Prioritized WiFi network list. JSON array:
    //   [{"ssid":"...","pass":"...","priority":N}, ...]
    // Lower priority value tried first. setupWifi() iterates this list.
    String wifi_networks;
};

// Load from NVS on boot, falling back to compile-time defaults (secrets.h).
void initSettings();

// Read-only view of current settings.
const DeviceSettings& getSettings();

// Persist new settings to NVS and update the in-memory copy.
void updateSettings(const DeviceSettings& s);

// Wipe NVS and re-init from compile-time defaults.
void factoryResetSettings();
