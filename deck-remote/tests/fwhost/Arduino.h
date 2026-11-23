// Host stand-in for the Arduino core: just what the remote's games use.
#pragma once
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <algorithm>
#define PROGMEM
extern uint32_t host_ms;
inline uint32_t millis() { return host_ms; }
