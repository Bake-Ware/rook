#pragma once
#include <stdint.h>
#include <stdlib.h>
inline uint32_t esp_random() { return ((uint32_t)rand() << 16) ^ (uint32_t)rand(); }
inline void esp_fill_random(void* p, size_t n) { for (size_t i = 0; i < n; i++) ((uint8_t*)p)[i] = (uint8_t)rand(); }
