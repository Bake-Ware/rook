// The 128x32 OLED: a status screen, text an agent put up, and the question
// screen for remote.ask. Draws from its own state under a lock; any task may
// call these.
#pragma once
#include <Arduino.h>
#include <string>
#include <vector>

namespace ui {

bool begin(uint8_t addr);
void tick();                                          // redraw when something changed

void status_line(int line, const std::string& text);  // lines 0..3 of the status screen
void text(const std::string& t, uint32_t seconds);    // agent text; 0 s = until replaced
std::string shown();                                  // what the screen says now
void flash(const std::string& t);
void big(const std::string& t);                       // a few characters, large, until replaced                     // a brief note (1.5 s) over the status screen

// Question screen: prompt and options with a cursor.
void ask_begin(const std::string& prompt, const std::vector<std::string>& options);
void ask_move(int delta);
int ask_selected();
void ask_end();
bool asking();

}  // namespace ui
