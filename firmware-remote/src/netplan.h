// When the Wi-Fi task may use the radio, kept apart from the radio so the
// rules are plain to test (test/net_test.cpp). The remote's keys come first:
// IR must work with no Wi-Fi and no hub, and on the C3 the radio is one
// radio, shared by Wi-Fi and the Bluetooth remote whose keys it carries.
//   - No scan or join starts within QUIET_MS of a key, or while one is held.
//   - Joining rounds that fail back off: 10 s, 30 s, 60 s, then every 2 min,
//     from the end of the round; a join, or the person picking a network or
//     asking for a scan, starts it over.
//   - Transmit power starts moderate (11 dBm) and is raised a step only after
//     a round that failed: the supply is shared with the IR LED.
#pragma once
#include <stdint.h>

namespace netplan {

static const uint32_t QUIET_MS = 3000;

// How long after a round that failed (fails: rounds failed in a row, 1 for the first).
inline uint32_t retry_delay(unsigned fails) {
    return fails <= 1 ? 10000 : fails == 2 ? 30000 : fails == 3 ? 60000 : 120000;
}

// May a scan or a join start now? last_key: millis() of the last key (0: none yet).
inline bool may_start(uint32_t now, uint32_t last_key, bool key_held) {
    if (key_held) return false;
    return last_key == 0 || (uint32_t)(now - last_key) >= QUIET_MS;
}

// Transmit power steps, in the units WiFi.setTxPower takes (quarter dBm): 11, 15, 19.5 dBm.
static const int8_t POWER[3] = {44, 60, 78};
inline int power_step_after(int step, bool joined) { return joined ? step : step < 2 ? step + 1 : 2; }

}  // namespace netplan
