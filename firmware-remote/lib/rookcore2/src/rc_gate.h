// Worker-side enforcement for a single-band firmware worker (spec core-v2
// 5.2, 5.4, 6.3, 9.2): the home hub's anchor, its operational keys learned
// from rook-hubkey grants, hub contact from signed hub announces, the ticket
// check and the replay cache. Mirrors rook/core2/wire.py verify_ticket and
// rook/core2/worker/security.py WorkerGate for the home band only: a firmware
// worker has no memberships, so its home ceiling is everything (9.2).
#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <functional>
#include <string>
#include <vector>

namespace rc {

struct CapMeta {
    std::string name;
    std::string risk;
};

struct Anchor {
    std::string hub;     // hub_id = key_id(root)
    std::string band;    // 32 hex
    uint8_t root[32];
    bool set = false;
};

class Gate {
public:
    static const int SKEW = 30, MIN_LIFETIME = 60, MAX_LIFETIME = 120, REPLAY_KEEP = 900;
    static const int GRANT_GRACE = 3600, ANNOUNCE_SKEW = 90, HUB_LOST = 90;
    static const size_t REPLAY_MAX = 64;

    struct Replay { std::string nonce; int64_t until; };

    Anchor anchor;
    std::string worker_id, name;
    std::vector<CapMeta> caps;
    std::vector<Replay> replay;
    std::function<void()> on_replay_change;     // persist the cache

    // Learn an op key from a grant that arrived on band. True if accepted.
    bool add_hubkey(const std::string& band, JsonObjectConst grant, int64_t now);
    // A signed hub announce: hub contact, and the hub node's worker_id.
    bool hub_announce(const std::string& band, JsonObjectConst msg, int64_t now);
    bool lost(int64_t now) const { return now - contact_ > HUB_LOST; }
    const std::string& hub_node() const { return hub_node_; }
    std::string hub_core() const { return hub_core_; }

    // Decide a request targeted at this worker. Returns "" to run it, else the
    // reason code (no_ticket, malformed, ... , unknown_cap).
    std::string check(JsonObjectConst request, const std::string& band, int64_t now);

    const CapMeta* cap(const char* name) const;

private:
    struct OpKey { std::string kid; uint8_t pub[32]; int64_t until; };
    std::vector<OpKey> keys_;
    std::string hub_node_, hub_core_;
    int64_t contact_ = -1000000000;
    int64_t last_seq_ = -1;
    std::string last_seq_kid_;

    const OpKey* op_key(const std::string& kid, int64_t now);
    bool seen(const std::string& nonce, int64_t now);
    bool remember(const std::string& nonce, int64_t exp, int64_t now);
};

}  // namespace rc
