#pragma once
#include "Arduino.h"
#define WL_CONNECTED 3
struct HostWiFi {
    int status() { return WL_CONNECTED; }
    bool hostByName(const char*, IPAddress& ip) { ip = IPAddress(0x0100007f); return true; }
};
extern HostWiFi WiFi;
