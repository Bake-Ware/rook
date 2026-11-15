// Small portable helpers for the Rook core v2 firmware library: SHA-256,
// HMAC/HKDF-SHA256, base64 (standard and url-safe), hex. No Arduino types, so
// the same code runs in the host tests (test/).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>

namespace rc {

struct Sha256 {
    uint32_t h[8];
    uint64_t len;
    uint8_t buf[64];
    size_t fill;
    Sha256();
    void update(const void* data, size_t n);
    void finish(uint8_t out[32]);
};

void sha256(const void* data, size_t n, uint8_t out[32]);
void hmac_sha256(const uint8_t* key, size_t klen, const uint8_t* msg, size_t mlen, uint8_t out[32]);

std::string hex(const uint8_t* p, size_t n);
bool unhex(const std::string& s, uint8_t* out, size_t n);
bool is_hex(const std::string& s, size_t n);   // exactly n lowercase hex chars

std::string b64(const uint8_t* p, size_t n);                 // standard, padded
std::string b64url_nopad(const uint8_t* p, size_t n);
bool unb64(const std::string& s, std::string& out);          // standard, padded, strict

}  // namespace rc
