// Tetris on the remote's screen held upright (32 wide, 128 tall): a 10 x 36
// well of 3-pixel cells under a score line and the next piece. The caller
// feeds it the buttons held now; it finds the presses and repeats itself.
#pragma once
#include <Arduino.h>

class Adafruit_GFX;

namespace tetris {

enum : uint8_t { LEFT = 1, RIGHT = 2, DOWN = 4, ROTATE = 8, DROP = 16, PAUSE = 32, QUIT = 64 };

void start(uint32_t ms);                 // a new game; buttons held now don't count as presses
bool tick(uint32_t ms, uint8_t keys);    // true: the screen changed
bool quit();                             // QUIT was pressed since start
uint32_t score();
void draw(Adafruit_GFX& d);

}  // namespace tetris
