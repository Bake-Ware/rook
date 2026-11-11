// Ed25519 keys and signed objects (spec core-v2 3.3, 5.2, 5.8) on Monocypher.
#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string>

namespace rc {

bool parse_pub(const char* text, uint8_t out[32]);         // "ed25519:<b64>" or bare b64
std::string pub_text(const uint8_t pub[32]);                // "ed25519:<b64>"
std::string key_id(const uint8_t pub[32]);                  // first 16 hex of SHA-256(pub)

// True iff obj["sig"] verifies under pub with the prefix of obj["typ"]. Never throws.
bool verify_sig(const uint8_t pub[32], JsonObjectConst obj);

// A worker key from a 32-byte seed: secret (64 bytes, Monocypher layout) and public.
void key_from_seed(const uint8_t seed[32], uint8_t secret[64], uint8_t pub[32]);
std::string sign_b64(const uint8_t secret[64], const std::string& msg);

}  // namespace rc
