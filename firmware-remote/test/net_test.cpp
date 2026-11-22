// Host test of when the Wi-Fi task may use the radio (src/netplan.h): the
// back-off between failed joining rounds, the quiet window after a key, and the
// transmit power steps. And a day without the network, simulated: how many
// rounds the radio is busy for, before and after 0.12.7.
//   cd firmware-remote/test && g++ -std=gnu++17 -I../src net_test.cpp -o /tmp/net_test && /tmp/net_test
#include <stdio.h>
#include <string>
#include "netplan.h"

static int fails = 0, passes = 0;
static void expect(bool ok, const std::string& what) {
    if (ok) passes++;
    else fails++;
    printf("%s %s\n", ok ? "ok  " : "FAIL", what.c_str());
}

int main() {
    using namespace netplan;
    expect(retry_delay(1) == 10000 && retry_delay(2) == 30000 && retry_delay(3) == 60000 && retry_delay(4) == 120000 &&
               retry_delay(50) == 120000,
           "failed rounds back off: 10 s, 30 s, 60 s, then every 2 min");
    expect(may_start(5000, 0, false), "no key yet: a round may start");
    expect(!may_start(5000, 4000, false) && !may_start(6999, 4000, false) && may_start(7000, 4000, false),
           "within 3 s of a key no round starts; at 3 s it may");
    expect(!may_start(100000, 1, true), "while a key is held no round starts, however long ago it went down");
    // a key 2048 ms before millis() wraps: 500 ms after the wrap is 2548 ms on, 1000 ms after is 3048
    expect(!may_start(500, 0xFFFFF800u, false) && may_start(1000, 0xFFFFF800u, false),
           "the quiet window holds across millis() wrapping");
    expect(POWER[0] == 44 && power_step_after(0, true) == 0 && power_step_after(0, false) == 1 &&
               power_step_after(1, false) == 2 && power_step_after(2, false) == 2 && power_step_after(2, true) == 2,
           "transmit power starts at 11 dBm and is raised a step (15, 19.5 dBm) only after a round fails");
    // A day with the network gone (the access point off): rounds of ~4 s scan + ~15 s join.
    auto busy = [](bool backoff) {
        uint32_t t = 0, radio = 0;
        unsigned f = 0;
        int rounds = 0;
        while (t < 86400000u) {
            radio += 19000;
            t += 19000;
            rounds++;
            f++;
            t += backoff ? retry_delay(f) : 5000;      // 0.12.6: 5 s between rounds, always
        }
        printf("     %s: %d rounds, the radio busy %u%% of the day\n", backoff ? "0.12.7" : "0.12.6", rounds,
               (unsigned)((uint64_t)radio * 100 / t));
        return (uint64_t)radio * 100 / t;
    };
    uint64_t before = busy(false), after = busy(true);
    expect(before > 70 && after < 15, "a day without the network: the radio busy " + std::to_string(before) + "% before, " +
                                          std::to_string(after) + "% now");
    printf("%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
