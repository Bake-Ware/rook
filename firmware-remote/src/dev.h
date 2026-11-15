// Drivers for the devices a scan found (lan.h), and the one task that talks
// to them, so the buttons never wait on the network:
//   roku   ECP, plain HTTP on port 8060, no pairing
//   webos  LG TVs: SSAP over a WebSocket (ws :3000, else wss :3001), paired
//          once by accepting a prompt on the TV; the key it gives is kept
// Actions: scan, key (a button's name: up, ok, vol_up...), launch (an app's
// name or id), power (off when it answers, Wake-on-LAN when it doesn't),
// pair, forget.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <functional>
#include <string>
#include <vector>
#include "lan.h"

namespace dev {

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> note);
// Queue an action for the device task. wait_ms 0: don't wait for it (true:
// queued). Otherwise true when it worked; err says why not.
bool run(const std::string& action, const std::string& device, const std::string& arg, uint32_t wait_ms,
         std::string& err, std::string* out = nullptr);
bool paired(const lan::Device& d);     // needs no pairing, or has been paired
std::vector<std::string> supports(const lan::Device& d);
void fill(const lan::Device& d, JsonObject o);   // lan::fill plus supports and paired

}  // namespace dev
