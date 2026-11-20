// Device profiles, kept on the remote itself (NVS, one blob a profile) so
// they work with no hub and no Wi-Fi: a name, an optional link to a device on
// the network (lan.h id), and a table key name -> action. An action is
//   ir   a code: NEC {address, command} (3 bytes) or raw {carrier, durations}
//        (varints, about 2 bytes a duration), sent from the IR LED;
//   lan  the key (its own name, or another) on the linked device's driver (dev.h);
//   cap  a Rook cap call, {cap, worker, args}, that the hub carries out (the
//        remote cannot start a call itself: it rides the remote.button event).
// A key with no action goes to the linked device, as lan, when there is one.
// Only a small index (name, link, icon, which keys have which action) is held
// in RAM; an action is read from NVS when its key is pressed. The icon is the
// picture the screen draws beside the profile's name (ui.h draws them).
#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string>
#include <vector>

namespace prof {

static const int MAX = 8;                      // profiles
static const size_t MAX_NAME = 20, MAX_LINK = 40;
static const size_t MAX_ARGS = 240;            // a cap action's args, as JSON
static const size_t MAX_RAW = 256;             // durations in one raw code
static const size_t MAX_BYTES = 1536;          // one profile, stored
static const size_t MAX_TOTAL = 4096;          // all of them

enum Type : uint8_t { NEC = 0, RAW = 1, LAN = 2, CAP = 3 };

// The pictures a profile can have. NONE shows as GENERIC.
enum Icon : uint8_t { ICON_NONE = 0, ICON_TV, ICON_PROJECTOR, ICON_SPEAKER, ICON_STREAMING, ICON_LIGHT, ICON_PHONE,
                      ICON_CAR, ICON_GAME, ICON_GENERIC, ICON_COUNT };
const char* icon_name(int icon);               // tv, projector, speaker, streaming, light, phone, car, game, generic
const char* icon_label(int icon);              // as the picker shows it: TV, Projector...
int icon_id(const std::string& name);          // -1: no icon by that name (blank, none: ICON_NONE)
std::string icon_names();                      // all of them, for errors

struct Code {
    uint8_t type = NEC;
    bool raw = false;                          // type == RAW
    bool ir() const { return type == NEC || type == RAW; }
    uint16_t address = 0;
    uint8_t command = 0;
    uint32_t carrier = 38000;
    std::vector<uint32_t> d;
    std::string key;                           // LAN: the key sent (blank: the pressed key's own name)
    std::string cap, worker, args;             // CAP: args as JSON text
};

struct Info {
    std::string name, link;
    uint8_t icon = ICON_NONE;
    uint64_t keys = 0;                         // bit per key id with an action
    uint64_t lan = 0, cap = 0;                 // those whose action is lan, cap (the rest: ir)
    uint16_t bytes = 0;
    uint8_t slot = 0;
};

void begin();
std::vector<Info> list();
bool find(const std::string& name, Info& out);                    // any case
bool linked_to(const std::string& lan_id, Info& out);             // the first profile with this link
int key_id(const std::string& name);           // -1: not a key profiles have (aliases: settings, bt_menu = menu...)
const char* key_name(int id);
std::string key_names();                       // all of them, for errors
inline bool has(const Info& p, int key) { return key >= 0 && (p.keys >> key & 1); }
bool code(const Info& p, int key, Code& out, std::string& err);  // the key's action, read from NVS
bool repeats(int key);                         // held, it repeats (volume, arrows, channels, seek)

// keys: {key: {protocol: "nec", address, command} | {raw: [...], carrier?} | {lan: true | "<key>"} |
//        {cap: "x.y", worker?, args?} | null (removes it)}.
// merge false: the profile is exactly this. set_link false: the link stays as it was.
// icon: an Icon, or -1 to keep the one it has.
bool save(const std::string& name, bool set_link, const std::string& link, JsonObjectConst keys, bool merge,
          std::string& err, bool& created, int icon = -1);
bool remove(const std::string& name, std::string& err);
bool relink(const std::string& name, const std::string& link);
bool rename(const std::string& name, const std::string& to, std::string& err);
bool set_icon(const std::string& name, int icon, std::string& err);
std::string clean_name(const std::string& name);   // trimmed; blank when it is no good as a name
void fill(const Info& p, JsonObject o);        // name, link, icon, keys, lan_keys, cap_keys, bytes
size_t total_bytes();

}  // namespace prof
