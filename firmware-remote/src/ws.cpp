#include "ws.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_random.h>
#include <vector>

bool WsClient::connected() { return c_ && c_->connected(); }

void WsClient::close() {
    if (!c_) return;
    c_->stop();
    delete c_;
    c_ = nullptr;
}

bool WsClient::read_n(uint8_t* b, size_t n, uint32_t ms) {
    uint32_t end = millis() + ms;
    size_t got = 0;
    while (got < n) {
        int a = c_->available();
        if (a > 0) {
            int r = c_->read(b + got, n - got);
            if (r > 0) got += r;
            continue;
        }
        if (!c_->connected() || (int32_t)(end - millis()) <= 0) return false;
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return true;
}

bool WsClient::open(const std::string& host, uint16_t port, const std::string& path, bool tls, uint32_t timeout_ms) {
    close();
    tls_ = tls;
    if (tls) {
        WiFiClientSecure* s = new WiFiClientSecure();
        s->setInsecure();
        s->setHandshakeTimeout(std::max<uint32_t>(4, timeout_ms / 1000));
        c_ = s;
        if (!s->connect(host.c_str(), port, (int32_t)timeout_ms)) { close(); return false; }
    } else {
        c_ = new WiFiClient();
        if (!c_->connect(host.c_str(), port, (int32_t)timeout_ms)) { close(); return false; }
    }
    c_->setNoDelay(true);
    std::string req = "GET " + (path.empty() ? std::string("/") : path) + " HTTP/1.1\r\nHost: " + host + ":" +
                      std::to_string(port) +
                      "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: cm9vay1yZW1vdGUtd3MtMQ==\r\nSec-WebSocket-Version: 13\r\n\r\n";
    c_->write((const uint8_t*)req.data(), req.size());
    // The answer's headers, a byte at a time so nothing after them is eaten.
    std::string head;
    uint8_t ch;
    while (head.size() < 2048) {
        if (!read_n(&ch, 1, timeout_ms)) { close(); return false; }
        head += (char)ch;
        if (head.size() >= 4 && head.compare(head.size() - 4, 4, "\r\n\r\n") == 0) break;
    }
    if (head.find(" 101") == std::string::npos) { close(); return false; }
    return true;
}

bool WsClient::frame(uint8_t op, const uint8_t* data, size_t n) {
    if (!connected() || n > 65535) return false;
    std::vector<uint8_t> f;
    f.reserve(n + 8);
    f.push_back(0x80 | op);
    if (n < 126) {
        f.push_back(0x80 | (uint8_t)n);
    } else {
        f.push_back(0x80 | 126);
        f.push_back((uint8_t)(n >> 8));
        f.push_back((uint8_t)n);
    }
    uint8_t mask[4];
    esp_fill_random(mask, 4);
    f.insert(f.end(), mask, mask + 4);
    for (size_t i = 0; i < n; i++) f.push_back(data[i] ^ mask[i & 3]);
    return c_->write(f.data(), f.size()) == f.size();
}

bool WsClient::send(const std::string& text) { return frame(0x1, (const uint8_t*)text.data(), text.size()); }

int WsClient::recv(std::string& out, uint32_t wait_ms, size_t max) {
    out.clear();
    uint32_t end = millis() + wait_ms;
    bool more = false;                       // inside a message split over frames
    for (;;) {
        if (!connected() && !(c_ && c_->available())) return -1;
        if (!c_->available()) {
            if ((int32_t)(end - millis()) <= 0) return 0;
            vTaskDelay(pdMS_TO_TICKS(2));
            continue;
        }
        uint8_t h[2];
        if (!read_n(h, 2, 3000)) { close(); return -1; }
        uint8_t op = h[0] & 0x0f;
        bool fin = h[0] & 0x80, masked = h[1] & 0x80;
        uint64_t len = h[1] & 0x7f;
        if (len == 126) {
            uint8_t e[2];
            if (!read_n(e, 2, 3000)) { close(); return -1; }
            len = ((uint64_t)e[0] << 8) | e[1];
        } else if (len == 127) {
            uint8_t e[8];
            if (!read_n(e, 8, 3000)) { close(); return -1; }
            len = 0;
            for (int i = 0; i < 8; i++) len = (len << 8) | e[i];
        }
        uint8_t mask[4] = {0, 0, 0, 0};
        if (masked && !read_n(mask, 4, 3000)) { close(); return -1; }
        bool keep = (op == 0x1 || (op == 0x0 && more) || op == 0x9) && out.size() + len <= max;
        std::string body;
        uint8_t buf[256];
        uint64_t left = len;
        size_t at = 0;
        while (left) {
            size_t n = left < sizeof buf ? (size_t)left : sizeof buf;
            if (!read_n(buf, n, 5000)) { close(); return -1; }
            if (keep) {
                for (size_t i = 0; i < n; i++) body += (char)(buf[i] ^ mask[(at + i) & 3]);
            }
            at += n;
            left -= n;
        }
        if (op == 0x8) { close(); return -1; }
        if (op == 0x9) { frame(0xA, (const uint8_t*)body.data(), body.size()); continue; }
        if (op == 0xA) continue;
        if (op == 0x1 || op == 0x0) {
            if (keep) out += body; else out.clear();      // too long: dropped whole
            more = !fin;
            if (fin && keep) return 1;
            if (fin) out.clear();
        }
    }
}
