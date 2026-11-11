// The Rook band transport (Telesthete profile, spec core-v1 section 2, kept by
// core-v2 2.1): UDP to the relay, ChaCha20-Poly1305 with empty AAD, 27-byte
// frame header, 21-byte fragment envelope with reassembly, keepalives.
// Crypto and framing come from the Rook dongle firmware (firmware/src/telesthete.cpp).
#pragma once
#include <Arduino.h>
#include <WiFiUdp.h>
#include <functional>
#include <string>

namespace rc {

class Band {
public:
    // A complete message (JSON or binary), reassembled and decrypted. Runs on
    // the caller of poll(); keepalives (single 0x00) are not delivered.
    std::function<void(const uint8_t*, size_t)> on_message;
    // Cheap pre-filter: return false to skip a message before it is assembled
    // into one buffer (it still has to be decrypted). Optional.

    bool begin(const std::string& psk, const std::string& relay);   // relay "host:port"
    void poll();                          // read pending datagrams, keepalive, re-resolve
    bool send(const std::string& msg);    // fragments as needed; thread-safe
    const std::string& band_hex() const { return band_hex_; }
    bool ready() const { return ready_; }
    uint32_t last_rx_ms() const { return last_rx_; }

private:
    struct Partial {
        uint8_t fid[16];
        uint16_t total = 0, got = 0;
        uint32_t started = 0;
        uint8_t* data = nullptr;          // total * 1003
        size_t* lens = nullptr;
        uint8_t* have = nullptr;
    };
    static const size_t WIRE_HEADER = 27, FRAG_HEADER = 21, CHUNK = 1003;
    static const int MAX_PARTIALS = 4;
    static const uint16_t MAX_CHUNKS = 32;   // 32 KB per message; bigger ones are never for us

    WiFiUDP udp_;
    IPAddress ip_;
    uint16_t port_ = 0;
    std::string host_, band_hex_;
    uint8_t band_id_[16];
    uint8_t key_[32];
    uint64_t seq_ = 0;
    bool ready_ = false;
    uint32_t last_keepalive_ = 0, last_resolve_ = 0, last_rx_ = 0;
    SemaphoreHandle_t lock_ = nullptr;
    Partial parts_[MAX_PARTIALS];
    uint8_t* rxbuf_ = nullptr;

    bool resolve();
    void send_chunk(const uint8_t* fid, uint16_t idx, uint16_t total, const uint8_t* data, size_t n);
    void on_datagram(const uint8_t* buf, size_t n);
    void on_chunk(const uint8_t* plain, size_t n);
    void drop(Partial& p);
};

}  // namespace rc
