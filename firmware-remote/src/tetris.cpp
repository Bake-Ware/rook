#include "tetris.h"
#include <Adafruit_GFX.h>
#include <esp_random.h>

namespace {

#ifdef ROOK_BRIDGE
// The screen turned a quarter turn clockwise: 64 wide, 128 tall.
const int COLS = 10, ROWS = 20, CELL = 5, TOP = 27, X0 = 6, BASE_MS = 650;
#else
const int COLS = 10, ROWS = 36, CELL = 3, TOP = 20, X0 = 0, BASE_MS = 450;
#endif
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
int kind, rot, px, py, next_kind, held_kind;
uint32_t points;
int cleared;
bool over, paused, quit_asked, down_blocked, hold_used;
uint16_t prev_keys;
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
    int v = BASE_MS - (cleared / 10) * 40;
    return v < 60 ? 60 : v;
}

// The piece `k` at the top of the well.
void enter(int k) {
    kind = k;
    rot = 0;
    px = 3;
    py = 0;
    if (hits(kind, rot, px, py)) over = true;
}

void spawn() {
    int k = next_kind;
    next_kind = from_bag();
    hold_used = false;
    enter(k);
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

bool turn(int by) {
    int r = (rot + by + 4) % 4;
    for (int dx : {0, -1, 1, -2, 2}) {
        if (hits(kind, r, px + dx, py)) continue;
        rot = r;
        px += dx;
        return true;
    }
    return false;
}

void cell(Adafruit_GFX& d, int x, int y) { d.fillRect(X0 + 1 + x * CELL, TOP + y * CELL, CELL - 1, CELL - 1, 1); }

// Where the piece would land: its cells in outline (a dot when they are too small for one).
void ghost_cell(Adafruit_GFX& d, int x, int y) {
    if (CELL >= 4) d.drawRect(X0 + 1 + x * CELL, TOP + y * CELL, CELL - 1, CELL - 1, 1);
    else d.drawPixel(X0 + 1 + x * CELL + CELL / 2 - 1, TOP + y * CELL + CELL / 2 - 1, 1);
}

// A piece in small, for the next one and the one held.
void mini(Adafruit_GFX& d, int k, int x, int y, int size) {
    if (k < 0) return;
    uint16_t m = SHAPES[k][0];
    for (int i = 0; i < 16; i++)
        if (m & (0x8000 >> i)) d.fillRect(x + (i % 4) * size, y + (i / 4) * size, size, size, 1);
}

}  // namespace

namespace tetris {

void start(uint32_t ms) {
    memset(board, 0, sizeof board);
    points = 0;
    cleared = 0;
    over = paused = quit_asked = down_blocked = false;
    prev_keys = 0xFFFF;
    bag_n = 0;
    held_kind = -1;
    next_kind = from_bag();
    spawn();
    next_fall = ms + interval();
}

bool tick(uint32_t ms, uint16_t keys) {
    uint16_t pressed = keys & ~prev_keys;
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
    if ((pressed & ROTATE) && turn(1)) changed = true;
    if ((pressed & ROTATE_BACK) && turn(-1)) changed = true;
    if ((pressed & HOLD) && !hold_used) {
        int was = kind;
        if (held_kind < 0) spawn();
        else enter(held_kind);
        held_kind = was;
        hold_used = true;            // not again until a piece has landed
        next_fall = ms + interval();
        changed = true;
    }
    if (!(keys & DOWN)) down_blocked = false;
    static const uint16_t KEY[3] = {LEFT, RIGHT, DOWN};
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
#ifdef ROOK_BRIDGE
    uint8_t was = d.getRotation();
    d.setRotation((was + 1) & 3);          // a quarter turn clockwise: 64 wide, 128 tall
#endif
    d.setTextSize(1);
    d.setTextColor(1);
    char b[8];
    snprintf(b, sizeof b, "%5lu", (unsigned long)(points > 99999 ? 99999 : points));
#ifdef ROOK_BRIDGE
    // Score across the top; under it the piece held (left), the level, the piece to come (right).
    d.setCursor(17, 0);
    d.print(b);
    d.setCursor(1, 0);
    d.print("H");
    d.setCursor(57, 0);
    d.print("N");
    mini(d, held_kind, 1, 10, 3);
    mini(d, next_kind, 51, 10, 3);
    char lv[8];
    snprintf(lv, sizeof lv, "L%d", cleared / 10 + 1);
    d.setCursor(32 - (int)strlen(lv) * 3, 12);
    d.print(lv);
    if (hold_used) d.drawFastHLine(1, 23, 12, 1);     // the held piece can't be taken back yet
    d.drawFastHLine(X0, TOP + ROWS * CELL, COLS * CELL + 2, 1);
#else
    d.setCursor(1, 0);
    d.print(b);
    mini(d, next_kind, 12, 10, 2);
    d.drawFastHLine(0, TOP - 1, 32, 1);
#endif
    d.drawFastVLine(X0, TOP, ROWS * CELL, 1);
    d.drawFastVLine(X0 + COLS * CELL + 1, TOP, ROWS * CELL, 1);
    for (int y = 0; y < ROWS; y++)
        for (int x = 0; x < COLS; x++)
            if (board[y] >> x & 1) cell(d, x, y);
    if (!over) {
        uint16_t m = SHAPES[kind][rot];
        int gy = py;
        while (!hits(kind, rot, px, gy + 1)) gy++;
        for (int i = 0; i < 16; i++) {
            if (!(m & (0x8000 >> i))) continue;
            if (gy != py && gy + i / 4 >= 0) ghost_cell(d, px + i % 4, gy + i / 4);
        }
        for (int i = 0; i < 16; i++)
            if ((m & (0x8000 >> i)) && py + i / 4 >= 0) cell(d, px + i % 4, py + i / 4);
    }
#ifdef ROOK_BRIDGE
    if (over) {
        d.fillRect(8, 56, 48, 34, 0);
        d.drawRect(8, 56, 48, 34, 1);
        d.setCursor(20, 60);
        d.print("GAME");
        d.setCursor(20, 69);
        d.print("OVER");
        d.setCursor(17, 80);
        d.print("OK=go");
    } else if (paused) {
        d.fillRect(8, 66, 48, 14, 0);
        d.drawRect(8, 66, 48, 14, 1);
        d.setCursor(17, 69);
        d.print("PAUSE");
    }
    d.setRotation(was);
#else
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
#endif
}

}  // namespace tetris
