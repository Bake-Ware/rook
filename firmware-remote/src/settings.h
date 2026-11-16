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

// Wi-Fi networks: the list's order is the order they are joined in. Names
// match whatever their case; adding one puts it at the top.
std::vector<std::pair<std::string, std::string>> wifi();
bool wifi_same(const std::string& a, const std::string& b);   // the same network's name
void wifi_add(const std::string& ssid, const std::string& pass);
void wifi_clear();
void wifi_remove(const std::string& ssid);
bool wifi_prefer(const std::string& ssid);     // to the top; false: not saved
void wifi_dedupe();                            // at boot: one entry a name, the first one's password

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

// What the paired Bluetooth remote's keys are called: a JSON object, raw key
// name (bt_c3) to the name it is sent under (up).
std::string bt_names();
void set_bt_names(const std::string& json);

// Small named values other modules keep (keys of at most 15 characters).
std::string kv(const char* key);
void set_kv(const char* key, const std::string& value);

void factory_reset();

}  // namespace settings
