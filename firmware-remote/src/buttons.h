// Buttons charlieplexed across the PCF8574's eight lines: each button joins
// two lines (no diodes, nothing to ground). A scan pulls one line low at a
// time and reads which others follow. A button is known by its line pair;
// pairs are numbered in the order they are first pressed (and kept in NVS),
// so pressing every button once in reading order sets the layout.
#pragma once
#include <Arduino.h>
#include <functional>
#include <string>
#include <vector>

struct ButtonEvent {
    uint32_t seq;
    int index;              // position in the layout
    std::string name;
    std::string lines;      // the two expander lines it joins, "a-b"
    const char* action;     // "press" | "long"
    uint32_t held_ms;
    uint32_t at_ms;
};

namespace buttons {

bool begin(uint8_t pcf_addr);
void poll();                                   // call every few ms
std::function<void(const ButtonEvent&)>& on_event();

int count();
std::string name(int index);
std::string pair(int index);                   // "a-b"
std::vector<int> held();
void set_names(const std::vector<std::string>& names);
void forget_layout();                          // next presses number the buttons again
void set_identify(bool on);                    // report presses by line pair only: no learning
bool identify();
void set_layout(const std::vector<std::string>& pairs);   // button order, as "a-b" pairs
bool learning();                               // fewer buttons known than expected
int expected();

}  // namespace buttons
