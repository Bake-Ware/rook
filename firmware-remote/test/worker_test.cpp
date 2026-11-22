// Host test of the worker and band (lib/rookcore2) as they answer core.manifests,
// the reply that broke on the C3 bridge in 0.12.1: a real Worker with the real
// manifest, a "hub" Band on the same band key, signed tickets, and a pretend
// relay (test/host/) that can lose packets. Allocations over a limit can be
// made to fail, as they do on a chip whose heap is in pieces.
//   cd firmware-remote/test && g++ -m32 -std=gnu++17 -O1 -Ihost -I../include -I../lib/rookcore2/src \
//     -I../.pio/libdeps/bridge/ArduinoJson/src worker_test.cpp host/host_net.cpp \
//     ../lib/rookcore2/src/*.c* -o /tmp/worker_test && /tmp/worker_test
// Under AddressSanitizer and UBSan: add -g -fsanitize=address,undefined -fno-sanitize-recover=undefined,
// run with ASAN_OPTIONS=detect_leaks=0.
#include <ArduinoJson.h>
#include <execinfo.h>
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
// Or: the N-th allocation fails (once), or every one from the N-th on.
int host_exempt = 0;                     // > 0: the hub's side and the pretend network, never failed
static long alloc_n = 0, fail_at = -1;
static bool fail_on = false;
void* operator new(size_t n) {
    if (!host_exempt && fail_at >= 0) {
        long k = alloc_n++;
        if (k == fail_at || (fail_on && k > fail_at)) {
            static int shown = 0;
            if (getenv("WT_BT") && shown++ < atoi(getenv("WT_BT"))) {
                void* f[24];
                int nf = backtrace(f, 24);
                fprintf(stderr, "--- allocation %ld fails:\n", k);
                backtrace_symbols_fd(f, nf, 2);
            }
            throw std::bad_alloc();
        }
    }
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
// core.event batches the hub heard: each parsed (bad: not JSON), its size and events.
struct Batch { bool bad; size_t bytes, events; long dropped; std::vector<std::string> payloads; };
static std::vector<Batch> batches;
static bool answer_events = true;          // the hub answers each batch ok
static const char* HUB_NODE = "feedfacefeedfacefeedfacefeedface";
static long long announce_seq = 0;

// A signed hub announce: the worker has hub contact and knows the hub's node.
static std::string hub_announce() {
    JsonDocument d;
    JsonObject m = d.to<JsonObject>();
    m["kind"] = "announce";
    m["typ"] = "rook-announce";
    m["v"] = 2;
    m["hub"] = hub_id;
    m["kid"] = rc::key_id(op_pub);
    m["band"] = band_hex;
    m["ts"] = (long long)time(nullptr);
    m["seq"] = ++announce_seq;
    m["worker_id"] = HUB_NODE;
    m["core"] = "2.0";
    sign(m, op_sec);
    m["hubkey"] = grant.as<JsonObjectConst>();
    std::string out;
    serializeJson(d, out);
    return out;
}

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
    w.log = [&](const char* m) {             // the app's logger (on the chip it allocates nothing): exempt
        host_exempt++;
        logs.push_back(m);
        host_exempt--;
    };
    expect(w.begin(c, REMOTE_MANIFEST_JSON, "remote"), "worker begins with the real manifest (" +
                                                           std::to_string(strlen(REMOTE_MANIFEST_JSON)) + " bytes)");
    // remote.state answers 3 KB: too big to keep for a resend
    w.on("remote.state", [](JsonObjectConst, JsonObject r, std::string&) {
        r["filler"] = std::string(3000, 'x');
        return true;
    });
    rc::Band hub;
    hub.on_message = [&hub](const uint8_t* m, size_t n) {
        size_t limit = alloc_limit;      // the hub's side is a PC: the limit is the worker's
        alloc_limit = 0;
        host_exempt++;
        std::string s((const char*)m, n);
        if (!watching.empty() && s.find("\"id\":\"" + watching + "\"") != std::string::npos &&
            s.find("\"from\"") != std::string::npos)
            got.push_back(s);
        if (s.find("\"cap\":\"core.event\"") != std::string::npos) {
            JsonDocument d;
            Batch b{(bool)deserializeJson(d, s), s.size(), 0, 0, {}};
            if (!b.bad) {
                b.dropped = d["args"]["dropped"] | 0L;
                for (JsonObjectConst e : d["args"]["events"].as<JsonArrayConst>()) {
                    std::string p;
                    serializeJson(e["payload"], p);
                    b.payloads.push_back(p);
                }
                b.events = b.payloads.size();
                if (answer_events) {
                    JsonDocument r;
                    r["id"] = d["id"];
                    r["from"] = HUB_NODE;
                    r["ok"] = true;
                    r["result"].to<JsonObject>();
                    std::string out;
                    serializeJson(r, out);
                    hub.send(out);
                }
            }
            batches.push_back(std::move(b));
        }
        alloc_limit = limit;
        host_exempt--;
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
    // ---- events (core.event): what restarted the C3 in 0.12.3 ----
    hub.send(hub_announce());
    run(w, hub);
    expect(w.hub_ok(), "events: the worker has hub contact (a signed hub announce)");
    auto voice_part = [](int seq) {
        JsonDocument p;
        p["id"] = "0a0b0c0d";
        p["seq"] = seq;
        p["data"] = std::string(2400, 'A' + seq % 26);       // base64 of 1800 bytes of audio: 2439 bytes in all
        return p;
    };
    // 10. A big payload is refused at the door, counted, and reported in the next batch.
    {
        batches.clear();
        JsonDocument p;
        p["blob"] = std::string(5000, 'x');
        bool queued = w.emit("remote.button", p.as<JsonObjectConst>());
        expect(!queued && w.events_dropped() == 1, "a 5 KB payload is refused at emit and counted");
        JsonDocument b;
        b["button"] = "ok";
        w.emit("remote.button", b.as<JsonObjectConst>());
        run(w, hub);
        expect(batches.size() == 1 && !batches[0].bad && batches[0].events == 1 && batches[0].dropped == 1,
               "the next batch carries the event and dropped 1");
    }
    // 11. Voice: 16 parts of 2439 bytes queued while the hub is away (no answers).
    {
        batches.clear();
        answer_events = false;
        for (int i = 0; i < 16; i++) w.emit("remote.voice.part", voice_part(i).as<JsonObjectConst>());
        expect(w.events_bytes() <= rc::Worker::OUTBOX_BYTES, "16 voice parts: the queue stays under " +
                                                                std::to_string(rc::Worker::OUTBOX_BYTES) + " bytes (" +
                                                                std::to_string(w.events_bytes()) + ")");
        run(w, hub);
        bool ok = !batches.empty();
        for (auto& b : batches) ok = ok && !b.bad && b.bytes <= rc::Worker::BATCH_BYTES + 1024 && b.events >= 1;
        expect(ok, "voice batches are whole JSON and at most " + std::to_string(rc::Worker::BATCH_BYTES) +
                       " bytes of payload (" + (batches.empty() ? std::string("none") : std::to_string(batches[0].bytes)) + " bytes)");
        bool intact = !batches.empty() && batches[0].payloads.size() > 0;
        if (intact) {
            JsonDocument d;
            deserializeJson(d, batches[0].payloads[0]);
            intact = std::string(d["data"] | "").size() == 2400;
        }
        expect(intact, "the payloads arrive as queued");
        answer_events = true;
    }
    // Time moves on (the worker resends an unanswered batch after 15 s, then backs off).
    auto drain = [&](int rounds) {
        for (int k = 0; k < rounds && w.events_queued(); k++) {
            host_skip(31000);
            hub.send(hub_announce());         // hub contact stays fresh
            run(w, hub, 5);
        }
    };
    // 12. The hub answers again: the batch it ignored goes again, and the rest after it; the queue empties.
    {
        batches.clear();
        size_t queued = w.events_queued();
        drain(30);
        size_t got_events = 0;
        bool ok = true;
        for (auto& b : batches) {
            got_events += b.events;
            ok = ok && !b.bad;
        }
        expect(ok && w.events_queued() == 0 && got_events >= queued,
               "the hub back: " + std::to_string(queued) + " queued events go in " + std::to_string(batches.size()) +
                   " batches and the queue empties (" + std::to_string(w.events_queued()) + " left)");
    }
    // 13. Memory short: no block over 6 KB. Batches get small, nothing escapes.
    {
        batches.clear();
        ESP.max_block = 6000;
        for (int i = 0; i < 6; i++) w.emit("remote.voice.part", voice_part(i).as<JsonObjectConst>());
        bool threw = false;
        alloc_limit = 6000;
        try {
            drain(20);
        } catch (...) {
            threw = true;
        }
        alloc_limit = 0;
        ESP.max_block = 20000;
        bool small = !batches.empty() && w.events_queued() == 0;
        for (auto& b : batches) small = small && !b.bad && b.events <= 1;
        expect(!threw, "no block over 6 KB: building batches allocates nothing that big");
        expect(small, "no block over 6 KB: one voice part a batch (" + std::to_string(batches.size()) + " batches)");
    }
    // 14. Almost no memory: emit refuses and counts; no batch, nothing escapes.
    {
        batches.clear();
        ESP.max_block = 2500;
        uint32_t before = w.events_dropped();
        bool queued = w.emit("remote.voice.part", voice_part(1).as<JsonObjectConst>());
        bool threw = false;
        alloc_limit = 2500;
        try {
            run(w, hub, 20);
        } catch (...) {
            threw = true;
        }
        alloc_limit = 0;
        ESP.max_block = 20000;
        expect(!queued && w.events_dropped() == before + 1, "no block over 2.5 KB: a voice part is dropped and counted, not queued");
        expect(!threw, "no block over 2.5 KB: nothing escapes");
    }
    // 15. Many small events: at most 100 queued, at most 50 a batch, the rest dropped and counted.
    {
        batches.clear();
        answer_events = false;
        uint32_t before = w.events_dropped();
        for (int i = 0; i < 130; i++) {
            JsonDocument b;
            b["button"] = "vol_up";
            b["seq"] = i;
            w.emit("remote.button", b.as<JsonObjectConst>());
        }
        drain(1);
        expect(w.events_queued() <= (size_t)rc::Worker::OUTBOX_MAX, "130 events: at most 100 queued (" + std::to_string(w.events_queued()) + ")");
        expect(w.events_dropped() > before, "130 events: the rest counted as dropped (" + std::to_string(w.events_dropped() - before) + ")");
        bool ok = !batches.empty();
        for (auto& b : batches) ok = ok && !b.bad && b.events <= (size_t)rc::Worker::BATCH_MAX;
        expect(ok, "130 events: batches of at most 50, whole JSON");
        answer_events = true;
        batches.clear();
        drain(20);
        expect(w.events_queued() == 0 && !batches.empty() && batches.back().dropped == 0,
               "130 events: the hub answering, the queue empties and dropped is cleared once reported");
    }
    // 16. The band's piece-wise send at and around every part boundary: a
    //     message in pieces (some empty, split anywhere) arrives byte for byte.
    {
        rc::Band a, b;
        std::vector<std::string> heard;
        b.on_message = [&heard](const uint8_t* m, size_t n) {
            size_t limit = alloc_limit;
            alloc_limit = 0;
            if (n && m[0] == '~') heard.push_back(std::string((const char*)m, n));
            alloc_limit = limit;
        };
        a.begin(PSK, "relay:7000");
        b.begin(PSK, "relay:7000");
        const size_t C = 1003;
        std::vector<size_t> sizes = {1, 2, 15, 16, 17, 979, 980, 981, 982, 986, 987, 988, C - 1, C, C + 1,
                                     2 * C - 1, 2 * C, 2 * C + 1, 3 * C + 7, 31 * C, 32 * C - 1, 32 * C};
        bool all = true;
        std::string bad;
        unsigned seed = 7;
        for (size_t sz : sizes) {
            std::string msg(sz, '\0');
            msg[0] = '~';
            for (size_t i = 1; i < sz; i++) msg[i] = (char)(' ' + (i * 31 + sz) % 90);
            for (int split = 0; split < 4; split++) {
                // cut msg into pieces: none, at every part boundary +-1, at random places, with empty ones between
                std::vector<rc::Band::Piece> ps;
                size_t at = 0;
                while (at < sz) {
                    size_t len;
                    if (split == 0) len = sz;
                    else if (split == 1) len = (at % C) == 0 ? C - 1 : 2;
                    else if (split == 2) len = 1 + (seed = seed * 1103515245 + 12345) % 1500;
                    else len = 1 + (seed = seed * 1103515245 + 12345) % 40;
                    len = std::min(len, sz - at);
                    ps.push_back({msg.data() + at, len});
                    if (split == 3) ps.push_back({nullptr, 0});
                    at += len;
                }
                heard.clear();
                a.send(ps.data(), ps.size());
                for (int r = 0; r < 4; r++) {
                    b.poll();
                    a.poll();
                }
                if (heard.size() != 1 || heard[0] != msg) {
                    all = false;
                    bad += " " + std::to_string(sz) + "/" + std::to_string(split) + "(" + std::to_string(heard.size()) + ")";
                }
            }
        }
        expect(all, "band: " + std::to_string(sizes.size() * 4) + " messages in pieces, at and around every part boundary, arrive whole" + bad);
        // the same through send(std::string), and an empty message
        heard.clear();
        a.send(std::string("~") + std::string(2 * C, 'q'));
        for (int r = 0; r < 4; r++) b.poll();
        expect(heard.size() == 1 && heard[0].size() == 2 * C + 1, "band: a 2007-byte string arrives whole");
        rc::Band::Piece none{nullptr, 0};
        a.send(&none, 1);                    // an empty message: one empty part, nothing delivered past the keepalive check
        for (int r = 0; r < 4; r++) b.poll();
        expect(true, "band: an empty message is sent without touching memory");
    }
    // 17. Fault injection over the worker's paths: the N-th allocation fails
    //     (once, then from it on) for every N, while it answers core.manifests,
    //     a 3 KB remote.state and an event batch. Nothing may escape the
    //     worker, and afterwards it must still answer (no lock left taken).
    {
        // (the test's own work, the hub's send included, is exempt: only the worker's allocations fail)
        auto scenario = [&](int which) {
            host_exempt++;
            watching = rand_hex(16);
            got.clear();
            batches.clear();
            std::string req = which == 0 ? request(watching, rand_hex(16), "core.manifests", pa)
                                         : request(watching, rand_hex(16), "remote.state", JsonObjectConst());
            JsonDocument ev;
            ev["button"] = "ok";
            if (which != 2) hub.send(req);
            host_exempt--;
            if (which == 2) {
                w.emit("remote.button", ev.as<JsonObjectConst>());
                host_skip(31000);
            }
            for (int i = 0; i < 6; i++) {
                w.loop();
                host_exempt++;
                hub.poll();
                host_exempt--;
            }
        };
        const char* names[] = {"core.manifests", "remote.state (3 KB)", "an event batch"};
        for (int which = 0; which < 3; which++) {
            fail_at = 1 << 30;                // count
            alloc_n = 0;
            scenario(which);
            long count = alloc_n;
            fail_at = -1;
            int escaped = 0, wedged = 0;
            for (int mode = 0; mode < 2; mode++) {
                for (long at = 0; at <= count + 2 && at < 400; at++) {
                    alloc_n = 0;
                    fail_at = at;
                    fail_on = mode == 1;
                    try {
                        scenario(which);
                    } catch (...) {
                        escaped++;
                    }
                    fail_at = -1;
                    fail_on = false;
                    if (host_exempt) { host_exempt = 0; escaped++; }      // a throw left the test's count raised
                    // still answering?
                    host_skip(31000);
                    hub.send(hub_announce());
                    watching = rand_hex(16);
                    got.clear();
                    hub.send(request(watching, rand_hex(16), "core.lease.drop", JsonObjectConst()));
                    run(w, hub, 6);
                    if (got.empty()) {
                        wedged++;
                        if (getenv("WT_LOG")) printf("     wedged after %s at %ld/%d: last log %s\n", names[which], at, mode,
                                                     logs.empty() ? "-" : logs.back().c_str());
                    }
                }
            }
            expect(escaped == 0 && wedged == 0, std::string("fault injection, ") + names[which] + ": " + std::to_string(count) +
                                                    " allocations, each failing in turn and from it on: nothing escapes, the worker still answers (" +
                                                    std::to_string(escaped) + " escaped, " + std::to_string(wedged) + " wedged)");
        }
    }
    // 18. No lock but the band's own is held while a packet goes out: the app's
    //     emit (the keys' loop) and the worker's other callers never wait on the network.
    expect(NET.locked_sends == 0, "over the whole run, no packet went out with the worker's lock held (" +
                                      std::to_string(NET.locked_sends) + " of " + std::to_string(NET.sent) + ")");
    // 19. emit while a batch is in flight, the queue full: the new event is the one dropped,
    //     and the batch's payloads (sent from where they lie) stay as they were.
    {
        answer_events = false;
        host_skip(31000);
        hub.send(hub_announce());
        run(w, hub, 4);
        batches.clear();
        for (int i = 0; i < 5; i++) w.emit("remote.voice.part", voice_part(i).as<JsonObjectConst>());
        run(w, hub, 4);                       // a batch goes, unanswered: in flight
        size_t queued = w.events_queued();
        uint32_t dropped = w.events_dropped();
        bool refused = !w.emit("remote.voice.part", voice_part(9).as<JsonObjectConst>());
        expect(!batches.empty() && refused && w.events_dropped() == dropped + 1 && w.events_queued() == queued,
               "a batch in flight and the queue full: emit drops the new event, the queue is left as it was");
        answer_events = true;
        drain(10);
        expect(w.events_queued() == 0, "the hub answering again, the queue empties");
    }
    // 20. emit_now: a press for the hub goes at once, alone, even with a batch in flight and backing off.
    {
        host_skip(31000);
        hub.send(hub_announce());
        run(w, hub, 4);
        answer_events = false;                // the hub ignores batches: one stays in flight, backing off
        JsonDocument b;
        b["button"] = "vol_up";
        w.emit("remote.button", b.as<JsonObjectConst>());
        run(w, hub, 4);
        batches.clear();
        uint32_t sent = w.live_sent();
        JsonDocument p;
        p["button"] = "play";
        p["via"] = "rook";
        w.emit_now("remote.button", p.as<JsonObjectConst>());
        w.loop();                             // the band task's next turn
        hub.poll();
        bool alone = batches.size() == 1 && batches[0].events == 1 && batches[0].payloads[0].find("play") != std::string::npos;
        if (getenv("WT_LOG")) {
            printf("     live sent %u (was %u), dropped %u, hub_ok %d, batches %zu:", w.live_sent(), sent, w.live_dropped(), (int)w.hub_ok(), batches.size());
            for (auto& bt : batches) printf(" [%zu events]", bt.events);
            printf("\n");
        }
        expect(w.live_sent() == sent + 1 && alone, "emit_now: sent on the next turn, alone, past a batch in flight");
        // 21. a press that sat over 2 s (on the chip: Wi-Fi down, so the band task
        //     does not run) is dropped when the band task next runs, not sent late
        uint32_t dropped = w.live_dropped();
        w.emit_now("remote.button", p.as<JsonObjectConst>());
        host_skip(2500);
        batches.clear();
        run(w, hub, 4);
        bool late = false;
        for (auto& bt : batches)
            for (auto& pl : bt.payloads) late = late || pl.find("play") != std::string::npos;
        expect(!late && w.live_dropped() == dropped + 1, "emit_now: a press that could not go within 2 s is dropped and counted, not sent late");
        answer_events = true;
        drain(10);
    }
    printf("%d passed, %d failed (%u packets sent, %u lost on purpose)\n", passes, fails, NET.sent, NET.lost);
    return fails ? 1 : 0;
}
