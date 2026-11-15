#include "rc_band.h"
#include "rc_util.h"
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_random.h>
#include <string.h>

namespace rc {

// ---- ChaCha20-Poly1305 (IETF, RFC 8439), empty AAD --------------------------
// From firmware/src/telesthete.cpp (Rook dongle): poly1305-donna-32 and a
// plain-C ChaCha20, because this Arduino core's mbedcrypto has neither.

static inline uint32_t load32_le(const uint8_t* s) {
    return (uint32_t)s[0] | ((uint32_t)s[1] << 8) | ((uint32_t)s[2] << 16) | ((uint32_t)s[3] << 24);
}
static inline void store32_le(uint8_t* d, uint32_t v) {
    d[0] = v; d[1] = v >> 8; d[2] = v >> 16; d[3] = v >> 24;
}

static void poly1305_mac(const uint8_t key[32], const uint8_t* msg, size_t len, uint8_t out[16]) {
    uint32_t r0, r1, r2, r3, r4, s1, s2, s3, s4, h0, h1, h2, h3, h4, c;
    uint64_t d0, d1, d2, d3, d4;
    r0 = (load32_le(key + 0)) & 0x03ffffff;
    r1 = (load32_le(key + 3) >> 2) & 0x03ffff03;
    r2 = (load32_le(key + 6) >> 4) & 0x03ffc0ff;
    r3 = (load32_le(key + 9) >> 6) & 0x03f03fff;
    r4 = (load32_le(key + 12) >> 8) & 0x000fffff;
    s1 = r1 * 5; s2 = r2 * 5; s3 = r3 * 5; s4 = r4 * 5;
    h0 = h1 = h2 = h3 = h4 = 0;
    uint8_t buf[16];
    while (len > 0) {
        size_t n;
        uint32_t hibit;
        if (len >= 16) { memcpy(buf, msg, 16); n = 16; hibit = 1u << 24; }
        else { memset(buf, 0, 16); memcpy(buf, msg, len); buf[len] = 1; n = len; hibit = 0; }
        msg += n; len -= n;
        h0 += (load32_le(buf + 0)) & 0x3ffffff;
        h1 += (load32_le(buf + 3) >> 2) & 0x3ffffff;
        h2 += (load32_le(buf + 6) >> 4) & 0x3ffffff;
        h3 += (load32_le(buf + 9) >> 6) & 0x3ffffff;
        h4 += (load32_le(buf + 12) >> 8) | hibit;
        d0 = (uint64_t)h0 * r0 + (uint64_t)h1 * s4 + (uint64_t)h2 * s3 + (uint64_t)h3 * s2 + (uint64_t)h4 * s1;
        d1 = (uint64_t)h0 * r1 + (uint64_t)h1 * r0 + (uint64_t)h2 * s4 + (uint64_t)h3 * s3 + (uint64_t)h4 * s2;
        d2 = (uint64_t)h0 * r2 + (uint64_t)h1 * r1 + (uint64_t)h2 * r0 + (uint64_t)h3 * s4 + (uint64_t)h4 * s3;
        d3 = (uint64_t)h0 * r3 + (uint64_t)h1 * r2 + (uint64_t)h2 * r1 + (uint64_t)h3 * r0 + (uint64_t)h4 * s4;
        d4 = (uint64_t)h0 * r4 + (uint64_t)h1 * r3 + (uint64_t)h2 * r2 + (uint64_t)h3 * r1 + (uint64_t)h4 * r0;
                   c = (uint32_t)(d0 >> 26); h0 = (uint32_t)d0 & 0x3ffffff;
        d1 += c;   c = (uint32_t)(d1 >> 26); h1 = (uint32_t)d1 & 0x3ffffff;
        d2 += c;   c = (uint32_t)(d2 >> 26); h2 = (uint32_t)d2 & 0x3ffffff;
        d3 += c;   c = (uint32_t)(d3 >> 26); h3 = (uint32_t)d3 & 0x3ffffff;
        d4 += c;   c = (uint32_t)(d4 >> 26); h4 = (uint32_t)d4 & 0x3ffffff;
        h0 += c * 5; c = h0 >> 26; h0 &= 0x3ffffff;
        h1 += c;
    }
    c = h1 >> 26; h1 &= 0x3ffffff;
    h2 += c; c = h2 >> 26; h2 &= 0x3ffffff;
    h3 += c; c = h3 >> 26; h3 &= 0x3ffffff;
    h4 += c; c = h4 >> 26; h4 &= 0x3ffffff;
    h0 += c * 5; c = h0 >> 26; h0 &= 0x3ffffff;
    h1 += c;
    uint32_t g0 = h0 + 5; c = g0 >> 26; g0 &= 0x3ffffff;
    uint32_t g1 = h1 + c; c = g1 >> 26; g1 &= 0x3ffffff;
    uint32_t g2 = h2 + c; c = g2 >> 26; g2 &= 0x3ffffff;
    uint32_t g3 = h3 + c; c = g3 >> 26; g3 &= 0x3ffffff;
    uint32_t g4 = h4 + c - (1u << 26);
    uint32_t mask = (g4 >> 31) - 1;
    g0 &= mask; g1 &= mask; g2 &= mask; g3 &= mask; g4 &= mask;
    mask = ~mask;
    h0 = (h0 & mask) | g0; h1 = (h1 & mask) | g1; h2 = (h2 & mask) | g2; h3 = (h3 & mask) | g3; h4 = (h4 & mask) | g4;
    h0 = h0 | (h1 << 26);
    h1 = (h1 >> 6) | (h2 << 20);
    h2 = (h2 >> 12) | (h3 << 14);
    h3 = (h3 >> 18) | (h4 << 8);
    uint64_t f;
    f = (uint64_t)h0 + load32_le(key + 16);             h0 = (uint32_t)f;
    f = (uint64_t)h1 + load32_le(key + 20) + (f >> 32); h1 = (uint32_t)f;
    f = (uint64_t)h2 + load32_le(key + 24) + (f >> 32); h2 = (uint32_t)f;
    f = (uint64_t)h3 + load32_le(key + 28) + (f >> 32); h3 = (uint32_t)f;
    store32_le(out + 0, h0); store32_le(out + 4, h1); store32_le(out + 8, h2); store32_le(out + 12, h3);
}

#define ROTL32(v, n) (((v) << (n)) | ((v) >> (32 - (n))))
#define QR(a, b, c, d) do { \
    a += b; d ^= a; d = ROTL32(d, 16); c += d; b ^= c; b = ROTL32(b, 12); \
    a += b; d ^= a; d = ROTL32(d, 8);  c += d; b ^= c; b = ROTL32(b, 7); } while (0)

static void chacha20_block(uint8_t out[64], const uint8_t key[32], const uint8_t nonce[12], uint32_t counter) {
    uint32_t in[16] = {0x61707865u, 0x3320646eu, 0x79622d32u, 0x6b206574u};
    for (int i = 0; i < 8; i++) in[4 + i] = load32_le(key + 4 * i);
    in[12] = counter;
    in[13] = load32_le(nonce); in[14] = load32_le(nonce + 4); in[15] = load32_le(nonce + 8);
    uint32_t x[16];
    memcpy(x, in, 64);
    for (int i = 0; i < 10; i++) {
        QR(x[0], x[4], x[8], x[12]); QR(x[1], x[5], x[9], x[13]); QR(x[2], x[6], x[10], x[14]); QR(x[3], x[7], x[11], x[15]);
        QR(x[0], x[5], x[10], x[15]); QR(x[1], x[6], x[11], x[12]); QR(x[2], x[7], x[8], x[13]); QR(x[3], x[4], x[9], x[14]);
    }
    for (int i = 0; i < 16; i++) store32_le(out + 4 * i, x[i] + in[i]);
}

static void chacha20_xor(uint8_t* out, const uint8_t* in, size_t len, const uint8_t key[32], const uint8_t nonce[12], uint32_t ctr) {
    uint8_t ks[64];
    for (size_t off = 0; off < len; off += 64) {
        chacha20_block(ks, key, nonce, ctr++);
        size_t n = len - off < 64 ? len - off : 64;
        for (size_t i = 0; i < n; i++) out[off + i] = in[off + i] ^ ks[i];
    }
}

static void aead_tag(uint8_t tag[16], const uint8_t otk[32], const uint8_t* ct, size_t ct_len) {
    // mac_data = ct || pad16 || le64(0) || le64(ct_len); fed in pieces through one buffer
    size_t pad = (16 - (ct_len & 15)) & 15;
    size_t mlen = ct_len + pad + 16;
    uint8_t* m = (uint8_t*)malloc(mlen);
    if (!m) { memset(tag, 0, 16); return; }
    memcpy(m, ct, ct_len);
    memset(m + ct_len, 0, pad + 8);
    uint64_t cl = ct_len;
    for (int i = 0; i < 8; i++) m[ct_len + pad + 8 + i] = (uint8_t)(cl >> (8 * i));
    poly1305_mac(otk, m, mlen, tag);
    free(m);
}

static void nonce_of(uint8_t n[12], uint64_t seq) {
    memset(n, 0, 4);
    for (int i = 0; i < 8; i++) n[4 + i] = (uint8_t)(seq >> (56 - 8 * i));
}

// ---- Band -------------------------------------------------------------------

bool Band::begin(const std::string& psk, const std::string& relay) {
    if (!lock_) lock_ = xSemaphoreCreateMutex();
    if (!rxbuf_) rxbuf_ = (uint8_t*)malloc(1600);
    uint8_t h[32];
    sha256(psk.data(), psk.size(), h);
    memcpy(band_id_, h, 16);
    band_hex_ = hex(band_id_, 16);
    // HKDF-SHA256(salt "telesthete-v1", ikm psk, info "encryption-chacha20-poly1305", L 32)
    static const char salt[] = "telesthete-v1";
    static const char info[] = "encryption-chacha20-poly1305";
    uint8_t prk[32];
    hmac_sha256((const uint8_t*)salt, sizeof(salt) - 1, (const uint8_t*)psk.data(), psk.size(), prk);
    uint8_t t[sizeof(info)];
    memcpy(t, info, sizeof(info) - 1);
    t[sizeof(info) - 1] = 0x01;
    hmac_sha256(prk, 32, t, sizeof(info), key_);
    // random 63-bit starting sequence (spec 2.3)
    seq_ = (((uint64_t)esp_random() << 32) | esp_random()) & 0x7fffffffffffffffULL;
    size_t colon = relay.rfind(':');
    if (colon == std::string::npos) return false;
    host_ = relay.substr(0, colon);
    port_ = (uint16_t)atoi(relay.c_str() + colon + 1);
    udp_.begin(0);
    ready_ = resolve();
    return ready_;
}

bool Band::resolve() {
    last_resolve_ = millis();
    if (WiFi.status() != WL_CONNECTED) return false;
    IPAddress ip;
    if (!WiFi.hostByName(host_.c_str(), ip) || ip == IPAddress((uint32_t)0)) return false;
    ip_ = ip;
    return true;
}

void Band::send_chunk(const uint8_t* fid, uint16_t idx, uint16_t total, const uint8_t* data, size_t n) {
    size_t plen = FRAG_HEADER + n;
    uint8_t* frame = (uint8_t*)malloc(WIRE_HEADER + plen + 16);
    if (!frame) return;
    uint8_t* plain = frame + WIRE_HEADER;           // encrypt in place
    plain[0] = 0x01;
    memcpy(plain + 1, fid, 16);
    plain[17] = idx >> 8; plain[18] = idx; plain[19] = total >> 8; plain[20] = total;
    if (n) memcpy(plain + FRAG_HEADER, data, n);
    uint64_t seq = ++seq_;
    memcpy(frame, band_id_, 16);
    frame[16] = 0x02; frame[17] = 0; frame[18] = 0;
    for (int i = 0; i < 8; i++) frame[19 + i] = (uint8_t)(seq >> (56 - 8 * i));
    uint8_t nonce[12], block0[64];
    nonce_of(nonce, seq);
    chacha20_block(block0, key_, nonce, 0);
    chacha20_xor(plain, plain, plen, key_, nonce, 1);
    aead_tag(plain + plen, block0, plain, plen);
    udp_.beginPacket(ip_, port_);
    udp_.write(frame, WIRE_HEADER + plen + 16);
    udp_.endPacket();
    free(frame);
}

bool Band::send(const std::string& msg) {
    if (!ready_) return false;
    xSemaphoreTake(lock_, portMAX_DELAY);
    uint8_t fid[16];
    esp_fill_random(fid, 16);
    size_t n = msg.size();
    uint16_t total = n == 0 ? 1 : (uint16_t)((n + CHUNK - 1) / CHUNK);
    for (uint16_t i = 0; i < total; i++) {
        size_t off = (size_t)i * CHUNK;
        size_t len = n - off < CHUNK ? n - off : CHUNK;
        send_chunk(fid, i, total, (const uint8_t*)msg.data() + off, n ? len : 0);
    }
    xSemaphoreGive(lock_);
    return true;
}

void Band::drop(Partial& p) {
    free(p.data); free(p.lens); free(p.have);
    p.data = nullptr; p.lens = nullptr; p.have = nullptr;
    p.total = p.got = 0;
}

void Band::on_chunk(const uint8_t* f, size_t n) {
    if (n < FRAG_HEADER || f[0] != 0x01) return;
    uint16_t idx = (uint16_t)f[17] << 8 | f[18];
    uint16_t total = (uint16_t)f[19] << 8 | f[20];
    if (total == 0 || idx >= total || n - FRAG_HEADER > CHUNK) return;
    const uint8_t* data = f + FRAG_HEADER;
    size_t len = n - FRAG_HEADER;
    if (total == 1) {
        if (len == 1 && data[0] == 0x00) return;          // keepalive
        if (on_message) on_message(data, len);
        return;
    }
    if (total > MAX_CHUNKS) return;
    uint32_t now = millis();
    Partial* p = nullptr;
    Partial* oldest = &parts_[0];
    for (auto& q : parts_) {
        if (q.total && now - q.started > 30000) drop(q);
        if (q.total && memcmp(q.fid, f + 1, 16) == 0) p = &q;
    }
    if (p && p->total != total) { drop(*p); p = nullptr; }     // restart on a changed total
    if (!p) {
        for (auto& q : parts_) {
            if (!q.total) { p = &q; break; }
            if (q.started < oldest->started) oldest = &q;
        }
        if (!p) { drop(*oldest); p = oldest; }
        p->data = (uint8_t*)heap_caps_malloc((size_t)total * CHUNK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!p->data) p->data = (uint8_t*)malloc((size_t)total * CHUNK);
        p->lens = (size_t*)calloc(total, sizeof(size_t));
        p->have = (uint8_t*)calloc(total, 1);
        if (!p->data || !p->lens || !p->have) { drop(*p); return; }
        memcpy(p->fid, f + 1, 16);
        p->total = total;
        p->got = 0;
        p->started = now;
    }
    if (p->have[idx]) return;                                  // duplicate
    memcpy(p->data + (size_t)idx * CHUNK, data, len);
    p->lens[idx] = len;
    p->have[idx] = 1;
    if (++p->got < p->total) return;
    // compact (every chunk but the last is full by spec, but don't rely on it)
    size_t off = 0;
    for (uint16_t i = 0; i < p->total; i++) {
        if (off != (size_t)i * CHUNK) memmove(p->data + off, p->data + (size_t)i * CHUNK, p->lens[i]);
        off += p->lens[i];
    }
    uint8_t* msg = p->data;
    p->data = nullptr;
    drop(*p);
    if (on_message) on_message(msg, off);
    free(msg);
}

void Band::on_datagram(const uint8_t* buf, size_t n) {
    if (n < WIRE_HEADER + 16 + 1 || memcmp(buf, band_id_, 16) != 0) return;
    if (buf[16] != 0x02 && buf[16] != 0x06) return;
    uint64_t seq = 0;
    for (int i = 0; i < 8; i++) seq = seq << 8 | buf[19 + i];
    const uint8_t* ct = buf + WIRE_HEADER;
    size_t ct_len = n - WIRE_HEADER - 16;
    uint8_t nonce[12], block0[64], tag[16];
    nonce_of(nonce, seq);
    chacha20_block(block0, key_, nonce, 0);
    aead_tag(tag, block0, ct, ct_len);
    uint8_t diff = 0;
    for (int i = 0; i < 16; i++) diff |= tag[i] ^ ct[ct_len + i];
    if (diff) return;
    uint8_t* plain = (uint8_t*)malloc(ct_len);
    if (!plain) return;
    chacha20_xor(plain, ct, ct_len, key_, nonce, 1);
    last_rx_ = millis();
    on_chunk(plain, ct_len);
    free(plain);
}

void Band::poll() {
    uint32_t now = millis();
    if (!ready_ || (now - last_resolve_ > 600000)) {
        if (now - last_resolve_ > 5000 || !ready_) {
            bool was = ready_;
            if (resolve()) ready_ = true;
            if (ready_ && !was) last_keepalive_ = 0;
        }
    }
    if (!ready_) return;
    if (last_keepalive_ == 0 || now - last_keepalive_ > 15000) {
        last_keepalive_ = now;
        std::string ka(1, '\0');
        send(ka);
    }
    for (int k = 0; k < 32; k++) {
        int n = udp_.parsePacket();
        if (n <= 0) break;
        if (n > 1600) { udp_.flush(); continue; }
        int got = udp_.read(rxbuf_, n);
        if (got > 0) on_datagram(rxbuf_, (size_t)got);
    }
}

}  // namespace rc
