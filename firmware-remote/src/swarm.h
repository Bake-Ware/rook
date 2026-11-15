// Swarm: a shooter for the bridge's screen, turned a quarter turn clockwise
// like its Tetris (64 wide, 128 tall). A formation sways at the top; one at a
// time they leave it, dive at the ship dropping bombs, and come round again.
// The top row takes two hits. Clear them all for the next, faster, stage.
// Keys are tetris.h's: LEFT and RIGHT move, DROP (or a ROTATE, or DOWN) fires
// and keeps firing while held, PAUSE or HOLD pauses, QUIT leaves.
#pragma once
#include <Arduino.h>

class Adafruit_GFX;

namespace swarm {

void start(uint32_t ms);
bool tick(uint32_t ms, uint16_t keys);   // true: the screen changed
bool quit();
uint32_t score();
void draw(Adafruit_GFX& d);              // for ui::canvas

}  // namespace swarm
