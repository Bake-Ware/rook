// Host stand-in for the Arduino core's Print, as much as Adafruit GFX and ui.cpp use.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#ifndef radians
#define radians(d) ((d) * 0.017453292519943295)
#endif
#ifndef PROGMEM
#define PROGMEM
#endif
class __FlashStringHelper;
// What Adafruit GFX's String overloads need to name (ui.cpp prints C strings only).
class String {
public:
    const char* c_str() const { return ""; }
    unsigned length() const { return 0; }
};
class Print {
public:
    virtual ~Print() {}
    virtual size_t write(uint8_t c) = 0;
    virtual size_t write(const uint8_t* b, size_t n) { size_t k = 0; while (n--) k += write(*b++); return k; }
    size_t print(const char* s) { return write((const uint8_t*)s, strlen(s)); }
    size_t print(char c) { return write((uint8_t)c); }
    size_t print(int v) { char b[16]; snprintf(b, sizeof b, "%d", v); return print(b); }
    size_t print(unsigned v) { char b[16]; snprintf(b, sizeof b, "%u", v); return print(b); }
    size_t println(const char* s) { return print(s) + print("\n"); }
};
