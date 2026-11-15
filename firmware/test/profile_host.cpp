// profile.cpp on a PC, for tests/test_firmware_profile.py: one carrier
// fragment per stdin line (hex) in, one JSON payload per stdout line out.
// "announce" and "heartbeat" lines send those. The caps are stand-ins; the
// real ones (telesthete.cpp) need the hardware.
#include "manifest.h"
#include "profile.h"

#include <chrono>
#include <iostream>
#include <string>

static const char* ping(JsonVariantConst args, JsonObject r) {
    const char* echo = args["echo"];
    if (echo) r["echo"] = echo;
    return nullptr;
}

static const char* status(JsonVariantConst, JsonObject r) {
    r["enabled"] = true;
    return nullptr;
}

static const char* refuse(JsonVariantConst, JsonObject) { return "hid_disabled"; }

static const Cap CAPS[] = {{"info.ping", ping}, {"hid.status", status}, {"hid.set", refuse}, {nullptr, nullptr}};

static void say(const JsonDocument& doc) {
    std::string s;
    serializeJson(doc, s);
    std::cout << s << std::endl;
}

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    Profile p{argv[1], argv[2], "host", ROOK_MANIFEST_PLUGIN, ROOK_MANIFEST_VERSION, ROOK_MANIFEST_HASH,
              ROOK_MANIFESTS_HASH, CAPS, {0}, 0};
    static Reassembler frags;
    reassemblerInit(frags);
    auto start = std::chrono::steady_clock::now();
    std::string line;
    while (std::getline(std::cin, line)) {
        JsonDocument out;
        if (line == "announce") {
            profileAnnounce(p, out);
            say(out);
            continue;
        }
        if (line == "heartbeat") {
            profileHeartbeat(p, out);
            say(out);
            continue;
        }
        std::string frag;
        for (size_t i = 0; i + 1 < line.size(); i += 2) frag.push_back((char)std::stoi(line.substr(i, 2), nullptr, 16));
        uint32_t now = (uint32_t)std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - start).count();
        uint8_t* msg = nullptr;
        size_t n = reassemble(frags, (const uint8_t*)frag.data(), frag.size(), now, &msg);
        if (!n) continue;
        Heard heard = profileHear(p, msg, n, now, out);
        free(msg);
        if (heard == HEARD_REPLY) say(out);
        if (heard == HEARD_NEW_HUB) {
            profileAnnounce(p, out);
            say(out);
        }
    }
    return 0;
}
