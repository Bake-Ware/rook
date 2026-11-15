// Towers: a tower defence on the bridge's screen as it sits (128 x 64). Waves
// come down a winding road; towers bought with gold on the tiles beside it
// stop them: a gun (one target, often), a blast (everything near its mark),
// frost (halves the speed of what is in reach). Ten lives; each one through
// costs one, a big one three. Keys are tetris.h's bits, read flat: LEFT,
// RIGHT, ROTATE (up) and DOWN move the cursor, DROP builds the chosen tower
// or improves the one under the cursor (three levels), ROTATE_BACK chooses
// the next kind, HOLD calls the next wave now, PAUSE pauses, QUIT leaves.
#pragma once
#include <Arduino.h>

class Adafruit_GFX;

namespace towers {

void start(uint32_t ms);
bool tick(uint32_t ms, uint16_t keys);   // true: the screen changed
bool quit();
uint32_t score();                        // the wave reached, hundreds; plus what was stopped
void draw(Adafruit_GFX& d);              // for ui::canvas

}  // namespace towers
