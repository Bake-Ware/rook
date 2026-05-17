// Telesthete band worker — UDP, XSalsa20-Poly1305 AEAD (NaCl secretbox
// construction, wire-compatible with PyNaCl's nacl.secret.SecretBox), 27-byte
// header + 21-byte fragmentation envelope per SPEC §6.4. We only emit
// single-fragment messages (all our replies fit comfortably under 1003 B
// plaintext); inbound multi-fragment frames are dropped with a debug log.
//
// Crypto stack:
//     - SHA-256 (mbedtls) for band_id derivation
//     - HMAC-SHA-256 (mbedtls) for HKDF key derivation
//     - Salsa20 core in plain C (column + row rounds, 10 double-rounds)
//     - HSalsa20 for the XSalsa20 subkey
//     - Poly1305 (mbedtls) for the MAC
//
// XSalsa20-Poly1305 (NaCl) wire shape per frame:
//     ciphertext_wire = tag(16) || ciphertext_xor
// where ciphertext_xor = plaintext XOR keystream[32..]; the first 32 bytes
// of the XSalsa20 keystream are the one-time Poly1305 key.

#include "telesthete.h"
#include "config.h"
#include "settings.h"
#include "hid.h"
#include "device_mode.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>
#include <esp_random.h>
#include <esp_system.h>
#include <mbedtls/sha256.h>
#include <mbedtls/md.h>
#include <string.h>

// ---- module state ------------------------------------------------------

static WiFiUDP g_udp;
static IPAddress g_hub_ip;
static uint16_t g_hub_port = 0;
static uint8_t g_band_id[16];
static uint8_t g_aead_key[32];
static uint64_t g_seq = 0;
static volatile uint32_t g_last_recv_ms = 0;
static String g_worker_id;
static String g_worker_name;
static String g_hub_host_cached;
static bool g_inited = false;

// Forward decls for endian helpers defined below in the Salsa20 section.
static inline uint32_t load32_le(const uint8_t* s);
static inline void store32_le(uint8_t* d, uint32_t v);

// ---- Poly1305 (poly1305-donna-32 adapted, public domain) ---------------

static void poly1305_mac(const uint8_t key[32],
                         const uint8_t* msg, size_t len,
                         uint8_t out_tag[16]) {
    uint32_t r0, r1, r2, r3, r4;
    uint32_t s1, s2, s3, s4;
    uint32_t h0, h1, h2, h3, h4;
    uint32_t pad0, pad1, pad2, pad3;
    uint64_t d0, d1, d2, d3, d4;
    uint32_t c;

    // r = clamp(key[0..16]) in 5 26-bit limbs
    r0 = (((uint32_t)key[ 0])      | ((uint32_t)key[ 1] <<  8) | ((uint32_t)key[ 2] << 16) | ((uint32_t)key[ 3] << 24))       & 0x03ffffff;
    r1 = (((uint32_t)key[ 3] >> 2) | ((uint32_t)key[ 4] <<  6) | ((uint32_t)key[ 5] << 14) | ((uint32_t)key[ 6] << 22))       & 0x03ffff03;
    r2 = (((uint32_t)key[ 6] >> 4) | ((uint32_t)key[ 7] <<  4) | ((uint32_t)key[ 8] << 12) | ((uint32_t)key[ 9] << 20))       & 0x03ffc0ff;
    r3 = (((uint32_t)key[ 9] >> 6) | ((uint32_t)key[10] <<  2) | ((uint32_t)key[11] << 10) | ((uint32_t)key[12] << 18))       & 0x03f03fff;
    r4 = ((uint32_t)key[13]        | ((uint32_t)key[14] <<  8) | ((uint32_t)key[15] << 16))                                    & 0x000fffff;

    s1 = r1 * 5; s2 = r2 * 5; s3 = r3 * 5; s4 = r4 * 5;
    h0 = h1 = h2 = h3 = h4 = 0;

    pad0 = (uint32_t)key[16] | ((uint32_t)key[17] << 8) | ((uint32_t)key[18] << 16) | ((uint32_t)key[19] << 24);
    pad1 = (uint32_t)key[20] | ((uint32_t)key[21] << 8) | ((uint32_t)key[22] << 16) | ((uint32_t)key[23] << 24);
    pad2 = (uint32_t)key[24] | ((uint32_t)key[25] << 8) | ((uint32_t)key[26] << 16) | ((uint32_t)key[27] << 24);
    pad3 = (uint32_t)key[28] | ((uint32_t)key[29] << 8) | ((uint32_t)key[30] << 16) | ((uint32_t)key[31] << 24);

    uint8_t buf[16];
    while (len > 0) {
        size_t n;
        uint32_t hi_bit;
        if (len >= 16) {
            memcpy(buf, msg, 16);
            n = 16;
            hi_bit = 1u << 24;
        } else {
            memset(buf, 0, 16);
            memcpy(buf, msg, len);
            buf[len] = 1;
            n = len;
            hi_bit = 0;
        }
        msg += n;
        len -= n;

        h0 += ((((uint32_t)buf[ 0])      | ((uint32_t)buf[ 1] <<  8) | ((uint32_t)buf[ 2] << 16) | ((uint32_t)buf[ 3] << 24)) & 0x03ffffff);
        h1 += ((((uint32_t)buf[ 3] >> 2) | ((uint32_t)buf[ 4] <<  6) | ((uint32_t)buf[ 5] << 14) | ((uint32_t)buf[ 6] << 22)) & 0x03ffffff);
        h2 += ((((uint32_t)buf[ 6] >> 4) | ((uint32_t)buf[ 7] <<  4) | ((uint32_t)buf[ 8] << 12) | ((uint32_t)buf[ 9] << 20)) & 0x03ffffff);
        h3 += ((((uint32_t)buf[ 9] >> 6) | ((uint32_t)buf[10] <<  2) | ((uint32_t)buf[11] << 10) | ((uint32_t)buf[12] << 18)) & 0x03ffffff);
        h4 += ((uint32_t)buf[13] | ((uint32_t)buf[14] <<  8) | ((uint32_t)buf[15] << 16)) | hi_bit;

        d0 = ((uint64_t)h0 * r0) + ((uint64_t)h1 * s4) + ((uint64_t)h2 * s3) + ((uint64_t)h3 * s2) + ((uint64_t)h4 * s1);
        d1 = ((uint64_t)h0 * r1) + ((uint64_t)h1 * r0) + ((uint64_t)h2 * s4) + ((uint64_t)h3 * s3) + ((uint64_t)h4 * s2);
        d2 = ((uint64_t)h0 * r2) + ((uint64_t)h1 * r1) + ((uint64_t)h2 * r0) + ((uint64_t)h3 * s4) + ((uint64_t)h4 * s3);
        d3 = ((uint64_t)h0 * r3) + ((uint64_t)h1 * r2) + ((uint64_t)h2 * r1) + ((uint64_t)h3 * r0) + ((uint64_t)h4 * s4);
        d4 = ((uint64_t)h0 * r4) + ((uint64_t)h1 * r3) + ((uint64_t)h2 * r2) + ((uint64_t)h3 * r1) + ((uint64_t)h4 * r0);

                       c = (uint32_t)(d0 >> 26); h0 = (uint32_t)d0 & 0x3ffffff;
        d1 += c;       c = (uint32_t)(d1 >> 26); h1 = (uint32_t)d1 & 0x3ffffff;
        d2 += c;       c = (uint32_t)(d2 >> 26); h2 = (uint32_t)d2 & 0x3ffffff;
        d3 += c;       c = (uint32_t)(d3 >> 26); h3 = (uint32_t)d3 & 0x3ffffff;
        d4 += c;       c = (uint32_t)(d4 >> 26); h4 = (uint32_t)d4 & 0x3ffffff;
        h0 += c * 5;   c = h0 >> 26;             h0 = h0 & 0x3ffffff;
        h1 += c;
    }

    // full carry pass
    c = h1 >> 26;              h1 &= 0x3ffffff;
    h2 += c; c = h2 >> 26;     h2 &= 0x3ffffff;
    h3 += c; c = h3 >> 26;     h3 &= 0x3ffffff;
    h4 += c; c = h4 >> 26;     h4 &= 0x3ffffff;
    h0 += c * 5; c = h0 >> 26; h0 &= 0x3ffffff;
    h1 += c;

    // h - p
    uint32_t g0 = h0 + 5; c = g0 >> 26; g0 &= 0x3ffffff;
    uint32_t g1 = h1 + c; c = g1 >> 26; g1 &= 0x3ffffff;
    uint32_t g2 = h2 + c; c = g2 >> 26; g2 &= 0x3ffffff;
    uint32_t g3 = h3 + c; c = g3 >> 26; g3 &= 0x3ffffff;
    uint32_t g4 = h4 + c - (1u << 26);

    // mask = 0 if h<p, 0xffffffff if h>=p
    uint32_t mask = (g4 >> 31) - 1;
    g0 &= mask; g1 &= mask; g2 &= mask; g3 &= mask; g4 &= mask;
    mask = ~mask;
    h0 = (h0 & mask) | g0;
    h1 = (h1 & mask) | g1;
    h2 = (h2 & mask) | g2;
    h3 = (h3 & mask) | g3;
    h4 = (h4 & mask) | g4;

    // pack 26-bit limbs -> 32-bit limbs
    h0 = ((h0      ) | (h1 << 26));
    h1 = ((h1 >>  6) | (h2 << 20));
    h2 = ((h2 >> 12) | (h3 << 14));
    h3 = ((h3 >> 18) | (h4 <<  8));

    // tag = (h + s) mod 2^128
    uint64_t f;
    f = (uint64_t)h0 + pad0;             h0 = (uint32_t)f;
    f = (uint64_t)h1 + pad1 + (f >> 32); h1 = (uint32_t)f;
    f = (uint64_t)h2 + pad2 + (f >> 32); h2 = (uint32_t)f;
    f = (uint64_t)h3 + pad3 + (f >> 32); h3 = (uint32_t)f;

    store32_le(out_tag +  0, h0);
    store32_le(out_tag +  4, h1);
    store32_le(out_tag +  8, h2);
    store32_le(out_tag + 12, h3);
}

// ---- Salsa20 / HSalsa20 ------------------------------------------------

static inline uint32_t load32_le(const uint8_t *s) {
    return (uint32_t)s[0]
         | ((uint32_t)s[1] << 8)
         | ((uint32_t)s[2] << 16)
         | ((uint32_t)s[3] << 24);
}
static inline void store32_le(uint8_t *d, uint32_t v) {
    d[0] = (uint8_t)v;
    d[1] = (uint8_t)(v >> 8);
    d[2] = (uint8_t)(v >> 16);
    d[3] = (uint8_t)(v >> 24);
}
#define ROTL32(v, n) (((v) << (n)) | ((v) >> (32 - (n))))

// Salsa20 quarter-round (NOT ChaCha20's). 4 ops, single statement-expr.
#define QR_S(a, b, c, d) do { \
    b ^= ROTL32((uint32_t)((a) + (d)),  7); \
    c ^= ROTL32((uint32_t)((b) + (a)),  9); \
    d ^= ROTL32((uint32_t)((c) + (b)), 13); \
    a ^= ROTL32((uint32_t)((d) + (c)), 18); \
} while (0)

static void salsa20_block(uint8_t out[64], const uint32_t in_[16]) {
    uint32_t x[16];
    memcpy(x, in_, 64);
    for (int i = 0; i < 10; i++) {
        // column round
        QR_S(x[0],  x[4],  x[8],  x[12]);
        QR_S(x[5],  x[9],  x[13], x[1]);
        QR_S(x[10], x[14], x[2],  x[6]);
        QR_S(x[15], x[3],  x[7],  x[11]);
        // row round
        QR_S(x[0],  x[1],  x[2],  x[3]);
        QR_S(x[5],  x[6],  x[7],  x[4]);
        QR_S(x[10], x[11], x[8],  x[9]);
        QR_S(x[15], x[12], x[13], x[14]);
    }
    for (int i = 0; i < 16; i++) {
        store32_le(out + i * 4, x[i] + in_[i]);
    }
}

// HSalsa20 = same rounds, no input add, special output word selection.
static void hsalsa20(uint8_t out[32], const uint8_t key[32], const uint8_t nonce16[16]) {
    static const uint32_t sigma[4] = {
        0x61707865u, 0x3320646eu, 0x79622d32u, 0x6b206574u
    };
    uint32_t x[16];
    x[0]  = sigma[0];
    x[1]  = load32_le(key + 0);
    x[2]  = load32_le(key + 4);
    x[3]  = load32_le(key + 8);
    x[4]  = load32_le(key + 12);
    x[5]  = sigma[1];
    x[6]  = load32_le(nonce16 + 0);
    x[7]  = load32_le(nonce16 + 4);
    x[8]  = load32_le(nonce16 + 8);
    x[9]  = load32_le(nonce16 + 12);
    x[10] = sigma[2];
    x[11] = load32_le(key + 16);
    x[12] = load32_le(key + 20);
    x[13] = load32_le(key + 24);
    x[14] = load32_le(key + 28);
    x[15] = sigma[3];

    for (int i = 0; i < 10; i++) {
        QR_S(x[0],  x[4],  x[8],  x[12]);
        QR_S(x[5],  x[9],  x[13], x[1]);
        QR_S(x[10], x[14], x[2],  x[6]);
        QR_S(x[15], x[3],  x[7],  x[11]);
        QR_S(x[0],  x[1],  x[2],  x[3]);
        QR_S(x[5],  x[6],  x[7],  x[4]);
        QR_S(x[10], x[11], x[8],  x[9]);
        QR_S(x[15], x[12], x[13], x[14]);
    }
    store32_le(out +  0, x[0]);
    store32_le(out +  4, x[5]);
    store32_le(out +  8, x[10]);
    store32_le(out + 12, x[15]);
    store32_le(out + 16, x[6]);
    store32_le(out + 20, x[7]);
    store32_le(out + 24, x[8]);
    store32_le(out + 28, x[9]);
}

// Generate `nblocks` of XSalsa20 keystream into `out` (out must hold nblocks*64 bytes).
static void xsalsa20_stream(uint8_t* out, size_t nblocks,
                            const uint8_t key[32], const uint8_t nonce24[24]) {
    uint8_t subkey[32];
    hsalsa20(subkey, key, nonce24);

    static const uint32_t sigma[4] = {
        0x61707865u, 0x3320646eu, 0x79622d32u, 0x6b206574u
    };
    uint32_t st[16];
    st[0]  = sigma[0];
    st[1]  = load32_le(subkey + 0);
    st[2]  = load32_le(subkey + 4);
    st[3]  = load32_le(subkey + 8);
    st[4]  = load32_le(subkey + 12);
    st[5]  = sigma[1];
    st[6]  = load32_le(nonce24 + 16);
    st[7]  = load32_le(nonce24 + 20);
    // st[8], st[9] = block counter, set per-block
    st[10] = sigma[2];
    st[11] = load32_le(subkey + 16);
    st[12] = load32_le(subkey + 20);
    st[13] = load32_le(subkey + 24);
    st[14] = load32_le(subkey + 28);
    st[15] = sigma[3];

    // 32-bit block counter — plaintexts on this device fit well under 2^31 blocks.
    for (size_t b = 0; b < nblocks; b++) {
        st[8] = (uint32_t)b;
        st[9] = 0;
        salsa20_block(out + b * 64, st);
    }
}

// ---- XSalsa20-Poly1305 (NaCl secretbox) --------------------------------

// out_wire must have room for plaintext_len + 16 bytes (tag prefix).
// Returns true on success.
static bool secretbox_encrypt(uint8_t* out_wire,
                              const uint8_t key[32], const uint8_t nonce24[24],
                              const uint8_t* plaintext, size_t pt_len) {
    size_t nblocks = (32 + pt_len + 63) / 64;
    if (nblocks == 0) nblocks = 1;
    uint8_t* stream = (uint8_t*)malloc(nblocks * 64);
    if (!stream) return false;
    xsalsa20_stream(stream, nblocks, key, nonce24);

    // wire = tag(16) || ciphertext
    uint8_t* ct = out_wire + 16;
    for (size_t i = 0; i < pt_len; i++) {
        ct[i] = plaintext[i] ^ stream[32 + i];
    }

    // Poly1305 over ciphertext using stream[0..32] as key.
    poly1305_mac(stream, ct, pt_len, out_wire);
    free(stream);
    return true;
}

// Returns true and writes pt_len plaintext bytes if MAC verifies.
static bool secretbox_decrypt(uint8_t* out_pt,
                              const uint8_t key[32], const uint8_t nonce24[24],
                              const uint8_t* wire, size_t wire_len) {
    if (wire_len < 16) return false;
    size_t ct_len = wire_len - 16;
    size_t nblocks = (32 + ct_len + 63) / 64;
    if (nblocks == 0) nblocks = 1;
    uint8_t* stream = (uint8_t*)malloc(nblocks * 64);
    if (!stream) return false;
    xsalsa20_stream(stream, nblocks, key, nonce24);

    uint8_t want_tag[16];
    poly1305_mac(stream, wire + 16, ct_len, want_tag);
    // constant-time compare
    uint8_t diff = 0;
    for (int i = 0; i < 16; i++) diff |= wire[i] ^ want_tag[i];
    if (diff != 0) {
        free(stream);
        return false;
    }
    for (size_t i = 0; i < ct_len; i++) {
        out_pt[i] = wire[16 + i] ^ stream[32 + i];
    }
    free(stream);
    return true;
}

// ---- key derivation -----------------------------------------------------

static void derive_band_id_and_key(const String& psk) {
    // band_id = SHA-256(psk)[:16]
    uint8_t hash[32];
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    mbedtls_sha256_update(&sha, (const uint8_t*)psk.c_str(), psk.length());
    mbedtls_sha256_finish(&sha, hash);
    mbedtls_sha256_free(&sha);
    memcpy(g_band_id, hash, 16);

    // HKDF-SHA256 to derive 32-byte AEAD key.
    // salt = "telesthete-v1"; info = "encryption"
    const uint8_t salt[] = "telesthete-v1";
    const uint8_t info[] = "encryption";
    uint8_t prk[32];

    const mbedtls_md_info_t* md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    mbedtls_md_context_t hmac;
    mbedtls_md_init(&hmac);
    mbedtls_md_setup(&hmac, md, 1);

    // PRK = HMAC(salt, psk)
    mbedtls_md_hmac_starts(&hmac, salt, sizeof(salt) - 1);
    mbedtls_md_hmac_update(&hmac, (const uint8_t*)psk.c_str(), psk.length());
    mbedtls_md_hmac_finish(&hmac, prk);

    // OKM[0..32] = HMAC(PRK, info || 0x01)
    mbedtls_md_hmac_starts(&hmac, prk, 32);
    mbedtls_md_hmac_update(&hmac, info, sizeof(info) - 1);
    uint8_t one = 0x01;
    mbedtls_md_hmac_update(&hmac, &one, 1);
    mbedtls_md_hmac_finish(&hmac, g_aead_key);
    mbedtls_md_free(&hmac);
}

// ---- header + fragmentation --------------------------------------------

// 27-byte header per SPEC: band_id[16] || channel_type[1] || channel_id[2 BE]
//                        || sequence[8 BE]
static const uint8_t CHANNEL_TYPE_CHANNEL = 0x02;
static const size_t  WIRE_HEADER = 27;

// 21-byte fragmentation envelope per SPEC §6.4
//     version[1] || fragment_id[16] || seq[2 BE] || total[2 BE]
static const uint8_t FRAG_VERSION = 0x01;
static const size_t  FRAG_HEADER = 21;

static void pack_header(uint8_t out[WIRE_HEADER], uint64_t sequence) {
    memcpy(out, g_band_id, 16);
    out[16] = CHANNEL_TYPE_CHANNEL;
    out[17] = 0; out[18] = 0;  // channel_id = 0
    // sequence big-endian
    out[19] = (uint8_t)(sequence >> 56);
    out[20] = (uint8_t)(sequence >> 48);
    out[21] = (uint8_t)(sequence >> 40);
    out[22] = (uint8_t)(sequence >> 32);
    out[23] = (uint8_t)(sequence >> 24);
    out[24] = (uint8_t)(sequence >> 16);
    out[25] = (uint8_t)(sequence >> 8);
    out[26] = (uint8_t)(sequence);
}

static void make_xchacha_nonce(uint8_t nonce[24], uint64_t sequence) {
    // 16 zero bytes || sequence (8 bytes BE) — matches reference impl.
    memset(nonce, 0, 16);
    nonce[16] = (uint8_t)(sequence >> 56);
    nonce[17] = (uint8_t)(sequence >> 48);
    nonce[18] = (uint8_t)(sequence >> 40);
    nonce[19] = (uint8_t)(sequence >> 32);
    nonce[20] = (uint8_t)(sequence >> 24);
    nonce[21] = (uint8_t)(sequence >> 16);
    nonce[22] = (uint8_t)(sequence >> 8);
    nonce[23] = (uint8_t)(sequence);
}

// Single-fragment plaintext = FRAG_HEADER(version, fid, seq=0, total=1) || payload.
static void wrap_fragment(uint8_t* out, const uint8_t* payload, size_t pt_len,
                          const uint8_t fid[16]) {
    out[0] = FRAG_VERSION;
    memcpy(out + 1, fid, 16);
    out[17] = 0; out[18] = 0;  // seq
    out[19] = 0; out[20] = 1;  // total
    if (pt_len) memcpy(out + 21, payload, pt_len);
}

// Returns assembled payload length (writes into out), or 0 if multi-fragment
// or malformed.
static size_t unwrap_fragment(uint8_t* out, const uint8_t* frag, size_t frag_len) {
    if (frag_len < FRAG_HEADER) return 0;
    if (frag[0] != FRAG_VERSION) return 0;
    uint16_t seq   = ((uint16_t)frag[17] << 8) | frag[18];
    uint16_t total = ((uint16_t)frag[19] << 8) | frag[20];
    if (total != 1 || seq != 0) {
        // multi-fragment not supported in v1 — log + drop
        Serial.printf("telesthete: dropping multi-fragment frame (seq=%u total=%u)\n",
                      (unsigned)seq, (unsigned)total);
        return 0;
    }
    size_t payload_len = frag_len - FRAG_HEADER;
    if (payload_len) memcpy(out, frag + FRAG_HEADER, payload_len);
    return payload_len ? payload_len : 1;  // keepalive=0-length not used; we send 1-byte sentinel
}

// ---- send + receive -----------------------------------------------------

static void send_payload(const uint8_t* payload, size_t pt_len) {
    if (!g_inited) return;
    // Plaintext = FRAG_HEADER || payload
    size_t plain_len = FRAG_HEADER + pt_len;
    uint8_t* plain = (uint8_t*)malloc(plain_len);
    if (!plain) return;
    uint8_t fid[16];
    esp_fill_random(fid, 16);
    wrap_fragment(plain, payload, pt_len, fid);

    size_t wire_len = 16 + plain_len;
    uint8_t* wire = (uint8_t*)malloc(wire_len);
    if (!wire) { free(plain); return; }

    uint64_t seq = ++g_seq;
    uint8_t nonce[24];
    make_xchacha_nonce(nonce, seq);

    if (!secretbox_encrypt(wire, g_aead_key, nonce, plain, plain_len)) {
        free(plain); free(wire);
        return;
    }
    free(plain);

    uint8_t hdr[WIRE_HEADER];
    pack_header(hdr, seq);

    g_udp.beginPacket(g_hub_ip, g_hub_port);
    g_udp.write(hdr, WIRE_HEADER);
    g_udp.write(wire, wire_len);
    g_udp.endPacket();
    free(wire);
}

static void send_string(const String& s) {
    send_payload((const uint8_t*)s.c_str(), s.length());
}

static void send_keepalive() {
    uint8_t one = 0x00;
    send_payload(&one, 1);
}

// ---- announce ----------------------------------------------------------

static const char* CAPS_LIST[] = {
    "info.host", "info.uptime", "info.ping",
    "kvm.type", "kvm.key", "kvm.consumer",
    "kvm.hid.set", "kvm.hid.get",
};
static const size_t CAPS_LIST_N = sizeof(CAPS_LIST) / sizeof(CAPS_LIST[0]);

static void announce() {
    JsonDocument doc;
    doc["kind"] = "announce";
    doc["worker_id"] = g_worker_id;
    doc["name"] = g_worker_name;
    JsonArray caps = doc["caps"].to<JsonArray>();
    for (size_t i = 0; i < CAPS_LIST_N; i++) caps.add(CAPS_LIST[i]);
    JsonArray plugins = doc["plugins"].to<JsonArray>();
    plugins.add("info");
    plugins.add("kvm");
    String out;
    serializeJson(doc, out);
    send_string(out);
}

// ---- reply ------------------------------------------------------------

static void reply_ok(const char* msg_id, JsonDocument& body) {
    JsonDocument out;
    if (msg_id) out["id"] = msg_id;
    out["from"] = g_worker_id;
    out["ok"] = true;
    out["result"] = body;
    String s;
    serializeJson(out, s);
    send_string(s);
}

static void reply_err(const char* msg_id, const char* err) {
    JsonDocument out;
    if (msg_id) out["id"] = msg_id;
    out["from"] = g_worker_id;
    out["ok"] = false;
    out["error"] = err;
    String s;
    serializeJson(out, s);
    send_string(s);
}

// ---- capability dispatch ----------------------------------------------

static void cap_info_host(const char* msg_id) {
    JsonDocument r;
    r["device"] = TELESTHETE_NAME;
    r["version"] = ROOK_FW_VERSION;
    r["chip"] = ESP.getChipModel();
    r["mac"] = WiFi.macAddress();
    r["ssid"] = WiFi.SSID();
    r["ip"] = WiFi.localIP().toString();
    r["free_heap"] = ESP.getFreeHeap();
    reply_ok(msg_id, r);
}

static void cap_info_uptime(const char* msg_id) {
    JsonDocument r;
    r["uptime_ms"] = (uint64_t)millis();
    reply_ok(msg_id, r);
}

static void cap_info_ping(const char* msg_id, JsonVariant args) {
    JsonDocument r;
    const char* echo = args["echo"];
    if (echo) r["echo"] = echo;
    r["ts_ms"] = (uint64_t)millis();
    reply_ok(msg_id, r);
}

static void cap_kvm_type(const char* msg_id, JsonVariant args) {
    if (!hidEnabled()) { reply_err(msg_id, "hid_disabled"); return; }
    const char* text = args["text"];
    if (!text) { reply_err(msg_id, "missing 'text'"); return; }
    int delay_ms = args["delay_ms"] | DEFAULT_KEY_DELAY_MS;
    size_t len = strlen(text);
    for (size_t i = 0; i < len; i++) {
        Keyboard.write((uint8_t)text[i]);
        if (delay_ms > 0) delay(delay_ms);
    }
    JsonDocument r;
    r["typed"] = (uint32_t)len;
    reply_ok(msg_id, r);
}

static void cap_kvm_key(const char* msg_id, JsonVariant args) {
    if (!hidEnabled()) { reply_err(msg_id, "hid_disabled"); return; }
    JsonArray mods = args["modifiers"].as<JsonArray>();
    if (mods) {
        for (JsonVariant m : mods) {
            uint8_t k = lookupModifierKey(m.as<const char*>());
            if (k) Keyboard.press(k);
        }
    }
    const char* key = args["key"];
    if (key) {
        if (strlen(key) == 1) {
            Keyboard.press((uint8_t)key[0]);
        } else {
            uint8_t special = lookupSpecialKey(key);
            if (special) Keyboard.press(special);
        }
    }
    delay(50);
    Keyboard.releaseAll();
    JsonDocument r;
    r["ok"] = true;
    reply_ok(msg_id, r);
}

static void cap_kvm_consumer(const char* msg_id, JsonVariant args) {
    if (!hidEnabled()) { reply_err(msg_id, "hid_disabled"); return; }
    uint16_t code = 0;
    const char* name = args["key"];
    if (name) code = lookupConsumerKey(name);
    if (!code && args["code"].is<uint16_t>()) code = args["code"].as<uint16_t>();
    if (!code) { reply_err(msg_id, "unknown consumer key"); return; }
    Consumer.press(code);
    delay(50);
    Consumer.release();
    JsonDocument r;
    r["code"] = code;
    reply_ok(msg_id, r);
}

static void cap_kvm_hid_set(const char* msg_id, JsonVariant args) {
    if (!args["enabled"].is<bool>()) { reply_err(msg_id, "missing 'enabled' bool"); return; }
    setHidEnabled(args["enabled"].as<bool>());
    JsonDocument r;
    r["enabled"] = hidEnabled();
    reply_ok(msg_id, r);
}

static void cap_kvm_hid_get(const char* msg_id) {
    JsonDocument r;
    r["enabled"] = hidEnabled();
    reply_ok(msg_id, r);
}

static void dispatch(const uint8_t* payload, size_t len) {
    if (len == 1 && payload[0] == 0x00) {
        // keepalive sentinel — ignore
        return;
    }
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload, len);
    if (err) return;
    if (!doc.is<JsonObject>()) return;

    const char* cap = doc["cap"];
    if (!cap) return;  // announce/reply chatter — drop

    const char* target = doc["target"];
    if (target && strcmp(target, g_worker_id.c_str()) != 0) return;

    const char* msg_id = doc["id"];
    JsonVariant args = doc["args"];

    if      (strcmp(cap, "info.host")   == 0) cap_info_host(msg_id);
    else if (strcmp(cap, "info.uptime") == 0) cap_info_uptime(msg_id);
    else if (strcmp(cap, "info.ping")   == 0) cap_info_ping(msg_id, args);
    else if (strcmp(cap, "kvm.type")    == 0) cap_kvm_type(msg_id, args);
    else if (strcmp(cap, "kvm.key")     == 0) cap_kvm_key(msg_id, args);
    else if (strcmp(cap, "kvm.consumer")== 0) cap_kvm_consumer(msg_id, args);
    else if (strcmp(cap, "kvm.hid.set") == 0) cap_kvm_hid_set(msg_id, args);
    else if (strcmp(cap, "kvm.hid.get") == 0) cap_kvm_hid_get(msg_id);
    else if (target) {
        // only reply with "unknown" when addressed directly, to avoid
        // spamming the band when peers issue open calls.
        String e = String("unknown capability: ") + cap;
        reply_err(msg_id, e.c_str());
    }
}

static void recv_one(uint8_t* udp_buf, size_t udp_len) {
    if (udp_len < WIRE_HEADER + 16) return;
    if (memcmp(udp_buf, g_band_id, 16) != 0) return;  // wrong band
    uint64_t sequence = 0;
    for (int i = 0; i < 8; i++) sequence = (sequence << 8) | udp_buf[19 + i];

    const uint8_t* wire = udp_buf + WIRE_HEADER;
    size_t wire_len = udp_len - WIRE_HEADER;
    if (wire_len < 16) return;
    size_t pt_len = wire_len - 16;
    uint8_t* plain = (uint8_t*)malloc(pt_len);
    if (!plain) return;

    uint8_t nonce[24];
    make_xchacha_nonce(nonce, sequence);

    if (!secretbox_decrypt(plain, g_aead_key, nonce, wire, wire_len)) {
        free(plain);
        return;
    }

    g_last_recv_ms = millis();

    uint8_t* assembled = (uint8_t*)malloc(pt_len);  // pt_len always >= FRAG_HEADER
    if (!assembled) { free(plain); return; }
    size_t app_len = unwrap_fragment(assembled, plain, pt_len);
    free(plain);
    if (app_len == 0) { free(assembled); return; }
    dispatch(assembled, app_len);
    free(assembled);
}

// ---- task --------------------------------------------------------------

static void telestheteTask(void* arg) {
    const auto& s = getSettings();
    g_worker_name = s.worker_name.length() ? s.worker_name : String("rook-kvm");

    // Generate worker_id: 16 random bytes as hex.
    uint8_t wid[16];
    esp_fill_random(wid, 16);
    char hex[33];
    for (int i = 0; i < 16; i++) snprintf(hex + i * 2, 3, "%02x", wid[i]);
    g_worker_id = String(hex);

    derive_band_id_and_key(s.band_psk);
    g_hub_port = s.hub_port;
    g_hub_host_cached = s.hub_host;

    // DNS resolve hub. Retry while WiFi is up but DNS hasn't answered.
    bool resolved = false;
    while (!resolved) {
        if (WiFi.status() == WL_CONNECTED) {
            if (WiFi.hostByName(g_hub_host_cached.c_str(), g_hub_ip)) {
                resolved = true;
                break;
            }
            Serial.printf("telesthete: DNS lookup %s failed, retry in 5s\n",
                          g_hub_host_cached.c_str());
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    Serial.printf("telesthete: hub %s -> %s:%u band=%02x%02x%02x%02x worker=%s\n",
                  g_hub_host_cached.c_str(),
                  g_hub_ip.toString().c_str(), (unsigned)g_hub_port,
                  g_band_id[0], g_band_id[1], g_band_id[2], g_band_id[3],
                  g_worker_id.c_str());

    g_udp.begin(0);  // ephemeral port
    g_inited = true;

    // Implicit registration: keepalive frame teaches the hub our (NAT'd) addr.
    send_keepalive();
    delay(100);
    announce();

    uint32_t next_keepalive = millis() + 20000;
    uint32_t next_announce  = millis() + 30000;

    uint8_t buf[1600];
    for (;;) {
        int n = g_udp.parsePacket();
        if (n > 0 && n <= (int)sizeof(buf)) {
            int got = g_udp.read(buf, n);
            if (got > 0) recv_one(buf, (size_t)got);
        }

        uint32_t now = millis();
        if ((int32_t)(now - next_keepalive) >= 0) {
            send_keepalive();
            next_keepalive = now + 20000;
        }
        if ((int32_t)(now - next_announce) >= 0) {
            announce();
            next_announce = now + 30000;
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void setupTelesthete() {
    xTaskCreatePinnedToCore(telestheteTask, "tlst", 12288, NULL, 1, NULL, 0);
}

bool telestheteHubOk() {
    if (g_last_recv_ms == 0) return false;
    return (millis() - g_last_recv_ms) < 60000;
}

uint32_t telestheteLastSeenSec() {
    if (g_last_recv_ms == 0) return 0;
    return (millis() - g_last_recv_ms) / 1000;
}

const String& telestheteWorkerId() { return g_worker_id; }
