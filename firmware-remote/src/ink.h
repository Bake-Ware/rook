// Colours, for the build whose screen has them (the gamepad's LCD). On the
// one-colour screens every ink is 1: lit. Never 0, 1 or 2 on a colour screen
// (those stay off, lit and turned-over, as the OLED has them).
#pragma once
#include <Arduino.h>

#ifdef ROOK_GAMEPAD
#define INK(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))
#else
#define INK(r, g, b) ((uint16_t)1)
#endif
