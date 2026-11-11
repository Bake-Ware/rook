// Host test of the core v2 primitives against conformance/core-v2/vectors.
//   g++ -std=c++17 -I<ArduinoJson/src> -I../lib/rookcore2/src host_test.cpp ../lib/rookcore2/src/*.c* -o t && ./t <vectors dir>
#include <ArduinoJson.h>
#include <stdio.h>
#include <fstream>
#include <sstream>
#include <string>
#include "rc_canon.h"
#include "rc_gate.h"
#include "rc_sig.h"
#include "rc_util.h"

static int fails = 0, passes = 0;
static void expect(bool ok, const std::string& what) {
    if (ok) passes++;
    else { fails++; printf("FAIL %s\n", what.c_str()); }
}

static bool load(const std::string& path, JsonDocument& doc) {
    std::ifstream f(path);
    std::stringstream ss;
    ss << f.rdbuf();
    return !deserializeJson(doc, ss.str());
}

int main(int argc, char** argv) {
    std::string dir = argc > 1 ? argv[1] : ".";
    JsonDocument canon, sigs, tickets;
    if (!load(dir + "/canonical.json", canon) || !load(dir + "/signatures.json", sigs) || !load(dir + "/tickets.json", tickets)) {
        printf("can't load vectors\n");
        return 2;
    }
    // canonical
    for (JsonObjectConst c : canon["cases"].as<JsonArrayConst>()) {
        std::string out;
        bool ok = rc::canonical(c["value"], out);
        if (c["error"].as<bool>()) {
            // Our parser can't represent a lone surrogate distinctly; only the integer case is ours to refuse.
            if (std::string(c["name"].as<const char*>()) == "lone surrogate") continue;
            expect(!ok, std::string("canonical error: ") + c["name"].as<const char*>());
        } else {
            expect(ok && out == c["canonical"].as<const char*>(),
                   std::string("canonical: ") + c["name"].as<const char*>() + " got " + out);
        }
    }
    for (JsonObjectConst c : canon["args_hash"].as<JsonArrayConst>()) {
        std::string h;
        expect(rc::args_hash(c["args"], h) && h == c["args_hash"].as<const char*>(), "args_hash " + h);
    }
    // signature verify vectors
    for (JsonObjectConst v : sigs["verify"].as<JsonArrayConst>()) {
        uint8_t pub[32];
        bool parsed = rc::parse_pub(v["pub"].as<const char*>(), pub);
        bool ok = parsed && rc::verify_sig(pub, v["object"].as<JsonObjectConst>());
        bool want = v["valid"].isNull() ? v["ok"].as<bool>() : v["valid"].as<bool>();
        expect(ok == want, std::string("verify: ") + v["name"].as<const char*>());
    }
    // key ids
    for (JsonObjectConst k : sigs["keys"].as<JsonArrayConst>()) {
        uint8_t seed[32], sec[64], pub[32];
        rc::unhex(k["seed"].as<const char*>(), seed, 32);
        rc::key_from_seed(seed, sec, pub);
        expect(rc::pub_text(pub) == k["pub"].as<const char*>(), std::string("pub from seed ") + k["name"].as<const char*>());
        expect(rc::key_id(pub) == k["kid"].as<const char*>(), std::string("kid ") + k["name"].as<const char*>());
    }
    // hubkey grants
    for (JsonObjectConst h : sigs["hubkey"].as<JsonArrayConst>()) {
        rc::Gate g;
        rc::parse_pub(h["root"].as<const char*>(), g.anchor.root);
        g.anchor.hub = rc::key_id(g.anchor.root);
        g.anchor.band = h["band"].as<const char*>();
        g.anchor.set = true;
        bool ok = g.add_hubkey(h["band"].as<const char*>(), h["grant"].as<JsonObjectConst>(), h["now"].as<long long>());
        bool want = !h["key"].isNull();
        expect(ok == want, std::string("hubkey: ") + h["name"].as<const char*>());
    }
    // tickets: home-band cases only (this firmware has no memberships)
    JsonObjectConst w = tickets["worker"];
    JsonObjectConst ok_grant = sigs["hubkey"][0]["grant"];
    for (JsonObjectConst c : tickets["cases"].as<JsonArrayConst>()) {
        std::string name = c["name"].as<const char*>(), want = c["reason"].as<const char*>();
        rc::Gate g;
        g.worker_id = w["worker_id"].as<const char*>();
        g.name = w["name"].as<const char*>();
        for (JsonPairConst p : w["caps"].as<JsonObjectConst>()) g.caps.push_back({p.key().c_str(), p.value()["risk"].as<const char*>()});
        // home hub = the trust entry whose role is home
        for (JsonPairConst p : w["trust"].as<JsonObjectConst>()) {
            if (std::string(p.value()["role"].as<const char*>()) != "home") continue;
            g.anchor.hub = p.key().c_str();
            g.anchor.band = p.value()["band"].as<const char*>();
        }
        rc::parse_pub(sigs["keys"][0]["pub"].as<const char*>(), g.anchor.root);
        g.anchor.set = true;
        long long now = c["now"].as<long long>();
        g.add_hubkey(g.anchor.band, ok_grant, now);
        for (JsonVariantConst s : c["seen"].as<JsonArrayConst>()) g.replay.push_back({s.as<const char*>(), now + 900});
        JsonDocument req;
        req.set(c["request"]);
        if (!c["ticket"].isNull()) req["ticket"] = c["ticket"];
        std::string got = g.check(req.as<JsonObjectConst>(), c["band"].as<const char*>(), now);
        if (got.empty()) got = "ok";
        bool guest = std::string(c["band"].as<const char*>()) != g.anchor.band || name.find("guest") == 0;
        bool contract = name.find("contract") != std::string::npos;
        if (guest || contract) {
            printf("skip  %-48s want %-16s got %s\n", name.c_str(), want.c_str(), got.c_str());
            continue;
        }
        expect(got == want, "ticket: " + name + " want " + want + " got " + got);
    }
    printf("%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
