// A small WebSocket client (RFC 6455, text messages) over TCP or TLS. TLS is
// without certificate checks: it is for TVs on the local network, which sign
// their own.
#pragma once
#include <Arduino.h>
#include <string>

class WiFiClient;

class WsClient {
public:
    ~WsClient() { close(); }
    bool open(const std::string& host, uint16_t port, const std::string& path, bool tls, uint32_t timeout_ms);
    bool send(const std::string& text);
    // 1: a text message is in out. 0: nothing within wait_ms. -1: closed.
    int recv(std::string& out, uint32_t wait_ms, size_t max = 8192);
    void close();
    bool connected();
    bool tls() const { return tls_; }

private:
    WiFiClient* c_ = nullptr;
    bool tls_ = false;
    bool read_n(uint8_t* b, size_t n, uint32_t ms);
    bool frame(uint8_t op, const uint8_t* data, size_t n);
};
