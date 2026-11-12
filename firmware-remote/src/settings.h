// Persistent settings in NVS: Wi-Fi networks, the enrollment (home hub, band,
// worker id), the worker key seed and the replay cache. (Button layout: buttons.cpp.)
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <string>
#include <vector>

struct Enrollment {
    bool ok = false;
    std::string worker_id, name, hub, root, psk, relay, owner, url;
};

namespace settings {

void begin();
const Enrollment& enrollment();
bool set_enrollment(JsonObjectConst bundle, const char* name, std::string& err);
void clear_enrollment();

std::string name();
void set_name(const std::string& n);

// Wi-Fi networks, tried in order.
std::vector<std::pair<std::string, std::string>> wifi();
void wifi_add(const std::string& ssid, const std::string& pass);
void wifi_clear();
std::string wifi_last();                       // the network that last let us in: tried first
void set_wifi_last(const std::string& ssid);

// The worker key seed (32 bytes), made once on first boot.
void seed(uint8_t out[32]);

// Replay cache blob (rc::Gate entries) so it survives a restart (spec 6.3).
std::string replay();
void set_replay(const std::string& blob);

// The devices the remote's menu offers and the one chosen: its name rides
// every remote.button event (target), so a job can act on that device.
std::vector<std::string> targets();
void set_targets(const std::vector<std::string>& t);
int target();
void set_target(int i);
std::string target_name();

// The screen's rotation, in quarter turns clockwise (0..3).
int rotation();
void set_rotation(int r);

void factory_reset();

}  // namespace settings
