// When the Wi-Fi task may use the radio, kept apart from the radio so the
// rules are plain to test (test/net_test.cpp). The remote's keys come first:
// IR must work with no Wi-Fi and no hub, and on the C3 the radio is one
// radio, shared by Wi-Fi and the Bluetooth remote whose keys it carries.
//   - No scan or join starts within QUIET_MS of a key, or while one is held.
//   - Joining rounds that fail back off: 10 s, 30 s, 60 s, then every 2 min,
//     from the end of the round; a join, or the person picking a network or
//     asking for a scan, starts it over.
//   - Transmit power starts moderate (11 dBm) and is raised a step only after
//     a round that failed, or at once for an access point heard faintly
//     (join_step): the supply is shared with the IR LED.
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

// The step a join to an access point heard at rssi dBm starts at: the AP's
// signal says how far away it is, so a faint one gets more power at once
// (11 dBm into a -70 dBm link does not reach back; 0.12.7 tried that for
// minutes). At least step; one heard better than -60 dBm is fine at 11 dBm.
inline int join_step(int step, int rssi) {
    int need = rssi < -67 ? 2 : rssi < -60 ? 1 : 0;
    return need > step ? need : step;
}

// A key reported held counts as held for at most this long: a remote whose
// release never arrives must not keep the radio off for good.
static const uint32_t HOLD_MAX_MS = 10000;
inline bool held_counts(bool held, uint32_t now, uint32_t held_since) {
    return held && (uint32_t)(now - held_since) < HOLD_MAX_MS;
}

}  // namespace netplan
