// Direct IR transmit: an IR LED switched by an NPN transistor whose base is
// driven (through 1 kOhm) from one GPIO, pin HIGH = LED on. The LED may have
// little or no series resistance, so the pin idles LOW at all times and one
// send is capped (500 ms in all, no mark over 20 ms). Sent with the RMT
// peripheral and its hardware carrier; the RMT driver and its item buffer
// exist only for the length of a send.
#pragma once
#include <stdint.h>
#include <string>
#include <vector>

namespace ir {

static const uint32_t MAX_TOTAL_US = 500000, MAX_MARK_US = 20000;

struct Sent {
    int pin = -1;
    uint32_t duration_us = 0;   // first mark's start to the last mark's end
    uint32_t carrier_hz = 0;
    size_t edges = 0;           // marks and spaces sent
};

// Reads the pin (settings kv ir_pin) and drives it LOW. Call early in setup.
void begin();
bool available();               // false: this build has no RMT (sends always fail)
int pin();                      // -1: not configured
// Sets (or with -1 clears) the pin. warn: set when it is a strapping pin.
bool set_pin(int pin, std::string& err, std::string& warn);

// NEC: 9000/4500 header, 560 mark + 560 (0) or 1690 (1) space, LSB first:
// address, ~address (or a 16-bit extended address when address > 0xFF),
// command, ~command, final 560 mark; each repeat is 9000/2250/560 at 108 ms.
bool send_nec(uint16_t address, uint8_t command, int repeats, Sent& s, std::string& err);
// Durations in microseconds, alternately mark and space, starting with a mark.
bool send_raw(uint32_t carrier_hz, const std::vector<uint32_t>& durations, Sent& s, std::string& err);

}  // namespace ir
