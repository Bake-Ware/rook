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

// ROOK: the key's press goes to the hub as its remote.button event and that is
// all (jobs on the hub do what it means). Stored as a cap action with no cap
// name, so a build before 0.12.8 still reads the profile after a rollback.
enum Type : uint8_t { NEC = 0, RAW = 1, LAN = 2, CAP = 3, ROOK = 4 };

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
    uint64_t lan = 0, cap = 0, rook = 0;       // those whose action is lan, cap, rook (the rest: ir)
    // Keys by any other name the remote reports (bt_c77...): key id 64 + i is
    // custom[i]; masks by i, as above.
    std::vector<std::string> custom;
    uint32_t ckeys = 0, clan = 0, ccap = 0, crook = 0;
    uint16_t bytes = 0;
    uint8_t slot = 0;
};

void begin();
std::vector<Info> list();
bool find(const std::string& name, Info& out);                    // any case
bool linked_to(const std::string& lan_id, Info& out);             // the first profile with this link
// Keys: the named ones (power, vol_up... ids 0..63), and in a profile any
// other name the remote reports (bt_c77, the onn's YouTube key; [a-z0-9_],
// 1..MAX_KEY characters; at most MAX_CUSTOM a profile; ids 64 + i).
static const size_t MAX_KEY = 15;
static const int MAX_CUSTOM = 32, CUSTOM = 64;
int key_id(const std::string& name);           // a named key's id; -1: not one (aliases: settings, bt_menu = menu...)
int key_of(const Info& p, const std::string& name);   // its id in p (named or custom); -1: p has no such key
bool valid_key(const std::string& name);       // a named key, or a name a custom key may have
std::string key_label(const Info& p, int id);  // the key's name ("" for none)
const char* key_name(int id);                  // a named key's name
std::string key_names();                       // the named ones, for errors
inline bool has(const Info& p, int key) {
    if (key < 0) return false;
    if (key < CUSTOM) return p.keys >> key & 1;
    return key < CUSTOM + MAX_CUSTOM && (p.ckeys >> (key - CUSTOM) & 1);
}
bool code(const Info& p, int key, Code& out, std::string& err);  // the key's action (from RAM once read: see warm)
bool warm(const Info& p);                      // p's codes into RAM now (the profile picked): its keys read no flash
uint32_t flash_reads();                        // flash reads made for codes, so far
bool repeats(int key);                         // held, it repeats (volume, arrows, channels, seek)

// keys: {key: {protocol: "nec", address, command} | {raw: [...], carrier?} | {lan: true | "<key>"} |
//        {cap: "x.y", worker?, args?} | {rook: true} | null (removes it)}.
// merge false: the profile is exactly this. set_link false: the link stays as it was.
// icon: an Icon, or -1 to keep the one it has.
bool save(const std::string& name, bool set_link, const std::string& link, JsonObjectConst keys, bool merge,
          std::string& err, bool& created, int icon = -1);
bool remove(const std::string& name, std::string& err);
bool relink(const std::string& name, const std::string& link);
bool rename(const std::string& name, const std::string& to, std::string& err);
bool set_icon(const std::string& name, int icon, std::string& err);
std::string clean_name(const std::string& name);   // trimmed; blank when it is no good as a name
void fill(const Info& p, JsonObject o);        // name, link, icon, keys, lan_keys, cap_keys, rook_keys, bytes
size_t total_bytes();

}  // namespace prof
