// Host stand-in for the SSD1306 driver: the picture is a 1-bit canvas the
// renderer reads (INVERSE turns a pixel over, as on the panel).
#pragma once
#include <Adafruit_GFX.h>
#include "Wire.h"
#define SSD1306_BLACK 0
#define SSD1306_WHITE 1
#define SSD1306_INVERSE 2
#define SSD1306_SWITCHCAPVCC 2
#define SSD1306_DISPLAYOFF 0xAE
#define SSD1306_DISPLAYON 0xAF
class Adafruit_SSD1306 : public GFXcanvas1 {
public:
    Adafruit_SSD1306(int w, int h, TwoWire*, int) : GFXcanvas1(w, h) {}
    bool begin(int, uint8_t) { return true; }
    void display() {}
    void clearDisplay() { fillScreen(0); }
    void ssd1306_command(uint8_t) {}
    void drawPixel(int16_t x, int16_t y, uint16_t c) override {
        GFXcanvas1::drawPixel(x, y, c == SSD1306_INVERSE ? !getPixel(x, y) : c);
    }
    void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t c) override { for (int16_t i = 0; i < w; i++) drawPixel(x + i, y, c); }
    void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t c) override { for (int16_t i = 0; i < h; i++) drawPixel(x, y + i, c); }
};
