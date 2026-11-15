// Firmware updates over the air: the remote pulls a build from a URL into its
// spare app slot, checks it against the SHA-256 it was given (that hash came
// over the band, so the download itself may be plain http or unverified
// https), and restarts into it. The new build is on trial: it has five
// minutes to reach the hub, or the bootloader goes back to the build before.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <functional>
#include <string>

namespace ota {

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> show);
// Call often: confirms a build on trial once the hub is reached, or gives it up.
void tick(bool hub_ok);
void confirm();                        // keep the build on trial (by hand, over serial)
bool busy();
// Start an update in the background. False with err when it can't start.
bool start(const std::string& url, const std::string& sha256, std::string& err);
void fill(JsonObject o);               // state, percent, error, slot, trial

}  // namespace ota
