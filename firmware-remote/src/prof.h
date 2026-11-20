// IR device profiles, kept on the remote itself (NVS, one blob a profile) so
// they work with no hub and no Wi-Fi: a name, an optional link to a device on
// the network (lan.h id: its keys fill in for those the profile has no code
// for), and a table key name -> code, NEC {address, command} (3 bytes) or raw
// {carrier, durations} (varints, about 2 bytes a duration). Only a small index
// (name, link, which keys have codes) is held in RAM; a code is read from NVS
// when it is sent.
#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string>
#include <vector>

namespace prof {

static const int MAX = 8;                      // profiles
static const size_t MAX_NAME = 20, MAX_LINK = 40;
static const size_t MAX_RAW = 256;             // durations in one raw code
static const size_t MAX_BYTES = 1536;          // one profile, stored
static const size_t MAX_TOTAL = 4096;          // all of them

struct Code {
    bool raw = false;
    uint16_t address = 0;
    uint8_t command = 0;
    uint32_t carrier = 38000;
    std::vector<uint32_t> d;
};

struct Info {
    std::string name, link;
    uint64_t keys = 0;                         // bit per key id with a code
    uint16_t bytes = 0;
    uint8_t slot = 0;
};

void begin();
std::vector<Info> list();
bool find(const std::string& name, Info& out);                    // any case
bool linked_to(const std::string& lan_id, Info& out);             // the first profile with this link
int key_id(const std::string& name);           // -1: not a key profiles have (aliases: bt_menu = menu...)
const char* key_name(int id);
std::string key_names();                       // all of them, for errors
inline bool has(const Info& p, int key) { return key >= 0 && (p.keys >> key & 1); }
bool code(const Info& p, int key, Code& out, std::string& err);  // read from NVS
bool repeats(int key);                         // held, it repeats (volume, arrows, channels, seek)

// keys: {key: {protocol: "nec", address, command} | {raw: [...], carrier?} | null (removes it)}.
// merge false: the profile is exactly this. set_link false: the link stays as it was.
bool save(const std::string& name, bool set_link, const std::string& link, JsonObjectConst keys, bool merge,
          std::string& err, bool& created);
bool remove(const std::string& name, std::string& err);
bool relink(const std::string& name, const std::string& link);
void fill(const Info& p, JsonObject o);        // name, link, keys, bytes
size_t total_bytes();

}  // namespace prof
