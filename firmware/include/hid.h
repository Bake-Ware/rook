#pragma once
#include <Arduino.h>
#include "USBHIDKeyboard.h"

extern USBHIDKeyboard Keyboard;

uint8_t lookupSpecialKey(const char* name);
uint8_t lookupModifierKey(const char* name);
