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

class Worker {
public:
    static const char* const CORE;   // "2.0"

    bool begin(const WorkerConfig& cfg, const char* manifest_json, const char* plugin);
    void on(const char* cap, Handler h, bool blocking = false);
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

    Gate gate;

private:
    struct Route { std::string cap; Handler h; bool blocking; };
    struct Event { std::string id, event, payload; };

    WorkerConfig cfg_;
    Band band_;
    std::string plugin_, manifest_json_, manifest_hash_, manifests_hash_, entries_json_;
    JsonDocument manifest_;
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

    void on_message(const uint8_t* data, size_t len);
    void on_request(JsonObjectConst req);
    void on_reply(JsonObjectConst rep);
    void reply_ok(const std::string& id, JsonVariantConst result);
    void reply_raw(const std::string& id, const std::string& result_json);
    void reply_err(const std::string& id, const std::string& err);
    bool validate(const char* cap, JsonObjectConst args, JsonDocument& out, std::string& err);
    void send_announce();
    void send_heartbeat();
    void pump_events();
    void fill_status(JsonObject status);
    int64_t now() const;
    static void blocking_task(void* arg);
};

}  // namespace rc
