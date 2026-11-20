// Host test of the worker and band (lib/rookcore2) as they answer core.manifests,
// the reply that broke on the C3 bridge in 0.12.1: a real Worker with the real
// manifest, a "hub" Band on the same band key, signed tickets, and a pretend
// relay (test/host/) that can lose packets. Allocations over a limit can be
// made to fail, as they do on a chip whose heap is in pieces.
//   cd firmware-remote/test && g++ -m32 -std=gnu++17 -O1 -Ihost -I../include -I../lib/rookcore2/src \
//     -I../.pio/libdeps/bridge/ArduinoJson/src worker_test.cpp host/host_net.cpp \
//     ../lib/rookcore2/src/*.c* -o /tmp/worker_test && /tmp/worker_test
#include <ArduinoJson.h>
#include <stdio.h>
#include <new>
#include <string>
#include <vector>
#include "WiFiUdp.h"
#include "esp_random.h"
#include "rc_band.h"
#include "rc_canon.h"
#include "rc_sig.h"
#include "rc_util.h"
#include "rc_worker.h"
#include "remote_manifest.h"

// ---- allocations that fail past a size, as on a fragmented heap ----------------

size_t alloc_limit = 0;                  // 0: no limit (host/host_net.cpp reads it too)
static size_t alloc_refused = 0;
void* operator new(size_t n) {
    if (alloc_limit && n > alloc_limit) {
        alloc_refused++;
        throw std::bad_alloc();
    }
    void* p = malloc(n ? n : 1);
    if (!p) throw std::bad_alloc();
    return p;
}
void operator delete(void* p) noexcept { free(p); }
void operator delete(void* p, size_t) noexcept { free(p); }

static int fails = 0, passes = 0;
static void expect(bool ok, const std::string& what) {
    if (ok) passes++;
    else fails++;
    printf("%s %s\n", ok ? "ok  " : "FAIL", what.c_str());
}

// ---- a hub's keys and tickets ----------------------------------------------------

static const char* PSK = "worker-test-band-key";
static uint8_t root_sec[64], root_pub[32], op_sec[64], op_pub[32];
static std::string hub_id, band_hex, worker_id = "0123456789abcdef0123456789abcdef";
static JsonDocument grant;

static std::string rand_hex(size_t n) {
    uint8_t b[32];
    esp_fill_random(b, n);
    return rc::hex(b, n);
}

static void sign(JsonObject o, const uint8_t sec[64]) {
    std::string in;
    rc::signing_input(o, in);
    o["sig"] = rc::sign_b64(sec, in);
}

static void make_keys() {
    uint8_t s1[32], s2[32];
    for (int i = 0; i < 32; i++) s1[i] = (uint8_t)(i + 1), s2[i] = (uint8_t)(100 + i);
    rc::key_from_seed(s1, root_sec, root_pub);
    rc::key_from_seed(s2, op_sec, op_pub);
    hub_id = rc::key_id(root_pub);
    uint8_t h[32];
    rc::sha256(PSK, strlen(PSK), h);
    band_hex = rc::hex(h, 16);
    long long now = time(nullptr);
    JsonObject g = grant.to<JsonObject>();
    g["typ"] = "rook-hubkey";
    g["v"] = 2;
    g["hub"] = hub_id;
    g["kid"] = hub_id;
    g["key"] = rc::pub_text(op_pub);
    g["iat"] = now - 10;
    g["exp"] = now + 86400;
    g["bands"].add(band_hex);
    sign(g, root_sec);
}

// A request as the hub sends it: id, cap, args, a ticket signed by its op key.
static std::string request(const std::string& id, const std::string& nonce, const char* cap, JsonObjectConst args) {
    JsonDocument d;
    d["id"] = id;
    d["cap"] = cap;
    d["target"] = worker_id;
    d["args"] = args;
    d["accept"].add("drop");
    d["accept"].add("ack");
    JsonObject t = d["ticket"].to<JsonObject>();
    long long now = time(nullptr);
    t["typ"] = "rook-ticket";
    t["v"] = 2;
    t["kid"] = rc::key_id(op_pub);
    t["hub"] = hub_id;
    t["band"] = band_hex;
    t["call"] = id;
    t["nonce"] = nonce;
    t["worker"] = worker_id;
    t["cap"] = cap;
    std::string ah;
    rc::args_hash(d["args"], ah);
    t["args_hash"] = ah;
    t["session"] = "test";
    t["why"] = "host test";
    t["person"] = "tester";
    t["iat"] = now;
    t["exp"] = now + 90;
    sign(t, op_sec);
    t["hubkey"] = grant.as<JsonObjectConst>();
    std::string out;
    serializeJson(d, out);
    return out;
}

// ---- the band between them ------------------------------------------------------

static std::vector<std::string> got;      // replies the hub heard to the call watched
static std::string watching;

static void run(rc::Worker& w, rc::Band& hub, int rounds = 20) {
    for (int i = 0; i < rounds; i++) {
        w.loop();
        hub.poll();
    }
}

static bool full_manifest(const std::string& reply) {
    JsonDocument d;
    if (deserializeJson(d, reply)) return false;
    if (!d["ok"].as<bool>() || d["id"] != watching) return false;
    JsonVariantConst m = d["result"]["manifests"][0];
    if (m.isNull()) return false;
    std::string again;
    serializeJson(m, again);
    JsonDocument want;
    deserializeJson(want, REMOTE_MANIFEST_JSON);
    std::string w;
    serializeJson(want, w);
    return again == w;
}

static std::string error_of(const std::string& reply) {
    JsonDocument d;
    if (deserializeJson(d, reply) || d["ok"].as<bool>()) return "";
    return d["error"] | "";
}

int main() {
    make_keys();
    rc::Worker w;
    rc::WorkerConfig c;
    c.worker_id = worker_id;
    c.name = "bridge-test";
    c.version = "test";
    c.psk = PSK;
    c.relay = "relay:7000";
    c.hub = hub_id;
    c.root = rc::pub_text(root_pub);
    for (int i = 0; i < 32; i++) c.seed[i] = (uint8_t)(200 + i);
    std::vector<std::string> logs;
    w.log = [&](const char* m) { logs.push_back(m); };
    expect(w.begin(c, REMOTE_MANIFEST_JSON, "remote"), "worker begins with the real manifest (" +
                                                           std::to_string(strlen(REMOTE_MANIFEST_JSON)) + " bytes)");
    // remote.state answers 3 KB: too big to keep for a resend
    w.on("remote.state", [](JsonObjectConst, JsonObject r, std::string&) {
        r["filler"] = std::string(3000, 'x');
        return true;
    });
    rc::Band hub;
    hub.on_message = [](const uint8_t* m, size_t n) {
        size_t limit = alloc_limit;      // the hub's side is a PC: the limit is the worker's
        alloc_limit = 0;
        std::string s((const char*)m, n);
        if (!watching.empty() && s.find("\"id\":\"" + watching + "\"") != std::string::npos &&
            s.find("\"from\"") != std::string::npos)
            got.push_back(s);
        alloc_limit = limit;
    };
    hub.begin(PSK, "relay:7000");
    run(w, hub);

    JsonDocument plugins;
    plugins["plugins"].add("remote");
    JsonObjectConst pa = plugins.as<JsonObjectConst>();

    // 1. Asked once: one reply, the whole manifest.
    {
        watching = rand_hex(16);
        got.clear();
        hub.send(request(watching, rand_hex(16), "core.manifests", pa));
        run(w, hub);
        expect(got.size() == 1 && full_manifest(got[0]), "core.manifests: one reply, the whole manifest");
    }
    // 2. The reply lost on the way (every part of it), the hub sends the same
    //    request again: the worker sends the manifest again, whole.
    {
        watching = rand_hex(16);
        std::string nonce = rand_hex(16), req = request(watching, nonce, "core.manifests", pa);
        got.clear();
        static const WiFiUDP* hub_sock = nullptr;
        NET.drop = [](const WiFiUDP* from, const std::string&) { return from != NET.socks.back(); };   // the worker's packets
        hub_sock = NET.socks.back();
        (void)hub_sock;
        hub.send(req);
        run(w, hub);
        NET.drop = nullptr;
        expect(got.empty(), "reply lost: nothing reached the hub");
        hub.send(req);                     // the hub's resend: same id, same nonce
        run(w, hub);
        expect(got.size() == 1 && full_manifest(got[0]), "resent request: the manifest comes again, whole, once");
        hub.send(req);                     // and again (crossing the reply): answered again, not refused
        run(w, hub);
        expect(got.size() == 2 && full_manifest(got[1]), "a second resend: answered again (not 'replay', not silence)");
    }
    // 3. One part of the reply lost: the resend brings it whole.
    {
        watching = rand_hex(16);
        std::string req = request(watching, rand_hex(16), "core.manifests", pa);
        got.clear();
        static int n = 0;
        n = 0;
        NET.drop = [](const WiFiUDP* from, const std::string&) { return from != NET.socks.back() && ++n == 7; };
        hub.send(req);
        run(w, hub);
        NET.drop = nullptr;
        hub.send(req);
        run(w, hub);
        expect(got.size() == 1 && full_manifest(got[0]), "one part lost, then resent: one whole manifest arrives");
    }
    // 4. No piece of memory bigger than 4 KB to be had (the C3's heap in pieces):
    //    the manifest still goes, nothing aborts.
    {
        watching = rand_hex(16);
        got.clear();
        std::string req = request(watching, rand_hex(16), "core.manifests", pa);
        alloc_limit = 4096;
        alloc_refused = 0;
        bool threw = false;
        try {
            hub.send(req);
            run(w, hub);
        } catch (...) {
            threw = true;
        }
        alloc_limit = 0;
        expect(!threw, "with no 4 KB block free: nothing escapes the worker (on the chip: an abort and a restart)");
        expect(got.size() == 1 && full_manifest(got[0]), "with no 4 KB block free: the manifest is answered whole");
    }
    // 5. With no 1 KB block free a reply can't be made: an error comes back, not silence.
    {
        watching = rand_hex(16);
        got.clear();
        std::string req = request(watching, rand_hex(16), "remote.state", JsonObjectConst());
        bool threw = false;
        alloc_limit = 1024;
        try {
            hub.send(req);
            run(w, hub);
        } catch (...) {
            threw = true;
        }
        alloc_limit = 0;
        expect(!threw, "with no 1 KB block free: nothing escapes the worker");
        expect(got.size() == 1 && error_of(got[0]).find("out of memory") != std::string::npos,
               "with no 1 KB block free: answered 'out of memory' (" + (got.empty() ? std::string("nothing") : error_of(got[0])) + ")");
    }
    // 6. A 3 KB reply (too big to keep) lost: the resend is told so at once.
    {
        watching = rand_hex(16);
        std::string req = request(watching, rand_hex(16), "remote.state", JsonObjectConst());
        got.clear();
        NET.drop = [](const WiFiUDP* from, const std::string&) { return from != NET.socks.back(); };
        hub.send(req);
        run(w, hub);
        NET.drop = nullptr;
        hub.send(req);
        run(w, hub);
        expect(got.size() == 1 && error_of(got[0]).find("lost on the way") != std::string::npos,
               "an unkept reply lost: the resend is answered with an error, not silence");
    }
    // 7. A small reply lost: the resend gets the kept reply.
    {
        watching = rand_hex(16);
        std::string req = request(watching, rand_hex(16), "core.lease.drop", JsonObjectConst());
        got.clear();
        NET.drop = [](const WiFiUDP* from, const std::string&) { return from != NET.socks.back(); };
        hub.send(req);
        run(w, hub);
        NET.drop = nullptr;
        hub.send(req);
        run(w, hub);
        expect(got.size() == 1 && got[0].find("\"dropped\":0") != std::string::npos, "a small reply lost: the kept one goes again");
    }
    // 8. Memory short: kept replies are not kept.
    {
        ESP.free_heap = 15000;
        watching = rand_hex(16);
        std::string req = request(watching, rand_hex(16), "core.lease.drop", JsonObjectConst());
        got.clear();
        NET.drop = [](const WiFiUDP* from, const std::string&) { return from != NET.socks.back(); };
        hub.send(req);
        run(w, hub);
        NET.drop = nullptr;
        hub.send(req);
        run(w, hub);
        ESP.free_heap = 40000;
        expect(got.size() == 1 && error_of(got[0]).find("not kept") != std::string::npos,
               "memory short: nothing kept, the resend is told the reply is lost");
    }
    // 9. A new ticket (new nonce) for an id seen: a new call, answered.
    {
        watching = rand_hex(16);
        got.clear();
        hub.send(request(watching, rand_hex(16), "core.manifests", pa));
        run(w, hub);
        hub.send(request(watching, rand_hex(16), "core.manifests", pa));
        run(w, hub);
        expect(got.size() == 2 && full_manifest(got[1]), "a re-minted ticket for the same id: answered as a new call");
    }
    printf("%d passed, %d failed (%u packets sent, %u lost on purpose)\n", passes, fails, NET.sent, NET.lost);
    return fails ? 1 : 0;
}
