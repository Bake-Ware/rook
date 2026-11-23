// Host stand-in: drawing does nothing (the tests compare the games' state).
#pragma once
#include <Arduino.h>
class Adafruit_GFX {
public:
    template <typename... A> void drawPixel(A...) {}
    template <typename... A> void drawFastHLine(A...) {}
    template <typename... A> void drawFastVLine(A...) {}
    template <typename... A> void drawLine(A...) {}
    template <typename... A> void drawRect(A...) {}
    template <typename... A> void fillRect(A...) {}
    template <typename... A> void drawRoundRect(A...) {}
    template <typename... A> void fillRoundRect(A...) {}
    template <typename... A> void drawCircle(A...) {}
    template <typename... A> void fillCircle(A...) {}
    template <typename... A> void drawTriangle(A...) {}
    template <typename... A> void fillTriangle(A...) {}
    template <typename... A> void setTextSize(A...) {}
    template <typename... A> void setTextWrap(A...) {}
    template <typename... A> void setTextColor(A...) {}
    template <typename... A> void setCursor(A...) {}
    template <typename... A> void print(A...) {}
    int16_t width() { return 128; }
    int16_t height() { return 64; }
};
