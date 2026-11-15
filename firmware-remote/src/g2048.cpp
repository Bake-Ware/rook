#include "g2048.h"
#include <Adafruit_GFX.h>
#include <esp_random.h>
#include "tetris.h"

namespace {

const int CW = 25, CH = 16;      // a tile; four across leave a strip on the right for the score
uint8_t g[16];                   // each tile's power of two; 0: empty
uint32_t points = 0, best = 0, pop_until = 0, won_until = 0;
int pop_at = -1;
bool over = false, won = false, quit_asked = false;
uint16_t prev_keys = 0;

// The k-th tile of a line, counted from the side the tiles slide to. dir: 0 left, 1 right, 2 up, 3 down.
int at(int line, int k, int dir) {
    switch (dir) {
        case 0: return line * 4 + k;
        case 1: return line * 4 + 3 - k;
        case 2: return k * 4 + line;
        default: return (3 - k) * 4 + line;
    }
}

bool slide(int dir) {
    bool moved = false;
    for (int line = 0; line < 4; line++) {
        uint8_t out[4] = {0, 0, 0, 0};
        int n = 0;
        bool merged = false;         // the last tile put down is already a merge
        for (int k = 0; k < 4; k++) {
            uint8_t v = g[at(line, k, dir)];
            if (!v) continue;
            if (n && out[n - 1] == v && !merged) {
                out[n - 1]++;
                points += 1ul << out[n - 1];
                if (out[n - 1] == 11 && !won) {
                    won = true;
                    won_until = millis() + 4000;
                }
                merged = true;
            } else {
                out[n++] = v;
                merged = false;
            }
        }
        for (int k = 0; k < 4; k++) {
            int i = at(line, k, dir);
            if (g[i] != out[k]) moved = true;
            g[i] = out[k];
        }
    }
    return moved;
}

void spawn(uint32_t ms) {
    int free = 0;
    for (uint8_t v : g) free += !v;
    if (!free) return;
    int pick = (int)(esp_random() % (uint32_t)free);
    for (int i = 0; i < 16; i++) {
        if (g[i] || pick--) continue;
        g[i] = esp_random() % 10 ? 1 : 2;
        pop_at = i;
        pop_until = ms + 140;
        return;
    }
}

bool stuck() {
    for (int i = 0; i < 16; i++) {
        if (!g[i]) return false;
        if (i % 4 < 3 && g[i] == g[i + 1]) return false;
        if (i < 12 && g[i] == g[i + 4]) return false;
    }
    return true;
}

void number(char* out, size_t size, uint32_t v) {
    if (v >= 10000) snprintf(out, size, "%luk", (unsigned long)(v / 1000));
    else snprintf(out, size, "%lu", (unsigned long)v);
}

}  // namespace

namespace g2048 {

void start(uint32_t ms) {
    memset(g, 0, sizeof g);
    points = 0;
    over = won = quit_asked = false;
    won_until = 0;
    prev_keys = 0xFFFF;              // what is held now is not a move
    spawn(ms);
    spawn(ms);
    pop_at = -1;
}

bool tick(uint32_t ms, uint16_t keys) {
    uint16_t pressed = keys & ~prev_keys;
    prev_keys = keys;
    if (pressed & tetris::QUIT) {
        quit_asked = true;
        return true;
    }
    if (over) {
        if (!(pressed & tetris::DROP)) return false;
        start(ms);
        prev_keys = keys;
        return true;
    }
    int dir = pressed & tetris::LEFT ? 0 : pressed & tetris::RIGHT ? 1 : pressed & tetris::ROTATE ? 2 : pressed & tetris::DOWN ? 3 : -1;
    if (dir >= 0 && slide(dir)) {
        spawn(ms);
        if (points > best) best = points;
        over = stuck();
        return true;
    }
    if (pop_at >= 0 && (int32_t)(ms - pop_until) >= 0) {       // the new tile has grown to its size
        pop_at = -1;
        return true;
    }
    if (won_until && (int32_t)(ms - won_until) >= 0) {
        won_until = 0;
        return true;
    }
    return false;
}

bool quit() { return quit_asked; }

uint32_t score() { return points; }

void draw(Adafruit_GFX& d) {
    d.setTextSize(1);
    d.setTextWrap(false);
    char t[8];
    for (int i = 0; i < 16; i++) {
        int x = (i % 4) * CW, y = (i / 4) * CH, e = g[i];
        if (!e) {
            d.drawPixel(x + CW / 2, y + CH / 2, 1);
            continue;
        }
        if (i == pop_at) {                   // just arrived: small for a moment
            d.drawRoundRect(x + 6, y + 4, CW - 12, CH - 8, 2, 1);
            continue;
        }
        // The bigger the tile, the heavier it is drawn: an outline, a double outline, then solid.
        bool solid = e >= 7;
        if (solid) d.fillRoundRect(x + 1, y + 1, CW - 2, CH - 2, 3, 1);
        else d.drawRoundRect(x + 1, y + 1, CW - 2, CH - 2, 3, 1);
        if (e >= 4 && !solid) d.drawFastHLine(x + 4, y + CH - 3, CW - 8, 1);
        number(t, sizeof t, 1ul << e);
        d.setTextColor(solid ? 0 : 1);
        d.setCursor(x + (CW - (int)strlen(t) * 6 + 1) / 2, y + 4);
        d.print(t);
    }
    d.setTextColor(1);
    int px = 4 * CW + 3;
    d.setCursor(px, 0);
    d.print("2048");
    d.drawFastHLine(px, 9, 23, 1);
    d.setCursor(px, 13);
    d.print("SC");
    number(t, sizeof t, points);
    d.setCursor(px, 22);
    d.print(t);
    d.setCursor(px, 34);
    d.print("HI");
    number(t, sizeof t, best);
    d.setCursor(px, 43);
    d.print(t);
    if (over) {
        d.fillRect(px - 1, 54, 26, 10, 1);
        d.setTextColor(0);
        d.setCursor(px, 55);
        d.print("OVER");
        d.setTextColor(1);
    } else if (won_until) {
        d.setCursor(px, 55);
        d.print("WIN!");
    }
}

}  // namespace g2048
