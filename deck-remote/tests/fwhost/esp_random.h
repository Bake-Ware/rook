// Host stand-in: a known sequence (the Python port is fed the same one).
#pragma once
#include <cstdint>
extern uint32_t host_rng;
inline uint32_t esp_random() {
    host_rng = host_rng * 1664525u + 1013904223u;
    return host_rng;
}
