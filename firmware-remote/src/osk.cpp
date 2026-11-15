#include "osk.h"
#include <Adafruit_GFX.h>

namespace {

const int CELLS = 36, ACTIONS = 4, MAX_TEXT = 63;
#ifdef ROOK_BRIDGE
// 128x64 landscape: 12 across, 3 rows of characters, the actions in a row below.
const int COLS = 12, CW = 10, X0 = 5, Y0 = 14, RP = 10, ACTION_ROWS = 1;
#else
// 32x128 upright: 5 across, 8 rows of characters, the actions one under another.
const int COLS = 5, CW = 6, X0 = 1, Y0 = 20, RP = 9, ACTION_ROWS = 4;
#endif
const int GRID_ROWS = (CELLS + COLS - 1) / COLS, ROWS = GRID_ROWS + ACTION_ROWS;
const char* const PAGES[3] = {
    "abcdefghijklmnopqrstuvwxyz0123456789",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "!@#$%^&*()-_=+[]{};:'\",.<>/?\\|`~    ",
};
const char* const NEXT_PAGE[3] = {"ABC", "#+=", "abc"};

std::string title, typed;
int page = 0, row = 0, col = 0;

int row_len(int r) {
    if (r < GRID_ROWS) {
        int left = CELLS - r * COLS;
        return left < COLS ? left : COLS;
    }
    return ACTION_ROWS == 1 ? ACTIONS : 1;
}

int action_at(int r, int c) { return ACTION_ROWS == 1 ? c : r - GRID_ROWS; }

const char* action_label(int a) {
    switch (a) {
        case 0: return NEXT_PAGE[page];
        case 1: return "spc";
        case 2: return "del";
    }
    return "OK";
}

void to_row(int r) {
    int was = row_len(row), now = row_len(r);
    col = (col * now + was / 2) / was;
    if (col >= now) col = now - 1;
    row = r;
}

}  // namespace

namespace osk {

void begin(const std::string& t, const std::string& initial) {
    title = t;
    typed = initial;
    page = row = col = 0;
}

int key(const std::string& name) {
    if (name == "up") to_row((row + ROWS - 1) % ROWS);
    else if (name == "down") to_row((row + 1) % ROWS);
    else if (name == "left") col = (col + row_len(row) - 1) % row_len(row);
    else if (name == "right") col = (col + 1) % row_len(row);
    else if (name == "back") {
        if (typed.empty()) return -1;
        typed.pop_back();
    } else if (name == "input" || name == "home") {
        return -1;
    } else if (name == "ok") {
        if (row < GRID_ROWS) {
            if ((int)typed.size() < MAX_TEXT) typed += PAGES[page][row * COLS + col];
        } else {
            switch (action_at(row, col)) {
                case 0: page = (page + 1) % 3; break;
                case 1: if ((int)typed.size() < MAX_TEXT) typed += ' '; break;
                case 2: if (!typed.empty()) typed.pop_back(); break;
                default: return 1;
            }
        }
    }
    return 0;
}

const std::string& text() { return typed; }

void draw(Adafruit_GFX& d) {
    int w = d.width();
    d.setTextSize(1);
    d.setTextWrap(false);
    d.setTextColor(1);
    // What has been typed (its tail), with a cursor.
    std::string shown = typed + "_";
#ifdef ROOK_BRIDGE
    if (shown.size() > 21) shown = shown.substr(shown.size() - 21);
    d.setCursor(1, 1);
    d.print(shown.c_str());
    d.drawFastHLine(0, 10, w, 1);
#else
    if (shown.size() > 10) shown = shown.substr(shown.size() - 10);
    d.setCursor(1, 0);
    d.print(shown.substr(0, 5).c_str());
    if (shown.size() > 5) {
        d.setCursor(1, 9);
        d.print(shown.substr(5).c_str());
    }
    d.drawFastHLine(0, 18, w, 1);
#endif
    for (int r = 0; r < GRID_ROWS; r++)
        for (int c = 0; c < row_len(r); c++) {
            int x = X0 + c * CW, y = Y0 + r * RP;
            bool at = r == row && c == col;
            if (at) d.fillRect(x - (CW - 5) / 2 - 1, y - 1, CW + 1, RP, 1);
            d.setTextColor(at ? 0 : 1);
            d.setCursor(x, y);
            d.write(PAGES[page][r * COLS + c]);
        }
    for (int a = 0; a < ACTIONS; a++) {
        bool at = row >= GRID_ROWS && action_at(row, col) == a;
        const char* label = action_label(a);
#ifdef ROOK_BRIDGE
        int x = a * 32, y = Y0 + GRID_ROWS * RP + 6;
        if (at) d.fillRect(x, y - 2, 31, 11, 1);
        else d.drawRect(x, y - 2, 31, 11, 1);
        d.setTextColor(at ? 0 : 1);
        d.setCursor(x + (31 - (int)strlen(label) * 6) / 2 + 1, y);
#else
        int y = Y0 + GRID_ROWS * RP + a * RP;
        if (at) d.fillRect(0, y - 1, w, RP, 1);
        d.setTextColor(at ? 0 : 1);
        d.setCursor(1 + (30 - (int)strlen(label) * 6) / 2, y);
#endif
        d.print(label);
    }
    d.setTextColor(1);
}

}  // namespace osk
