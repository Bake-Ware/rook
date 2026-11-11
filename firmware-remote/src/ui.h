// The 128x32 OLED, held upright (32 wide, 128 tall: 5 characters by 16
// lines): a status screen, text an agent put up, the question screen for
// remote.ask, the remote's own menu, and the game. A line too long for the
// width scrolls. Draws from its own state under a lock; any task may call
// these, ui::tick() runs on the main loop.
#pragma once
#include <Arduino.h>
#include <string>
#include <vector>

namespace ui {

bool begin(uint8_t addr, int rotation);               // rotation: quarter turns clockwise, 0..3
void set_rotation(int rotation);
void tick();                                          // redraw when something changed
void redraw();                                        // something drawn from outside (the game) changed

void status_line(int line, const std::string& text);  // lines 0..3 of the status screen
void text(const std::string& t, uint32_t seconds);    // agent text; 0 s = until replaced
std::string shown();                                  // what the screen says now
void flash(const std::string& t);                     // a brief note (1.5 s) over the status screen
void big(const std::string& t);                       // a few characters, large, until replaced

// Question screen: prompt and options with a cursor.
void ask_begin(const std::string& prompt, const std::vector<std::string>& options);
void ask_move(int delta);
int ask_selected();
void ask_end();
bool asking();

// The remote's own menu: a title and items with a cursor.
void menu_begin(const std::string& title, const std::vector<std::string>& items, int selected);
void menu_move(int delta);
int menu_selected();
void menu_end();

void game(bool on);                                   // the screen is the game's (tetris::draw)

}  // namespace ui
