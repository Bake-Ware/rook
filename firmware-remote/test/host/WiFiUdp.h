// A pretend network: every datagram one WiFiUDP sends reaches every other
// (the relay), unless the test's drop() says it is lost.
#pragma once
#include "Arduino.h"
#include <deque>
#include <functional>
#include <string>
#include <vector>

class WiFiUDP;
struct HostNet {
    std::vector<WiFiUDP*> socks;
    std::function<bool(const WiFiUDP* from, const std::string& pkt)> drop;   // true: lost
    uint32_t sent = 0, lost = 0;
    uint32_t locked_sends = 0;          // packets sent while a lock besides the band's own was held
};
extern HostNet NET;

class WiFiUDP {
public:
    ~WiFiUDP();
    uint8_t begin(uint16_t);
    int beginPacket(IPAddress, uint16_t) { out_.clear(); return 1; }
    size_t write(const uint8_t* p, size_t n) { out_.append((const char*)p, n); return n; }
    int endPacket();
    int parsePacket();
    int read(uint8_t* p, size_t n);
    void flush() { cur_.clear(); }
    std::deque<std::string> in_;
private:
    std::string out_, cur_;
};
