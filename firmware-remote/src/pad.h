// What only a game controller has (the gamepad build; pad.cpp): several keys
// held at once, a light bar, motors. Its keys by name come through bt.h.
#pragma once
#include <Arduino.h>

namespace pad {

// What is held now, as the games take it (tetris.h's bits). flat: for the games
// played with the screen as it sits (2048, Towers).
uint16_t game_keys(bool flat);
void light(uint8_t r, uint8_t g, uint8_t b);              // the light bar
void rumble(uint8_t weak, uint8_t strong, uint32_t ms);   // the small motor, the big one, for that long
void tick();                                              // from the main loop: tells the pad what changed

}  // namespace pad
