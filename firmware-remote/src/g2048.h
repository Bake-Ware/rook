// 2048 on the bridge's screen as it sits (128 x 64): a 4 x 4 board of tiles
// with the score beside it. Keys are tetris.h's bits, read flat: LEFT, RIGHT,
// ROTATE (up) and DOWN slide the tiles, DROP starts again once it is over,
// QUIT leaves.
#pragma once
#include <Arduino.h>

class Adafruit_GFX;

namespace g2048 {

void start(uint32_t ms);
bool tick(uint32_t ms, uint16_t keys);   // true: the screen changed
bool quit();
uint32_t score();
void draw(Adafruit_GFX& d);              // for ui::canvas

}  // namespace g2048
