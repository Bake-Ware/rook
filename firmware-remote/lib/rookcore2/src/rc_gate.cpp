#include "rc_gate.h"
#include "rc_canon.h"
#include "rc_sig.h"
#include "rc_util.h"
#include <string.h>

namespace rc {

static bool is_int(JsonVariantConst v) {
    return !v.is<bool>() && (v.is<long long>() || v.is<unsigned long long>()) ;
}

static bool nonempty_str(JsonVariantConst v) {
    const char* s = v.as<const char*>();
    return v.is<const char*>() && s && *s;
}

const CapMeta* Gate::cap(const char* n) const {
    if (!n) return nullptr;
    for (auto& c : caps) if (c.name == n) return &c;
    return nullptr;
}

bool Gate::add_hubkey(const std::string& band, JsonObjectConst g, int64_t now) {
    if (!anchor.set || band != anchor.band) return false;
    const char* typ = g["typ"].as<const char*>();
    if (!typ || strcmp(typ, "rook-hubkey") != 0 || g["v"].as<int>() != 2 || !is_int(g["v"])) return false;
    std::string rid = key_id(anchor.root);
    const char* hub = g["hub"].as<const char*>();
    const char* kid = g["kid"].as<const char*>();
    if (!hub || !kid || rid != hub || rid != kid || anchor.hub != hub) return false;
    if (!is_int(g["iat"]) || !is_int(g["exp"])) return false;
    if (!verify_sig(anchor.root, g)) return false;
    int64_t iat = g["iat"].as<long long>(), exp = g["exp"].as<long long>();
    if (!(iat - GRANT_GRACE <= now && now <= exp + GRANT_GRACE)) return false;
    bool in_band = false;
    for (JsonVariantConst b : g["bands"].as<JsonArrayConst>())
        if (b.as<const char*>() && band == b.as<const char*>()) in_band = true;
    if (!in_band) return false;
    uint8_t pub[32];
    if (!parse_pub(g["key"].as<const char*>(), pub)) return false;
    std::string k = key_id(pub);
    int64_t until = exp + GRANT_GRACE;
    for (auto& o : keys_) {
        if (o.kid == k) {
            if (until >= o.until) o.until = until;
            return true;
        }
    }
    OpKey o;
    o.kid = k;
    memcpy(o.pub, pub, 32);
    o.until = until;
    keys_.push_back(o);
    if (keys_.size() > 4) keys_.erase(keys_.begin());
    return true;
}

const Gate::OpKey* Gate::op_key(const std::string& kid, int64_t now) {
    for (size_t i = 0; i < keys_.size();) {
        if (keys_[i].until < now) keys_.erase(keys_.begin() + i);
        else i++;
    }
    for (auto& o : keys_) if (o.kid == kid) return &o;
    return nullptr;
}

bool Gate::hub_announce(const std::string& band, JsonObjectConst m, int64_t now) {
    const char* typ = m["typ"].as<const char*>();
    if (!typ || strcmp(typ, "rook-announce") != 0 || m["v"].as<int>() != 2) return false;
    if (m["hubkey"].is<JsonObjectConst>()) add_hubkey(band, m["hubkey"].as<JsonObjectConst>(), now);
    const char* hub = m["hub"].as<const char*>();
    const char* kid = m["kid"].as<const char*>();
    const char* mb = m["band"].as<const char*>();
    if (!hub || !kid || !mb || band != mb || !is_int(m["ts"]) || !is_int(m["seq"])) return false;
    if (anchor.hub != hub) return false;
    const OpKey* k = op_key(kid, now);
    if (!k || !verify_sig(k->pub, m)) return false;
    int64_t ts = m["ts"].as<long long>(), seq = m["seq"].as<long long>();
    if ((ts > now ? ts - now : now - ts) > ANNOUNCE_SKEW) return false;
    if (last_seq_kid_ == kid && seq <= last_seq_) return false;
    last_seq_kid_ = kid;
    last_seq_ = seq;
    contact_ = now;
    const char* node = m["worker_id"].as<const char*>();
    if (node) hub_node_ = node;
    const char* core = m["core"].as<const char*>();
    hub_core_ = core ? core : "";
    return true;
}

bool Gate::seen(const std::string& nonce, int64_t now) {
    bool changed = false;
    for (size_t i = 0; i < replay.size();) {
        if (replay[i].until <= now) { replay.erase(replay.begin() + i); changed = true; }
        else i++;
    }
    (void)changed;
    for (auto& r : replay) if (r.nonce == nonce) return true;
    return false;
}

bool Gate::remember(const std::string& nonce, int64_t exp, int64_t now) {
    int64_t until = exp + SKEW > now + REPLAY_KEEP ? exp + SKEW : now + REPLAY_KEEP;
    if (replay.size() >= REPLAY_MAX) {
        // Evict the oldest entry only if its ticket's own window (exp + SKEW)
        // has surely passed: entries are kept at least REPLAY_KEEP, and every
        // ticket expires within MAX_LIFETIME + SKEW of being accepted.
        size_t oldest = 0;
        for (size_t i = 1; i < replay.size(); i++) if (replay[i].until < replay[oldest].until) oldest = i;
        if (replay[oldest].until - REPLAY_KEEP + MAX_LIFETIME + SKEW > now) return false;
        replay.erase(replay.begin() + oldest);
    }
    replay.push_back({nonce, until});
    if (on_replay_change) on_replay_change();
    return true;
}

static bool ticket_shape(JsonObjectConst t) {
    const char* typ = t["typ"].as<const char*>();
    if (!typ || strcmp(typ, "rook-ticket") != 0 || !is_int(t["v"]) || t["v"].as<int>() != 2) return false;
    static const char* const STR[] = {"kid", "hub", "band", "call", "nonce", "worker", "cap", "args_hash", "session", "why"};
    for (const char* k : STR) if (!nonempty_str(t[k])) return false;
    if (!is_int(t["iat"]) || !is_int(t["exp"])) return false;
    if (!is_hex(t["call"].as<const char*>(), 32) || !is_hex(t["nonce"].as<const char*>(), 32)) return false;
    std::string why = t["why"].as<const char*>();
    bool blank = true;
    for (char c : why) if (c != ' ' && c != '\t' && c != '\n' && c != '\r' && c != '\f' && c != '\v') blank = false;
    // len() in Python counts code points: count UTF-8 lead bytes.
    size_t cps = 0;
    for (unsigned char c : why) if ((c & 0xC0) != 0x80) cps++;
    if (blank || cps > 500) return false;
    if (!t["elevate"].isNull() && !t["elevate"].is<bool>()) return false;
    if (!t["contract"].isNull() && !nonempty_str(t["contract"])) return false;
    if (!t["carry_hash"].isNull() && !nonempty_str(t["carry_hash"])) return false;
    if (!t["owner"].isNull() && !(t["owner"].is<bool>() && nonempty_str(t["person"]))) return false;
    JsonVariantConst who[2] = {t["person"], t["agent"]};
    bool any = false;
    for (auto& w : who) {
        if (w.isNull()) continue;
        if (!nonempty_str(w)) return false;
        any = true;
    }
    return any;
}

static bool same_opt_str(JsonVariantConst a, JsonVariantConst b) {
    if (a.isNull() || b.isNull()) return a.isNull() && b.isNull();
    const char* x = a.as<const char*>();
    const char* y = b.as<const char*>();
    return x && y && strcmp(x, y) == 0;
}

std::string Gate::check(JsonObjectConst req, const std::string& band, int64_t now) {
    JsonVariantConst tv = req["ticket"];
    if (tv.isNull()) return "no_ticket";
    if (!tv.is<JsonObjectConst>()) return "malformed";
    JsonObjectConst t = tv.as<JsonObjectConst>();
    if (t["hubkey"].is<JsonObjectConst>()) add_hubkey(band, t["hubkey"].as<JsonObjectConst>(), now);
    if (!ticket_shape(t)) return "malformed";
    if (!anchor.set || anchor.hub != t["hub"].as<const char*>() || anchor.band != band) return "unknown_hub";
    const OpKey* k = op_key(t["kid"].as<const char*>(), now);
    if (!k) return "unknown_key";
    if (!verify_sig(k->pub, t)) return "bad_signature";
    if (band != t["band"].as<const char*>()) return "wrong_band";
    if (worker_id != t["worker"].as<const char*>()) return "wrong_worker";
    if (!same_opt_str(t["appliance"], req["appliance"])) return "wrong_appliance";
    if (!same_opt_str(t["call"], req["id"])) return "wrong_call";
    if (!same_opt_str(t["cap"], req["cap"])) return "wrong_cap";
    std::string ah;
    if (!args_hash(req["args"], ah) || ah != t["args_hash"].as<const char*>()) return "wrong_args";
    if (!req["carry"].isNull() || !t["carry_hash"].isNull()) {
        std::string ch;
        if (req["carry"].isNull() || t["carry_hash"].isNull()) return "wrong_args";
        if (!args_hash(req["carry"], ch) || ch != t["carry_hash"].as<const char*>()) return "wrong_args";
    }
    int64_t iat = t["iat"].as<long long>(), exp = t["exp"].as<long long>();
    int64_t life = exp - iat;
    if (life < MIN_LIFETIME || life > MAX_LIFETIME) return "bad_lifetime";
    if (now < iat - SKEW) return "not_yet_valid";
    if (now > exp + SKEW) return "expired";
    std::string nonce = t["nonce"].as<const char*>();
    if (seen(nonce, now)) return "replay";
    const CapMeta* meta = cap(t["cap"].as<const char*>());
    if (!meta || !t["contract"].isNull()) return "unknown_cap";   // this firmware provides no contracts
    // Home band, no memberships: the ceiling is everything (9.2); trust.set is home-only and this is home.
    if (!remember(nonce, exp, now)) return "replay";
    contact_ = now;
    return "";
}

}  // namespace rc
