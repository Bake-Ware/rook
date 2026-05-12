#pragma once
#include <Arduino.h>
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"

extern USBHIDKeyboard Keyboard;
extern USBHIDConsumerControl Consumer;

uint8_t lookupSpecialKey(const char* name);
uint8_t lookupModifierKey(const char* name);
uint16_t lookupConsumerKey(const char* name);
