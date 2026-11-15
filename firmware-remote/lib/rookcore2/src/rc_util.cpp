#include "rc_util.h"
#include <string.h>

namespace rc {

// ---- SHA-256 (FIPS 180-4) ------------------------------------------------

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

static inline uint32_t ror(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void block(uint32_t h[8], const uint8_t* p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = hh + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

Sha256::Sha256() : len(0), fill(0) {
    static const uint32_t init[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(h, init, sizeof h);
}

void Sha256::update(const void* data, size_t n) {
    const uint8_t* p = (const uint8_t*)data;
    len += n;
    while (n) {
        size_t take = 64 - fill;
        if (take > n) take = n;
        memcpy(buf + fill, p, take);
        fill += take; p += take; n -= take;
        if (fill == 64) { block(h, buf); fill = 0; }
    }
}

void Sha256::finish(uint8_t out[32]) {
    uint64_t bits = len * 8;
    uint8_t pad = 0x80;
    update(&pad, 1);
    uint8_t zero = 0;
    while (fill != 56) update(&zero, 1);
    uint8_t L[8];
    for (int i = 0; i < 8; i++) L[i] = (uint8_t)(bits >> (56 - 8 * i));
    update(L, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = h[i] >> 24; out[4 * i + 1] = h[i] >> 16; out[4 * i + 2] = h[i] >> 8; out[4 * i + 3] = h[i];
    }
}

void sha256(const void* data, size_t n, uint8_t out[32]) {
    Sha256 s;
    s.update(data, n);
    s.finish(out);
}

void hmac_sha256(const uint8_t* key, size_t klen, const uint8_t* msg, size_t mlen, uint8_t out[32]) {
    uint8_t k[64] = {0};
    if (klen > 64) sha256(key, klen, k); else memcpy(k, key, klen);
    uint8_t ipad[64], opad[64];
    for (int i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
    uint8_t inner[32];
    Sha256 a; a.update(ipad, 64); a.update(msg, mlen); a.finish(inner);
    Sha256 b; b.update(opad, 64); b.update(inner, 32); b.finish(out);
}

// ---- hex / base64 ---------------------------------------------------------

std::string hex(const uint8_t* p, size_t n) {
    static const char* d = "0123456789abcdef";
    std::string s;
    s.reserve(n * 2);
    for (size_t i = 0; i < n; i++) { s += d[p[i] >> 4]; s += d[p[i] & 15]; }
    return s;
}

static int hv(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

bool is_hex(const std::string& s, size_t n) {
    if (s.size() != n) return false;
    for (char c : s) if (hv(c) < 0) return false;
    return true;
}

bool unhex(const std::string& s, uint8_t* out, size_t n) {
    if (!is_hex(s, n * 2)) return false;
    for (size_t i = 0; i < n; i++) out[i] = (uint8_t)(hv(s[2 * i]) << 4 | hv(s[2 * i + 1]));
    return true;
}

static std::string b64_with(const uint8_t* p, size_t n, const char* tbl, bool pad) {
    std::string s;
    s.reserve((n + 2) / 3 * 4);
    size_t i = 0;
    for (; i + 2 < n; i += 3) {
        uint32_t v = (uint32_t)p[i] << 16 | (uint32_t)p[i + 1] << 8 | p[i + 2];
        s += tbl[v >> 18]; s += tbl[(v >> 12) & 63]; s += tbl[(v >> 6) & 63]; s += tbl[v & 63];
    }
    if (i + 1 == n) {
        uint32_t v = (uint32_t)p[i] << 16;
        s += tbl[v >> 18]; s += tbl[(v >> 12) & 63];
        if (pad) s += "==";
    } else if (i + 2 == n) {
        uint32_t v = (uint32_t)p[i] << 16 | (uint32_t)p[i + 1] << 8;
        s += tbl[v >> 18]; s += tbl[(v >> 12) & 63]; s += tbl[(v >> 6) & 63];
        if (pad) s += "=";
    }
    return s;
}

std::string b64(const uint8_t* p, size_t n) {
    return b64_with(p, n, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", true);
}

std::string b64url_nopad(const uint8_t* p, size_t n) {
    return b64_with(p, n, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_", false);
}

static int b64v(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

bool unb64(const std::string& s, std::string& out) {
    out.clear();
    if (s.size() % 4) return false;
    for (size_t i = 0; i < s.size(); i += 4) {
        int v[4];
        int pads = 0;
        for (int j = 0; j < 4; j++) {
            char c = s[i + j];
            if (c == '=') {
                if (i + 4 != s.size() || j < 2) return false;
                v[j] = 0; pads++;
            } else {
                if (pads) return false;
                v[j] = b64v(c);
                if (v[j] < 0) return false;
            }
        }
        uint32_t x = (uint32_t)v[0] << 18 | (uint32_t)v[1] << 12 | (uint32_t)v[2] << 6 | v[3];
        out += (char)(x >> 16);
        if (pads < 2) out += (char)((x >> 8) & 255);
        if (pads < 1) out += (char)(x & 255);
    }
    return true;
}

}  // namespace rc
