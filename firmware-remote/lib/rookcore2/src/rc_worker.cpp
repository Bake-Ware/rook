#include "rc_worker.h"
#include "rc_canon.h"
#include "rc_sig.h"
#include "rc_util.h"
#include <esp_random.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

namespace rc {

const char* const Worker::CORE = "2.0";

static std::string rand_hex(size_t bytes) {
    uint8_t b[32];
    esp_fill_random(b, bytes);
    return hex(b, bytes);
}

static const void* find(const uint8_t* hay, size_t n, const std::string& needle) {
    if (needle.empty() || needle.size() > n) return nullptr;
    return memmem(hay, n, needle.data(), needle.size());
}

int64_t Worker::now() const { return (int64_t)time(nullptr); }
bool Worker::clock_ok() const { return time(nullptr) > 1700000000; }
bool Worker::hub_ok() const { return clock_ok() && !gate.lost(now()); }
std::string Worker::pub() const { return pub_text(pub_); }

bool Worker::begin(const WorkerConfig& cfg, const char* manifest_json, const char* plugin) {
    cfg_ = cfg;
    plugin_ = plugin;
    manifest_json_ = manifest_json;
    lock_ = xSemaphoreCreateMutex();
    if (deserializeJson(manifest_, manifest_json_)) return false;
    uint8_t d[32];
    sha256(manifest_json_.data(), manifest_json_.size(), d);
    manifest_hash_ = hex(d, 32);
    // entries: one builtin manifest; manifests_hash = sha256(canonical(entries))
    JsonDocument entries;
    JsonObject e = entries.add<JsonObject>();
    e["plugin"] = plugin_;
    e["version"] = manifest_["version"].as<const char*>();
    e["hash"] = manifest_hash_;
    e["builtin"] = true;
    if (!canonical(entries.as<JsonVariantConst>(), entries_json_)) return false;
    sha256(entries_json_.data(), entries_json_.size(), d);
    manifests_hash_ = hex(d, 32);

    key_from_seed(cfg_.seed, secret_, pub_);
    gate.worker_id = cfg_.worker_id;
    gate.name = cfg_.name;
    gate.anchor.hub = cfg_.hub;
    gate.anchor.set = parse_pub(cfg_.root.c_str(), gate.anchor.root) && key_id(gate.anchor.root) == cfg_.hub;
    for (const char* c : {"core.manifests", "core.lease.drop", "core.trust.get", "core.trust.set"}) {
        gate.caps.push_back({c, strcmp(c, "core.trust.set") == 0 ? "admin" : "read"});
    }
    for (JsonPairConst p : manifest_["caps"].as<JsonObjectConst>()) {
        gate.caps.push_back({plugin_ + "." + p.key().c_str(), p.value()["risk"].as<const char*>()});
    }
    band_.on_message = [this](const uint8_t* m, size_t n) { on_message(m, n); };
    band_.begin(cfg_.psk, cfg_.relay);
    gate.anchor.band = band_.band_hex();
    return gate.anchor.set;
}

void Worker::on(const char* cap, Handler h, bool blocking) {
    routes_.push_back({cap, h, blocking});
}

// ---- outbound ---------------------------------------------------------------

void Worker::fill_status(JsonObject s) {
    if (status) status(s[plugin_].to<JsonObject>());
}

void Worker::send_announce() {
    JsonDocument m;
    m["kind"] = "announce";
    m["worker_id"] = cfg_.worker_id;
    m["name"] = cfg_.name;
    m["core"] = CORE;
    m["key"] = pub_text(pub_);
    m["version"] = cfg_.version;
    if (!cfg_.description.empty()) m["description"] = cfg_.description;
    JsonObject f = m["facts"].to<JsonObject>();
    f["os"] = "esp32";
    f["arch"] = "xtensa";
    f["chip"] = ESP.getChipModel();
    f["cpus"] = ESP.getChipCores();
    m["manifests"] = serialized(entries_json_);
    m["manifests_hash"] = manifests_hash_;
    m["unavailable"].to<JsonObject>();
    m["role"] = "home";
    m["ceiling"]["grants"].add("*");
    fill_status(m["status"].to<JsonObject>());
    std::string out;
    serializeJson(m, out);
    band_.send(out);
    last_announce_ = millis();
    if (log) log("announce sent");
}

void Worker::send_heartbeat() {
    JsonDocument m;
    m["kind"] = "heartbeat";
    m["worker_id"] = cfg_.worker_id;
    m["core"] = CORE;
    m["ts"] = (long long)now();
    m["manifests_hash"] = manifests_hash_;
    fill_status(m["status"].to<JsonObject>());
    std::string out;
    serializeJson(m, out);
    band_.send(out);
}

void Worker::reply_raw(const std::string& id, const std::string& result_json) {
    JsonDocument r;
    r["id"] = id;
    r["from"] = cfg_.worker_id;
    r["ok"] = true;
    r["result"] = serialized(result_json);
    std::string out;
    serializeJson(r, out);
    band_.send(out);
}

void Worker::reply_ok(const std::string& id, JsonVariantConst result) {
    std::string res;
    serializeJson(result, res);
    reply_raw(id, res.empty() ? "null" : res);
}

void Worker::reply_err(const std::string& id, const std::string& err) {
    JsonDocument r;
    r["id"] = id;
    r["from"] = cfg_.worker_id;
    r["ok"] = false;
    r["error"] = err;
    std::string out;
    serializeJson(r, out);
    band_.send(out);
}

// ---- events (core.event) ----------------------------------------------------

void Worker::emit(const char* event, JsonObjectConst payload) {
    Event e;
    e.id = rand_hex(8);
    e.event = event;
    serializeJson(payload, e.payload);
    xSemaphoreTake(lock_, portMAX_DELAY);
    if (outbox_.size() >= 100) {
        // the batch in flight stays; drop the oldest queued after it
        size_t victim = batch_id_.empty() ? 0 : batch_n_;
        if (victim < outbox_.size()) { outbox_.erase(outbox_.begin() + victim); dropped_++; }
    }
    outbox_.push_back(e);
    xSemaphoreGive(lock_);
}

void Worker::pump_events() {
    uint32_t ms = millis();
    xSemaphoreTake(lock_, portMAX_DELAY);
    if (!batch_id_.empty() && ms - batch_sent_ > 15000) {   // unanswered: resend with the same event ids
        batch_id_.clear();
        batch_sent_ = ms;
        batch_backoff_ = batch_backoff_ * 2 > 30000 ? 30000 : batch_backoff_ * 2;
    }
    bool can = batch_id_.empty() && !outbox_.empty() && !gate.hub_node().empty() && hub_ok() &&
               (batch_sent_ == 0 || ms - batch_sent_ > batch_backoff_);
    if (!can) { xSemaphoreGive(lock_); return; }
    JsonDocument req;
    batch_id_ = rand_hex(16);
    req["id"] = batch_id_;
    req["cap"] = "core.event";
    req["target"] = gate.hub_node();
    JsonObject a = req["args"].to<JsonObject>();
    a["worker_id"] = cfg_.worker_id;
    JsonArray evs = a["events"].to<JsonArray>();
    batch_n_ = 0;
    for (auto& e : outbox_) {
        if (batch_n_ >= 50) break;
        JsonObject o = evs.add<JsonObject>();
        o["id"] = e.id;
        o["event"] = e.event;
        o["plugin"] = plugin_;
        o["payload"] = serialized(e.payload);
        batch_n_++;
    }
    if (dropped_) a["dropped"] = dropped_;
    batch_sent_ = ms;
    xSemaphoreGive(lock_);
    std::string out;
    serializeJson(req, out);
    band_.send(out);
}

void Worker::on_reply(JsonObjectConst rep) {
    xSemaphoreTake(lock_, portMAX_DELAY);
    if (batch_id_.empty() || batch_id_ != rep["id"].as<const char*>()) { xSemaphoreGive(lock_); return; }
    batch_id_.clear();
    if (rep["ok"].as<bool>()) {
        for (size_t i = 0; i < batch_n_ && !outbox_.empty(); i++) outbox_.pop_front();
        events_sent_ += batch_n_;
        dropped_ = 0;
        batch_backoff_ = 1000;
        batch_sent_ = 0;
    } else {
        batch_backoff_ = batch_backoff_ * 2 > 30000 ? 30000 : batch_backoff_ * 2;
        batch_sent_ = millis();
        const char* e = rep["error"].as<const char*>();
        if (e && strstr(e, "unknown worker")) announce_due_ = true;
        if (log) log((std::string("core.event refused: ") + (rep["error"].as<const char*>() ? rep["error"].as<const char*>() : "?")).c_str());
    }
    xSemaphoreGive(lock_);
}

// ---- inbound ----------------------------------------------------------------

void Worker::on_message(const uint8_t* data, size_t len) {
    if (len < 2 || data[0] != '{') return;
    // Everything on the band reaches us; parse only what can concern us.
    bool mine = find(data, len, cfg_.worker_id) != nullptr;
    bool hub = find(data, len, "\"rook-announce\"") != nullptr;
    bool rep = false;
    xSemaphoreTake(lock_, portMAX_DELAY);
    if (!batch_id_.empty()) rep = find(data, len, batch_id_) != nullptr;
    xSemaphoreGive(lock_);
    if (!mine && !hub && !rep) return;
    JsonDocument doc;
    if (deserializeJson(doc, (const char*)data, len)) return;
    JsonObjectConst m = doc.as<JsonObjectConst>();
    if (m.isNull()) return;
    const char* cap = m["cap"].as<const char*>();
    if (cap && *cap) {
        const char* target = m["target"].as<const char*>();
        if (target && cfg_.worker_id == target) on_request(m);
        return;
    }
    if (!m["cap"].isNull()) return;
    const char* kind = m["kind"].as<const char*>();
    if (kind && strcmp(kind, "announce") == 0) {
        if (!clock_ok()) return;
        bool before = hub_ok();
        if (gate.hub_announce(band_.band_hex(), m, now()) && (!before || !had_contact_)) {
            had_contact_ = true;
            announce_due_ = true;          // the hub may have restarted since we last announced
            batch_backoff_ = 1000;
            if (log) log("hub contact");
        }
        return;
    }
    if (!kind && m["id"].is<const char*>() && !m["ok"].isNull() && !m["from"].isNull()) on_reply(m);
}

bool Worker::validate(const char* cap, JsonObjectConst args, JsonDocument& out, std::string& err) {
    out.to<JsonObject>();
    std::string full(cap);
    if (full.compare(0, plugin_.size() + 1, plugin_ + ".") != 0) {   // core caps: no schema here
        if (!args.isNull()) out.set(args);
        return true;
    }
    JsonObjectConst schema = manifest_["caps"][full.substr(plugin_.size() + 1)]["args"].as<JsonObjectConst>();
    for (JsonPairConst a : args) {
        if (schema.isNull() || schema[a.key().c_str()].isNull()) {
            err = std::string("bad args: unexpected argument '") + a.key().c_str() + "'";
            return false;
        }
    }
    for (JsonPairConst s : schema) {
        const char* k = s.key().c_str();
        JsonObjectConst d = s.value().as<JsonObjectConst>();
        JsonVariantConst v = args[k];
        if (v.isNull()) {
            if (d["required"].as<bool>()) { err = std::string("bad args: missing required argument '") + k + "'"; return false; }
            if (!d["default"].isNull()) out[k] = d["default"];
            continue;
        }
        const char* t = d["type"] | "str";
        bool ok = true;
        if (!strcmp(t, "str")) ok = v.is<const char*>();
        else if (!strcmp(t, "int")) ok = !v.is<bool>() && (v.is<long long>() || (v.is<double>() && v.as<double>() == (long long)v.as<double>()));
        else if (!strcmp(t, "float")) ok = !v.is<bool>() && v.is<double>();
        else if (!strcmp(t, "bool")) ok = v.is<bool>();
        else if (!strcmp(t, "list")) {
            ok = v.is<JsonArrayConst>();
            const char* it = d["items"]["type"] | "";
            if (ok && !strcmp(it, "str"))
                for (JsonVariantConst x : v.as<JsonArrayConst>()) if (!x.is<const char*>()) ok = false;
        } else if (!strcmp(t, "map")) ok = v.is<JsonObjectConst>();
        if (!ok) { err = std::string("bad args: '") + k + "' must be " + t; return false; }
        if (!strcmp(t, "int")) {
            long long n = v.as<long long>();
            if ((!d["min"].isNull() && n < d["min"].as<long long>()) || (!d["max"].isNull() && n > d["max"].as<long long>())) {
                err = std::string("bad args: '") + k + "' out of range";
                return false;
            }
            out[k] = n;
        } else {
            out[k] = v;
        }
    }
    return true;
}

struct BlockingJob {
    Worker* w;
    Handler h;
    std::string id;
    JsonDocument args;
    std::function<void(const std::string&, bool, const std::string&)> done;
};

void Worker::blocking_task(void* arg) {
    BlockingJob* j = (BlockingJob*)arg;
    JsonDocument res;
    std::string err;
    bool ok = j->h(j->args.as<JsonObjectConst>(), res.to<JsonObject>(), err);
    std::string out;
    if (ok) serializeJson(res, out);
    j->done(j->id, ok, ok ? out : err);
    delete j;
    vTaskDelete(nullptr);
}

void Worker::on_request(JsonObjectConst req) {
    std::string id = req["id"].as<const char*>() ? req["id"].as<const char*>() : "";
    const char* cap = req["cap"].as<const char*>();
    if (!clock_ok()) { reply_err(id, "denied by worker: not_yet_valid"); return; }
    if (!gate.hub_core().empty() && gate.hub_core().rfind("2.", 0) != 0) {
        reply_err(id, "denied by worker: incompatible");
        return;
    }
    std::string reason = gate.check(req, band_.band_hex(), now());
    if (!reason.empty()) {
        reply_err(id, reason == "unknown_cap" ? std::string("unknown capability: ") + cap : "denied by worker: " + reason);
        if (log) log(("refused " + std::string(cap) + ": " + reason).c_str());
        return;
    }
    calls_++;
    JsonObjectConst args = req["args"].as<JsonObjectConst>();
    if (!req["args"].isNull() && args.isNull()) { reply_err(id, "args must be an object"); return; }
    // core caps
    if (!strcmp(cap, "core.manifests")) {
        JsonArrayConst want = args["plugins"].as<JsonArrayConst>();
        bool include = want.isNull() || want.size() == 0;
        for (JsonVariantConst p : want) if (p.as<const char*>() && plugin_ == p.as<const char*>()) include = true;
        reply_raw(id, include ? "{\"manifests\":[" + manifest_json_ + "]}" : "{\"manifests\":[]}");
        return;
    }
    if (!strcmp(cap, "core.lease.drop")) { reply_raw(id, "{\"dropped\":0}"); return; }
    if (!strcmp(cap, "core.trust.get")) {
        JsonDocument r;
        r["anchor"]["hub"] = cfg_.hub;
        r["anchor"]["band"] = band_.band_hex();
        r["memberships"].to<JsonArray>();
        reply_ok(id, r.as<JsonVariantConst>());
        return;
    }
    if (!strcmp(cap, "core.trust.set")) {
        reply_raw(id, "{\"ok\":false,\"error\":\"this firmware joins only its home band; enroll it again to move it\"}");
        return;
    }
    for (auto& r : routes_) {
        if (r.cap != cap) continue;
        JsonDocument a;
        std::string err;
        if (!validate(cap, args, a, err)) { reply_err(id, err); return; }
        if (r.blocking) {
            BlockingJob* j = new BlockingJob{this, r.h, id, JsonDocument(), nullptr};
            j->args.set(a);
            j->done = [this](const std::string& rid, bool ok, const std::string& body) {
                if (ok) reply_raw(rid, body.empty() ? "{}" : body); else reply_err(rid, body);
            };
            if (xTaskCreate(blocking_task, "rc_call", 8192, j, 1, nullptr) != pdPASS) {
                delete j;
                reply_err(id, "worker error: busy");
            }
            return;
        }
        JsonDocument res;
        if (r.h(a.as<JsonObjectConst>(), res.to<JsonObject>(), err)) reply_ok(id, res.as<JsonVariantConst>());
        else reply_err(id, err);
        return;
    }
    reply_err(id, std::string("unknown capability: ") + cap);
}

void Worker::loop() {
    band_.poll();
    if (!band_.ready()) return;
    uint32_t ms = millis();
    if (announce_due_ || ms - last_announce_ > 300000) {
        announce_due_ = false;
        send_announce();
        next_hb_ = ms + 24000 + esp_random() % 12000;
    }
    if ((int32_t)(ms - next_hb_) >= 0 && clock_ok()) {
        next_hb_ = ms + 24000 + esp_random() % 12000;   // 30 s +-20 %
        send_heartbeat();
    }
    pump_events();
}

}  // namespace rc
