#include "tetris.h"
#include <Adafruit_GFX.h>
#include <esp_random.h>

namespace {

const int COLS = 10, ROWS = 36, CELL = 3, TOP = 20;
const uint16_t FULL = (1u << COLS) - 1;
// Each piece's four turns as a 4x4 bitmap, top-left bit first.
const uint16_t SHAPES[7][4] = {
    {0x0F00, 0x2222, 0x00F0, 0x4444},   // I
    {0x44C0, 0x8E00, 0x6440, 0x0E20},   // J
    {0x4460, 0x0E80, 0xC440, 0x2E00},   // L
    {0xCC00, 0xCC00, 0xCC00, 0xCC00},   // O
    {0x06C0, 0x8C40, 0x6C00, 0x4620},   // S
    {0x0E40, 0x4C40, 0x4E00, 0x4640},   // T
    {0x0C60, 0x4C80, 0xC600, 0x2640},   // Z
};
const uint32_t FIRST_REPEAT[3] = {180, 180, 60}, REPEAT[3] = {60, 60, 40};   // left, right, down (ms)

uint16_t board[ROWS];
int kind, rot, px, py, next_kind;
uint32_t points;
int cleared;
bool over, paused, quit_asked, down_blocked;
uint8_t prev_keys;
uint32_t next_fall, repeat_at[3];
int bag[7], bag_n;

int from_bag() {
    if (bag_n == 0) {
        for (int i = 0; i < 7; i++) bag[i] = i;
        for (int i = 6; i > 0; i--) {
            int j = esp_random() % (i + 1);
            int t = bag[i]; bag[i] = bag[j]; bag[j] = t;
        }
        bag_n = 7;
    }
    return bag[--bag_n];
}

bool hits(int k, int r, int x, int y) {
    uint16_t m = SHAPES[k][r];
    for (int i = 0; i < 16; i++) {
        if (!(m & (0x8000 >> i))) continue;
        int bx = x + i % 4, by = y + i / 4;
        if (bx < 0 || bx >= COLS || by >= ROWS) return true;
        if (by >= 0 && (board[by] >> bx & 1)) return true;
    }
    return false;
}

uint32_t interval() {
    int v = 450 - (cleared / 10) * 40;
    return v < 60 ? 60 : v;
}

void spawn() {
    kind = next_kind;
    next_kind = from_bag();
    rot = 0;
    px = 3;
    py = 0;
    if (hits(kind, rot, px, py)) over = true;
}

void lock() {
    uint16_t m = SHAPES[kind][rot];
    for (int i = 0; i < 16; i++) {
        int by = py + i / 4;
        if ((m & (0x8000 >> i)) && by >= 0 && by < ROWS) board[by] |= 1u << (px + i % 4);
    }
    int n = 0;
    for (int y = ROWS - 1; y >= 0;) {
        if (board[y] == FULL) {
            for (int k = y; k > 0; k--) board[k] = board[k - 1];
            board[0] = 0;
            n++;
        } else {
            y--;
        }
    }
    static const int WORTH[5] = {0, 100, 300, 500, 800};
    points += WORTH[n > 4 ? 4 : n] * (cleared / 10 + 1);
    cleared += n;
    down_blocked = true;       // a held DOWN doesn't carry into the next piece
    spawn();
}

bool step_down() {
    if (!hits(kind, rot, px, py + 1)) {
        py++;
        return true;
    }
    lock();
    return false;
}

void cell(Adafruit_GFX& d, int x, int y) { d.fillRect(1 + x * CELL, TOP + y * CELL, CELL - 1, CELL - 1, 1); }

}  // namespace

namespace tetris {

void start(uint32_t ms) {
    memset(board, 0, sizeof board);
    points = 0;
    cleared = 0;
    over = paused = quit_asked = down_blocked = false;
    prev_keys = 0xFF;
    bag_n = 0;
    next_kind = from_bag();
    spawn();
    next_fall = ms + interval();
}

bool tick(uint32_t ms, uint8_t keys) {
    uint8_t pressed = keys & ~prev_keys;
    prev_keys = keys;
    if (pressed & QUIT) {
        quit_asked = true;
        return true;
    }
    if (over) {
        if (!(pressed & DROP)) return false;
        start(ms);
        prev_keys = keys;
        quit_asked = false;
        return true;
    }
    if (pressed & PAUSE) {
        paused = !paused;
        next_fall = ms + interval();
        return true;
    }
    if (paused) return false;
    bool changed = false;
    if (pressed & ROTATE) {
        int r = (rot + 1) % 4;
        for (int dx : {0, -1, 1, -2, 2}) {
            if (hits(kind, r, px + dx, py)) continue;
            rot = r;
            px += dx;
            changed = true;
            break;
        }
    }
    if (!(keys & DOWN)) down_blocked = false;
    static const uint8_t KEY[3] = {LEFT, RIGHT, DOWN};
    for (int i = 0; i < 3 && !over; i++) {
        bool fire = false;
        if (pressed & KEY[i]) {
            fire = true;
            repeat_at[i] = ms + FIRST_REPEAT[i];
            if (i == 2) down_blocked = false;
        } else if ((keys & KEY[i]) && (int32_t)(ms - repeat_at[i]) >= 0 && !(i == 2 && down_blocked)) {
            fire = true;
            repeat_at[i] = ms + REPEAT[i];
        }
        if (!fire) continue;
        if (i < 2) {
            int dx = i == 0 ? -1 : 1;
            if (!hits(kind, rot, px + dx, py)) {
                px += dx;
                changed = true;
            }
        } else {
            if (step_down()) points++;
            next_fall = ms + interval();
            changed = true;
        }
    }
    if (!over && (pressed & DROP)) {
        while (!hits(kind, rot, px, py + 1)) {
            py++;
            points += 2;
        }
        lock();
        next_fall = ms + interval();
        changed = true;
    }
    if (!over && (int32_t)(ms - next_fall) >= 0) {
        step_down();
        next_fall = ms + interval();
        changed = true;
    }
    return changed;
}

bool quit() { return quit_asked; }

uint32_t score() { return points; }

void draw(Adafruit_GFX& d) {
    d.setTextSize(1);
    d.setTextColor(1);
    char b[8];
    snprintf(b, sizeof b, "%5lu", (unsigned long)(points > 99999 ? 99999 : points));
    d.setCursor(1, 0);
    d.print(b);
    uint16_t nm = SHAPES[next_kind][0];
    for (int i = 0; i < 16; i++)
        if (nm & (0x8000 >> i)) d.fillRect(12 + (i % 4) * 2, 10 + (i / 4) * 2, 2, 2, 1);
    d.drawFastHLine(0, TOP - 1, 32, 1);
    d.drawFastVLine(0, TOP, ROWS * CELL, 1);
    d.drawFastVLine(31, TOP, ROWS * CELL, 1);
    for (int y = 0; y < ROWS; y++)
        for (int x = 0; x < COLS; x++)
            if (board[y] >> x & 1) cell(d, x, y);
    if (!over) {
        uint16_t m = SHAPES[kind][rot];
        for (int i = 0; i < 16; i++)
            if ((m & (0x8000 >> i)) && py + i / 4 >= 0) cell(d, px + i % 4, py + i / 4);
    }
    if (over) {
        d.fillRect(1, 50, 30, 31, 0);
        d.setCursor(4, 52);
        d.print("GAME");
        d.setCursor(4, 61);
        d.print("OVER");
        d.setCursor(1, 72);
        d.print("OK=go");
    } else if (paused) {
        d.fillRect(1, 58, 30, 11, 0);
        d.setCursor(1, 60);
        d.print("PAUSE");
    }
}

}  // namespace tetris
