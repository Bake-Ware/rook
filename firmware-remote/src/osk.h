// An on-screen keyboard for the remote's small screen: a grid of characters
// walked with the direction keys, three pages (lower case, upper case,
// symbols; digits on the first, their shifted marks !@#$%^&*() on the second) and four actions (next page, space,
// delete, done). Laid out for whichever screen the build has.
#pragma once
#include <Arduino.h>
#include <string>

class Adafruit_GFX;

namespace osk {

void begin(const std::string& title, const std::string& initial);
// One key by name: up, down, left, right, ok (type or act), back (delete; on
// empty text: cancel), input or home (cancel). 0 still typing, 1 done, -1 cancelled.
int key(const std::string& name);
const std::string& text();
void draw(Adafruit_GFX& d);

}  // namespace osk
