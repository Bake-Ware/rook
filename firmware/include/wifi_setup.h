#pragma once
#include <Arduino.h>
#include <vector>

struct WifiNet {
    String ssid;
    String pass;
    int priority;
};

void setupWifi();

// Read/write the saved network list as a parsed vector (sorted by priority asc).
std::vector<WifiNet> wifiListNetworks();
void wifiSaveNetworks(const std::vector<WifiNet>& nets);

// Mutators used by the serial TUI / web admin.
bool wifiAddOrUpdate(const String& ssid, const String& pass, int priority);
bool wifiRemove(const String& ssid);
void wifiForgetAll();

// Async-friendly actions invoked from the TUI thread.
void wifiKickReconnect();              // force re-evaluation against saved list
std::vector<String> wifiScanVisible(); // synchronous scan, returns SSIDs
