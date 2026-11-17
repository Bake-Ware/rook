// What else is on the Wi-Fi network the remote has joined: a scan asks over
// SSDP and mDNS and keeps a table of what answered (TVs, streaming boxes,
// smart-home devices), by address, with the kinds each answered as and the
// driver the remote has for it, if any (dev.h). A device is known by its MAC
// address when the network gives it, so it stays the same device when its IP
// changes. Devices the person picked or paired are remembered across restarts.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <functional>
#include <string>
#include <vector>

namespace lan {

struct Device {
    std::string id;                    // MAC without colons, else the address
    std::string name, model, ip, mac;
    std::string driver;                // roku, webos, or blank: seen but not drivable
    std::vector<std::string> kinds;    // roku, webos, cast, androidtv, airplay, dial, samsung, wled...
    uint16_t port = 0;                 // the driver's port, when the device said
    bool seen = false;                 // answered the last scan
    bool kept = false;                 // remembered across restarts
};

void begin(std::function<void(const char*)> log);
int scan();                            // blocks for about 15 s; how many devices answered
uint32_t scans();                      // how many scans have finished
std::vector<Device> devices();
bool find(const std::string& id_or_name, Device& out);
void keep(const std::string& id);      // remember this one across restarts
void forget(const std::string& id);
void learn_mac(const std::string& id); // look its MAC up again (after talking to it); may change its id
// Called (on the task that scanned or learned) when a device known by its address
// gets its MAC id, so what was filed under the old id can follow it.
void on_rekey(std::function<void(const std::string& old_id, const std::string& new_id)> f);
uint32_t renames();                    // how many ids have changed so far
std::string mac_of(const std::string& ip, int tries = 1);   // ARP; each try waits 200 ms
bool wake(const std::string& mac);     // Wake-on-LAN
void fill(const Device& d, JsonObject o);

}  // namespace lan
