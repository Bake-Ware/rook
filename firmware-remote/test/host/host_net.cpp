#include "WiFiUdp.h"
#include "WiFi.h"
#include <algorithm>

HostEsp ESP;
// The test's limit on allocations (worker_test.cpp) is the worker's: the
// network's own copies of packets (lwIP's buffers on the chip) are not under it.
extern size_t alloc_limit;
extern int host_exempt;                 // > 0: no allocation fails (worker_test.cpp's fault injection)
struct Unlimited {
    size_t was = alloc_limit;
    Unlimited() { alloc_limit = 0; host_exempt++; }
    ~Unlimited() { alloc_limit = was; host_exempt--; }
};
HostWiFi WiFi;
HostNet NET;

WiFiUDP::~WiFiUDP() { NET.socks.erase(std::remove(NET.socks.begin(), NET.socks.end(), this), NET.socks.end()); }
uint8_t WiFiUDP::begin(uint16_t) {
    if (std::find(NET.socks.begin(), NET.socks.end(), this) == NET.socks.end()) NET.socks.push_back(this);
    return 1;
}
int WiFiUDP::endPacket() {
    Unlimited u;
    NET.sent++;
    if (NET.drop && NET.drop(this, out_)) { NET.lost++; return 1; }
    for (auto* s : NET.socks) if (s != this) s->in_.push_back(out_);
    return 1;
}
int WiFiUDP::parsePacket() {
    Unlimited u;
    if (in_.empty()) return 0;
    cur_ = in_.front();
    in_.pop_front();
    return (int)cur_.size();
}
int WiFiUDP::read(uint8_t* p, size_t n) {
    size_t k = std::min(n, cur_.size());
    memcpy(p, cur_.data(), k);
    cur_.clear();
    return (int)k;
}
