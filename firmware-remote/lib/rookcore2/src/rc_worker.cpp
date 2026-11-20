#include "rc_worker.h"
#include "rc_canon.h"
#include "rc_sig.h"
#include "rc_util.h"
#include <esp_random.h>
#include <new>
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
    manifest_text_ = manifest_json;
    manifest_len_ = strlen(manifest_json);
    if (!lock_) lock_ = xSemaphoreCreateMutex();
    // Parsed without the docs, which are most of it: memory is short on the small chips.
    JsonDocument keep;
    keep["version"] = true;
    keep["caps"]["*"]["risk"] = true;
    JsonObject arg = keep["caps"]["*"]["args"]["*"].to<JsonObject>();
    for (const char* k : {"type", "required", "default", "min", "max", "items"}) arg[k] = true;
    if (deserializeJson(manifest_, manifest_text_, DeserializationOption::Filter(keep))) return false;
    manifest_.shrinkToFit();
    uint8_t d[32];
    sha256(manifest_text_, manifest_len_, d);
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

void Worker::on(const char* cap, Handler h, bool blocking, MaxTime max) {
    routes_.push_back({cap, h, blocking, max});
    if (blocking && !slow_task_) start_slow_task();
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

// The reply is put together in one allocation: a big result copied three times
// over (as it was) is more than a small chip has in one piece.
void Worker::reply_parts(const std::string& id, const char* pre, const char* body, size_t body_len, const char* post) {
    JsonDocument r;
    r["id"] = id;
    r["from"] = cfg_.worker_id;
    r["ok"] = true;
    std::string head;
    serializeJson(r, head);
    head.pop_back();                       // its closing brace: the result goes in before it
    std::string out;
    out.reserve(head.size() + strlen(pre) + body_len + strlen(post) + 12);
    out += head;
    out += ",\"result\":";
    out += pre;
    out.append(body, body_len);
    out += post;
    out += "}";
    band_.send(out);
}

void Worker::reply_raw(const std::string& id, const std::string& result_json) {
    reply_parts(id, "", result_json.data(), result_json.size(), "");
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
        if (gate.hub_announce(band_.band_hex(), m, now())) {
            if (!before || !had_contact_) {
                had_contact_ = true;
                announce_due_ = true;      // the hub may have restarted since we last announced
                batch_backoff_ = 1000;
                if (log) log("hub contact");
            } else if (millis() - last_announce_ > 120000) {
                announce_due_ = true;      // a hub back within the contact window forgot us without our noticing
            }
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

// ---- blocking calls ---------------------------------------------------------
//
// Each blocking call is a job in slow_ from when it is accepted to when its
// task is done with it. A call is answered once: by its task, or by the
// watchdog (check_slow) when it outruns its maximum, whichever is first
// (answered, under lock_). The standing task is made once, at begin, while
// memory is whole: a task made per call needs 8 KB in one piece, which a
// long-running C3's heap stops having (that was "worker error: busy").

static const uint32_t SLOW_STACK = 8192;

struct BlockingJob {
    Worker* w;
    Handler h;
    std::string id, cap;
    JsonDocument args;
    uint32_t started = 0, max_ms = 0;
    bool answered = false;     // under the worker's lock_
};

static_assert(sizeof(void*) <= sizeof(uint32_t), "jobs go to the standing task as a notification value");

void Worker::run_job(BlockingJob* j) {
    bool ok = false;
    std::string out, err;
    try {
        JsonDocument res;
        ok = j->h(j->args.as<JsonObjectConst>(), res.to<JsonObject>(), err);
        if (ok) {
            serializeJson(res, out);
            if (res.overflowed()) { ok = false; err = "worker error: out of memory for the result"; }
        }
    } catch (const std::bad_alloc&) {
        ok = false;
        err = "worker error: out of memory";
    } catch (...) {
        ok = false;
        err = "worker error: the call failed";
    }
    xSemaphoreTake(lock_, portMAX_DELAY);
    bool late = j->answered;
    j->answered = true;
    for (auto& s : slow_) if (s == j) s = nullptr;
    if (slow_cur_ == j) slow_cur_ = nullptr;     // its address may be a new job's soon
    xSemaphoreGive(lock_);
    try {
        if (late) {
            if (log) log((j->cap + " finished after the watchdog had answered it").c_str());
        } else if (ok) {
            reply_raw(j->id, out.empty() ? "{}" : out);
        } else {
            reply_err(j->id, err);
        }
    } catch (...) {
        // no memory to answer in: the caller times out, the slot is free all the same
    }
    delete j;
}

void Worker::blocking_task(void* arg) {
    BlockingJob* j = (BlockingJob*)arg;
    j->w->run_job(j);
    vTaskDelete(nullptr);
}

void Worker::slow_loop(void* arg) {
    Worker* w = (Worker*)arg;
    TaskHandle_t me = xTaskGetCurrentTaskHandle();
    for (;;) {
        uint32_t v = 0;
        if (xTaskNotifyWait(0, 0xFFFFFFFF, &v, portMAX_DELAY) != pdTRUE) continue;
        if (v) w->run_job((BlockingJob*)(uintptr_t)v);
        xSemaphoreTake(w->lock_, portMAX_DELAY);
        bool still = w->slow_task_ == me;
        if (still) {
            w->slow_cur_ = nullptr;
            w->slow_idle_ = true;
        }
        xSemaphoreGive(w->lock_);
        if (!still) vTaskDelete(nullptr);     // replaced while it overran: a new one stands
    }
}

bool Worker::start_slow_task() {
    if (!lock_) lock_ = xSemaphoreCreateMutex();
    TaskHandle_t t = nullptr;
    xSemaphoreTake(lock_, portMAX_DELAY);
    // made under the lock: it cannot look for itself in slow_task_ before it is there
    bool ok = xTaskCreate(slow_loop, "rc_slow", SLOW_STACK, this, 1, &t) == pdPASS;
    if (ok) {
        slow_task_ = t;
        slow_cur_ = nullptr;
        slow_idle_ = true;
    }
    xSemaphoreGive(lock_);
    return ok;
}

int Worker::slow_running() {
    int n = 0;
    xSemaphoreTake(lock_, portMAX_DELAY);
    for (auto* s : slow_) if (s) n++;
    xSemaphoreGive(lock_);
    return n;
}

// From loop(): answers the calls that ran past their maximum and frees their
// slots. A task stuck in one cannot be killed safely (it may hold a lock), but
// it no longer counts: the standing task is replaced and later calls run.
void Worker::check_slow() {
    struct Late { std::string id, cap; uint32_t age; };
    std::vector<Late> late;
    uint32_t ms = millis();
    bool replace = false;
    xSemaphoreTake(lock_, portMAX_DELAY);
    for (auto& s : slow_) {
        if (!s || s->answered || ms - s->started <= s->max_ms) continue;
        s->answered = true;
        late.push_back({s->id, s->cap, ms - s->started});
        if (s == slow_cur_) {
            slow_task_ = nullptr;           // it deletes itself if it ever comes back
            slow_cur_ = nullptr;
            slow_idle_ = false;
            replace = true;
        }
        s = nullptr;                        // its task still owns the job; it is freed there
        slow_abandoned_++;
    }
    bool want = !slow_task_ && (replace || ms - slow_retry_ > 5000);
    xSemaphoreGive(lock_);
    for (auto& l : late) {
        char b[160];
        snprintf(b, sizeof b, "%s ran %u s, past its maximum: answered with an error, slot freed", l.cap.c_str(),
                 (unsigned)(l.age / 1000));
        if (log) log(b);
        reply_err(l.id, "worker error: " + l.cap + " did not finish in time (" + std::to_string(l.age / 1000) +
                            " s); it was given up on");
    }
    if (want && !routes_.empty()) {
        slow_retry_ = ms;
        bool any = false;
        for (auto& r : routes_) any |= r.blocking;
        if (any && !start_slow_task() && log) log("no memory for a new slow-call task yet");
    }
}

void Worker::dispatch_slow(const Route& r, const std::string& id, const char* cap, const JsonDocument& a) {
    JsonObjectConst args = a.as<JsonObjectConst>();
    uint32_t max_ms = 120000;
    if (r.max) max_ms = r.max(args);
    else if (args["timeout"].is<long long>()) max_ms = (uint32_t)args["timeout"].as<long long>() * 1000 + 30000;
    BlockingJob* j = new (std::nothrow) BlockingJob();
    if (!j) { reply_err(id, "worker error: busy (out of memory)"); return; }
    bool ok = true;
    try {
        j->w = this;
        j->h = r.h;
        j->id = id;
        j->cap = cap;
        ok = j->args.set(a) && !j->args.overflowed();
    } catch (...) {
        ok = false;
    }
    if (!ok) { delete j; reply_err(id, "worker error: busy (out of memory)"); return; }
    j->started = millis();
    j->max_ms = max_ms;

    // Take a slot; hand it to the standing task if that is free, else a task of its own.
    xSemaphoreTake(lock_, portMAX_DELAY);
    BlockingJob** slot = nullptr;
    for (auto& s : slow_) if (!s) { slot = &s; break; }
    if (!slot) {
        xSemaphoreGive(lock_);
        delete j;
        reply_err(id, "worker error: busy (" + std::to_string(SLOW_MAX) + " slow calls running)");
        return;
    }
    *slot = j;
    TaskHandle_t standing = nullptr;
    if (slow_task_ && slow_idle_) {
        standing = slow_task_;
        slow_idle_ = false;
        slow_cur_ = j;
    }
    xSemaphoreGive(lock_);
    if (standing) {
        xTaskNotify(standing, (uint32_t)(uintptr_t)j, eSetValueWithOverwrite);
        return;
    }
    if (xTaskCreate(blocking_task, "rc_call", SLOW_STACK, j, 1, nullptr) == pdPASS) return;
    xSemaphoreTake(lock_, portMAX_DELAY);
    for (auto& s : slow_) if (s == j) s = nullptr;
    xSemaphoreGive(lock_);
    delete j;
    char b[120];
    snprintf(b, sizeof b, "worker error: busy (a slow call is running and there is no memory for another: heap %u, largest %u)",
             (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMaxAllocHeap());
    reply_err(id, b);
    if (log) log(b);
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
        reply_parts(id, "{\"manifests\":[", manifest_text_, include ? manifest_len_ : 0, "]}");
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
            dispatch_slow(r, id, cap, a);
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
    check_slow();
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
