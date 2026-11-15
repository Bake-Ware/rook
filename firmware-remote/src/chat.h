// The conversation with the house agent, as it looks on the remote's screen:
// the last few things said, each in a bubble (what the person said on the
// right, filled; what the agent said on the left, outlined; notes in the
// middle, bare), newest at the bottom, scrolled with up and down. A line
// under them says what is happening now (Listening, Thinking). Kept in
// memory only: a restart forgets it.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <string>

class Adafruit_GFX;

namespace chat {

void begin();
void add(const std::string& who, const std::string& text);   // who: you | agent | note
void clear();
void level(int percent);                 // while listening: how loud it hears now (a meter is drawn from these)
void status(const std::string& s);       // the line under the bubbles; blank: none
void scroll(int pixels);                 // towards older (negative) or newer
size_t count();
void draw(Adafruit_GFX& d);              // for ui::canvas
void fill(JsonArray a);                  // [{who, text}], oldest first

}  // namespace chat
