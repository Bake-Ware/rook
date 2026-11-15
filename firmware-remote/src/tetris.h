// Tetris on the remote's screen. The handheld holds its screen upright (32
// wide, 128 tall): a 10 x 36 well of 3-pixel cells under a score line and the
// next piece. The bridge turns its 128x64 screen a quarter turn clockwise for
// the game (64 wide, 128 tall): the classic 10 x 20 well of 5-pixel cells
// under the score, the piece held and the piece to come. Both show where the
// piece will land. The caller feeds it the buttons held now; it finds the
// presses and repeats itself.
#pragma once
#include <Arduino.h>

class Adafruit_GFX;

namespace tetris {

enum : uint16_t {
    LEFT = 1, RIGHT = 2, DOWN = 4, ROTATE = 8, DROP = 16, PAUSE = 32, QUIT = 64,
    ROTATE_BACK = 128,      // a quarter turn the other way
    HOLD = 256,             // put the piece aside (once a piece), or swap it with the one put aside
};

void start(uint32_t ms);                 // a new game; buttons held now don't count as presses
bool tick(uint32_t ms, uint16_t keys);   // true: the screen changed
bool quit();                             // QUIT was pressed since start
uint32_t score();
void draw(Adafruit_GFX& d);

}  // namespace tetris
