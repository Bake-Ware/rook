#include "rc_sig.h"
#include "rc_canon.h"
#include "rc_util.h"
#include <string.h>
extern "C" {
#include "monocypher-ed25519.h"
}

namespace rc {

bool parse_pub(const char* text, uint8_t out[32]) {
    if (!text) return false;
    const char* b = strncmp(text, "ed25519:", 8) == 0 ? text + 8 : text;
    std::string raw;
    if (!unb64(b, raw) || raw.size() != 32) return false;
    memcpy(out, raw.data(), 32);
    return true;
}

std::string pub_text(const uint8_t pub[32]) { return "ed25519:" + b64(pub, 32); }

std::string key_id(const uint8_t pub[32]) {
    uint8_t d[32];
    sha256(pub, 32, d);
    return hex(d, 8);
}

bool verify_sig(const uint8_t pub[32], JsonObjectConst obj) {
    const char* sig = obj["sig"].as<const char*>();
    if (!sig) return false;
    std::string raw;
    if (!unb64(sig, raw) || raw.size() != 64) return false;
    std::string msg;
    if (!signing_input(obj, msg)) return false;
    return crypto_ed25519_check((const uint8_t*)raw.data(), pub, (const uint8_t*)msg.data(), msg.size()) == 0;
}

void key_from_seed(const uint8_t seed[32], uint8_t secret[64], uint8_t pub[32]) {
    uint8_t s[32];
    memcpy(s, seed, 32);            // Monocypher wipes the seed it is given
    crypto_ed25519_key_pair(secret, pub, s);
}

std::string sign_b64(const uint8_t secret[64], const std::string& msg) {
    uint8_t sig[64];
    crypto_ed25519_sign(sig, secret, (const uint8_t*)msg.data(), msg.size());
    return b64(sig, 64);
}

}  // namespace rc
