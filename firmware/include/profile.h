#pragma once
// The minimal worker profile (docs/spec/worker-profile.md): the messages a
// worker sends and answers on a Rook 0.5 band, above the Telesthete carrier.
// Plain C++ over ArduinoJson, so it also builds and runs on a PC
// (tests/test_firmware_profile.py).
#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

// One cap: fills `result`, or returns an error string.
typedef const char* (*CapFn)(JsonVariantConst args, JsonObject result);
struct Cap { const char* name; CapFn fn; };   // name without the plugin; the table ends with {nullptr, nullptr}

struct Profile {
    const char* id;          // worker_id: 32 lowercase hex, the same after every restart
    const char* name;
    const char* version;     // firmware version, shown on the Workers page
    const char* plugin;      // the one builtin manifest: plugin, version, hash (manifest.h)
    const char* plugin_version;
    const char* manifest_hash;
    const char* manifests_hash;
    const Cap* caps;
    char hub[33];            // the hub's node id, from its announce
    uint32_t hub_heard_ms;
};

void profileAnnounce(const Profile& p, JsonDocument& out);
void profileHeartbeat(const Profile& p, JsonDocument& out);

enum Heard { HEARD_NOTHING, HEARD_REPLY, HEARD_NEW_HUB };
// One payload from the band. HEARD_REPLY: a call to us, answered in `out`.
// HEARD_NEW_HUB: a hub we haven't heard from in 90 s (or a new one) announced:
// announce again.
Heard profileHear(Profile& p, const uint8_t* payload, size_t len, uint32_t now_ms, JsonDocument& out);

// Reassembly of the carrier's fragment envelope (core v2 2.1): version,
// fragment id, seq, total, then at most 1003 data bytes.
static const size_t FRAG_HEADER = 21;
static const size_t FRAG_DATA = 1003;
static const uint16_t FRAG_MAX = 8;          // messages over 8 fragments (8,024 bytes) are dropped
static const int FRAG_SLOTS = 4;             // messages being reassembled at once
static const uint32_t FRAG_TIMEOUT_MS = 30000;

struct Reassembler {
    struct Slot {
        uint8_t fid[16];
        uint16_t total;
        uint16_t got;                         // bit per fragment received
        uint16_t len[FRAG_MAX];
        uint8_t* buf;                         // total * FRAG_DATA bytes, or nullptr: free slot
        uint32_t started_ms;
    } slots[FRAG_SLOTS];
};

void reassemblerInit(Reassembler& r);
// One fragment. Returns the whole message's length and sets *out to a
// malloc'd copy the caller frees; 0 while incomplete or for anything dropped.
size_t reassemble(Reassembler& r, const uint8_t* frag, size_t len, uint32_t now_ms, uint8_t** out);
