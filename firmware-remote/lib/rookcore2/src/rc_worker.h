// A Rook core v2 worker for microcontrollers (spec docs/spec/core-v2.md): one
// home band, a builtin plugin manifest compiled in, ticket checks on every
// request, announces and heartbeats, the core caps a worker must answer, and
// plugin events to the hub as core.event batches.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <deque>
#include <functional>
#include <string>
#include <vector>
#include "rc_band.h"
#include "rc_gate.h"

namespace rc {

struct WorkerConfig {
    std::string worker_id;      // 32 hex, from enrollment
    std::string name;           // [a-z0-9-]+, not "rook"
    std::string description;
    std::string version;        // firmware build
    std::string psk, relay;     // band key, relay "host:port"
    std::string hub, root;      // home hub id and "ed25519:<root key>"
    uint8_t seed[32];           // worker key seed (spec 5.8)
};

// A cap handler fills result (a JSON object) or returns false with err set.
// Blocking handlers (that wait on a person) run on their own task.
using Handler = std::function<bool(JsonObjectConst args, JsonObject result, std::string& err)>;
// The longest a blocking call may plausibly run with these args, in ms. Past
// it the caller is answered with an error and the call no longer counts as
// running (its task, if it ever finishes, says nothing more).
using MaxTime = std::function<uint32_t(JsonObjectConst args)>;

struct BlockingJob;

class Worker {
public:
    static const char* const CORE;   // "2.0"

    // manifest_json must outlive the worker (a constant): it is served from where it is.
    bool begin(const WorkerConfig& cfg, const char* manifest_json, const char* plugin);
    // max: blocking calls only; unset, a "timeout" (s) argument + 30 s, else 120 s.
    void on(const char* cap, Handler h, bool blocking = false, MaxTime max = nullptr);
    // Queue an event this worker's plugin declares in its manifest's emits.
    void emit(const char* event, JsonObjectConst payload);
    // Fills the announce/heartbeat status object for the plugin namespace.
    std::function<void(JsonObject)> status;
    std::function<void(const char*)> log;    // one-line diagnostics

    void loop();                              // call from one task, often
    void reannounce() { announce_due_ = true; }

    bool clock_ok() const;
    bool hub_ok() const;
    const std::string& band_hex() const { return band_.band_hex(); }
    std::string pub() const;
    uint32_t calls() const { return calls_; }
    uint32_t events_sent() const { return events_sent_; }
    size_t events_queued() const { return outbox_.size(); }
    int slow_running();                       // blocking calls still counted as running
    uint32_t slow_abandoned() const { return slow_abandoned_; }   // answered by the watchdog

    Gate gate;

private:
    struct Route { std::string cap; Handler h; bool blocking; MaxTime max; };
    static const int SLOW_MAX = 6;            // blocking calls at once
    struct Event { std::string id, event, payload; };

    WorkerConfig cfg_;
    Band band_;
    std::string plugin_, manifest_hash_, manifests_hash_, entries_json_;
    const char* manifest_text_ = nullptr;   // the caller's constant, not copied: it is several KB
    size_t manifest_len_ = 0;
    JsonDocument manifest_;                 // what the worker itself needs of it: risks and argument schemas
    std::vector<Route> routes_;
    uint8_t secret_[64], pub_[32];
    uint32_t next_hb_ = 0, last_announce_ = 0, calls_ = 0, events_sent_ = 0;
    bool announce_due_ = true, had_contact_ = false;
    std::deque<Event> outbox_;
    uint32_t dropped_ = 0;
    std::string batch_id_;
    size_t batch_n_ = 0;
    uint32_t batch_sent_ = 0, batch_backoff_ = 1000;
    SemaphoreHandle_t lock_ = nullptr;
    // Blocking calls: one task made at begin, while memory is whole, takes them
    // in turn; a second at once gets a task of its own (if there is memory).
    BlockingJob* slow_[SLOW_MAX] = {};        // under lock_
    TaskHandle_t slow_task_ = nullptr;        // under lock_; null: being replaced
    BlockingJob* slow_cur_ = nullptr;         // what slow_task_ runs, under lock_
    bool slow_idle_ = false;                  // under lock_
    uint32_t slow_abandoned_ = 0, slow_retry_ = 0;

    void on_message(const uint8_t* data, size_t len);
    void on_request(JsonObjectConst req);
    void on_reply(JsonObjectConst rep);
    void reply_ok(const std::string& id, JsonVariantConst result);
    void reply_raw(const std::string& id, const std::string& result_json);
    void reply_parts(const std::string& id, const char* pre, const char* body, size_t body_len, const char* post);
    void reply_err(const std::string& id, const std::string& err);
    bool validate(const char* cap, JsonObjectConst args, JsonDocument& out, std::string& err);
    void send_announce();
    void send_heartbeat();
    void pump_events();
    void fill_status(JsonObject status);
    int64_t now() const;
    static void blocking_task(void* arg);     // one call, then gone
    static void slow_loop(void* arg);         // the standing task
    bool start_slow_task();
    void run_job(BlockingJob* j);
    void check_slow();
    void dispatch_slow(const Route& r, const std::string& id, const char* cap, const JsonDocument& args);
};

}  // namespace rc
