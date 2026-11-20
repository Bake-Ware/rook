#pragma once
#include <stdlib.h>
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
inline void* heap_caps_malloc(size_t, int) { return nullptr; }     // no PSRAM, as on the C3
