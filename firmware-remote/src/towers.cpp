#include "towers.h"
#include <Adafruit_GFX.h>
#include <algorithm>
#include <esp_random.h>
#include "ink.h"
#include "tetris.h"

namespace {

const int GW = 16, GH = 7, T = 8, TOP = 8;       // the field: 16 x 7 tiles of 8 pixels under a line of figures
const int NF = 18, CELLS = GW * GH;
const uint32_t STEP_MS = 33;

enum : uint8_t { NONE, GUN, BLAST, FROST, ROAD = 255 };
struct Kind { const char* tag; uint8_t cost, range, dmg, rate; };
// A gun hits one, often. A blast hits everything near its mark, slowly. Frost
// hurts nothing: whatever is in its reach goes at half speed.
const Kind KINDS[4] = {{"", 0, 0, 0, 0}, {"GUN", 10, 20, 1, 10}, {"BLAST", 25, 18, 2, 28}, {"FROST", 20, 17, 0, 0}};

struct Foe { bool on, slow; uint8_t kind; int16_t hp, full, x, y; int32_t d; };     // d: how far along the road, sixteenths of a pixel
struct Flash { int16_t x0, y0, x1, y1; uint8_t t, r; };                              // r: a blast's ring; 0: a gun's line

// The road, corner to corner, in tiles: in at the left, out at the right.
const int8_t WP[][2] = {{-1, 1}, {13, 1}, {13, 3}, {2, 3}, {2, 5}, {16, 5}};
const int NWP = sizeof WP / sizeof WP[0];

uint8_t cell[CELLS], lvl[CELLS], cool[CELLS];
Foe foes[NF];
Flash flashes[6];
int cx = 7, cy = 2, sel = GUN, gold = 0, lives = 0, wave = 0, to_spawn = 0, spawn_wait = 0, frame = 0, kills = 0;
uint32_t next_step = 0, wave_at = 0, rep_at = 0;
bool over = false, paused = false, quit_asked = false, in_wave = false;
uint16_t prev_keys = 0;

int wp_x(int i) { return WP[i][0] * T + T / 2; }
int wp_y(int i) { return TOP + WP[i][1] * T + T / 2; }

// Where d along the road is. False: past its end.
bool place(Foe& f) {
    int32_t left = f.d;
    for (int i = 0; i + 1 < NWP; i++) {
        int ax = wp_x(i), ay = wp_y(i), bx = wp_x(i + 1), by = wp_y(i + 1);
        int32_t len = (abs(bx - ax) + abs(by - ay)) * 16;
        if (left <= len) {
            f.x = (int16_t)(ax + (bx - ax) * left / len);
            f.y = (int16_t)(ay + (by - ay) * left / len);
            return true;
        }
        left -= len;
    }
    return false;
}

void flash(int x0, int y0, int x1, int y1, int r) {
    Flash* f = &flashes[0];
    for (auto& c : flashes)
        if (c.t < f->t) f = &c;
    *f = {(int16_t)x0, (int16_t)y0, (int16_t)x1, (int16_t)y1, 3, (uint8_t)r};
}

void hurt(Foe& f, int dmg) {
    if (!f.on) return;
    f.hp -= dmg;
    if (f.hp > 0) return;
    f.on = false;
    kills++;
    gold += f.kind == 2 ? 15 + wave : 2 + wave / 4;
}

bool near(int ax, int ay, int bx, int by, int r) {
    int dx = ax - bx, dy = ay - by;
    return dx * dx + dy * dy <= r * r;
}

void begin_wave() {
    wave++;
    in_wave = true;
    to_spawn = wave % 5 == 0 ? 2 + wave / 5 : 6 + wave;          // every fifth: a few big ones
    if (to_spawn > 40) to_spawn = 40;
    spawn_wait = 0;
}

void spawn() {
    for (auto& f : foes) {
        if (f.on) continue;
        int base = 3 + wave * 2 + wave * wave / 6;
        uint8_t kind = wave % 5 == 0 ? 2 : wave % 3 == 0 ? 1 : 0;    // big; quick and frail; plain
        int hp = kind == 2 ? base * 6 : kind == 1 ? base / 2 + 1 : base;
        f = {true, false, kind, (int16_t)hp, (int16_t)hp, 0, 0, 0};
        place(f);
        return;
    }
}

void step(uint32_t ms) {
    frame++;
    if (!in_wave) {
        if ((int32_t)(ms - wave_at) >= 0) begin_wave();
    } else if (to_spawn && --spawn_wait <= 0) {
        spawn();
        to_spawn--;
        spawn_wait = wave % 5 == 0 ? 45 : wave % 3 == 0 ? 12 : 20;
    }
    for (auto& f : foes) f.slow = false;
    for (int i = 0; i < CELLS; i++) {
        if (cell[i] != FROST) continue;
        int tx = (i % GW) * T + T / 2, ty = TOP + (i / GW) * T + T / 2, r = KINDS[FROST].range + (lvl[i] - 1) * 4;
        for (auto& f : foes)
            if (f.on && near(f.x, f.y, tx, ty, r)) f.slow = true;
    }
    for (auto& f : foes) {
        if (!f.on) continue;
        int v = f.kind == 2 ? 6 : 9 + wave / 3;
        if (v > 16) v = 16;
        if (f.kind == 1) v = v * 17 / 10;
        f.d += f.slow ? v / 2 : v;
        if (!place(f)) {                         // through: it costs lives
            f.on = false;
            lives -= f.kind == 2 ? 3 : 1;
            if (lives <= 0) {
                lives = 0;
                over = true;
            }
        }
    }
    for (int i = 0; i < CELLS; i++) {
        if (cell[i] != GUN && cell[i] != BLAST) continue;
        if (cool[i]) {
            cool[i]--;
            continue;
        }
        const Kind& k = KINDS[cell[i]];
        int tx = (i % GW) * T + T / 2, ty = TOP + (i / GW) * T + T / 2, r = k.range + (lvl[i] - 1) * 3;
        Foe* mark = nullptr;                     // the one in reach that is furthest along
        for (auto& f : foes)
            if (f.on && near(f.x, f.y, tx, ty, r) && (!mark || f.d > mark->d)) mark = &f;
        if (!mark) continue;
        int dmg = k.dmg + lvl[i] - 1, mx = mark->x, my = mark->y;
        if (cell[i] == GUN) {
            hurt(*mark, dmg);
            flash(tx, ty, mx, my, 0);
        } else {
            for (auto& f : foes)
                if (f.on && near(f.x, f.y, mx, my, 9)) hurt(f, dmg);
            flash(tx, ty, mx, my, 8);
        }
        cool[i] = (uint8_t)(k.rate - (lvl[i] - 1) * 2);
    }
    if (in_wave && !to_spawn) {
        bool any = false;
        for (auto& f : foes) any = any || f.on;
        if (!any) {                              // a wave seen off: a little gold, a breath
            in_wave = false;
            gold += 5 + wave;
            wave_at = ms + 6000;
        }
    }
    for (auto& f : flashes)
        if (f.t) f.t--;
}

void boxed(Adafruit_GFX& d, const char* a, const char* b) {
    int w = (int)std::max(strlen(a), strlen(b)) * 6 + 10, x = (128 - w) / 2;
    d.fillRect(x, 22, w, 26, 0);
    d.drawRoundRect(x, 22, w, 26, 4, 1);
    d.setCursor((128 - (int)strlen(a) * 6) / 2, 26);
    d.print(a);
    d.setCursor((128 - (int)strlen(b) * 6) / 2, 36);
    d.print(b);
}

}  // namespace

namespace towers {

void start(uint32_t ms) {
    memset(cell, 0, sizeof cell);
    memset(lvl, 0, sizeof lvl);
    memset(cool, 0, sizeof cool);
    for (int i = 0; i + 1 < NWP; i++) {              // the road's tiles can't be built on
        int x = WP[i][0], y = WP[i][1], bx = WP[i + 1][0], by = WP[i + 1][1];
        for (;;) {
            if (x >= 0 && x < GW && y >= 0 && y < GH) cell[y * GW + x] = ROAD;
            if (x == bx && y == by) break;
            x += (bx > x) - (bx < x);
            y += (by > y) - (by < y);
        }
    }
    for (auto& f : foes) f.on = false;
    for (auto& f : flashes) f.t = 0;
    cx = 7;
    cy = 2;
    sel = GUN;
    gold = 30;
    lives = 10;
    wave = kills = 0;
    over = paused = quit_asked = in_wave = false;
    wave_at = ms + 8000;
    next_step = ms;
    prev_keys = 0xFFFF;
}

bool tick(uint32_t ms, uint16_t keys) {
    uint16_t pressed = keys & ~prev_keys;
    const uint16_t DIRS = tetris::LEFT | tetris::RIGHT | tetris::ROTATE | tetris::DOWN;
    if (pressed & DIRS) rep_at = ms + 320;
    else if ((keys & DIRS) && (int32_t)(ms - rep_at) >= 0) {       // held: the cursor keeps going
        pressed |= keys & DIRS;
        rep_at = ms + 110;
    }
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
    if (pressed & tetris::PAUSE) {
        paused = !paused;
        next_step = ms;
        return true;
    }
    bool changed = false;
    if (pressed & tetris::LEFT) cx--;
    if (pressed & tetris::RIGHT) cx++;
    if (pressed & tetris::ROTATE) cy--;
    if (pressed & tetris::DOWN) cy++;
    cx = cx < 0 ? 0 : cx >= GW ? GW - 1 : cx;
    cy = cy < 0 ? 0 : cy >= GH ? GH - 1 : cy;
    if (pressed & tetris::ROTATE_BACK) sel = sel % 3 + 1;
    if (pressed & tetris::DROP) {
        int i = cy * GW + cx;
        if (cell[i] == NONE && gold >= KINDS[sel].cost) {
            gold -= KINDS[sel].cost;
            cell[i] = (uint8_t)sel;
            lvl[i] = 1;
            cool[i] = 0;
        } else if (cell[i] >= GUN && cell[i] <= FROST && lvl[i] < 3 && gold >= KINDS[cell[i]].cost * lvl[i]) {
            gold -= KINDS[cell[i]].cost * lvl[i];
            lvl[i]++;
        }
    }
    if ((pressed & tetris::HOLD) && !in_wave && !paused) {          // don't wait: a little gold for the nerve
        wave_at = ms;
        gold += 2;
    }
    if (pressed) changed = true;
    if (paused || (int32_t)(ms - next_step) < 0) return changed;
    next_step = ms - next_step > 200 ? ms + STEP_MS : next_step + STEP_MS;
    step(ms);
    return true;
}

bool quit() { return quit_asked; }

uint32_t score() { return (uint32_t)wave * 100 + kills; }

void draw(Adafruit_GFX& d) {
    d.setTextSize(1);
    d.setTextWrap(false);
    d.setTextColor(1);
    uint32_t ms = millis();
    char t[24];
    // The line of figures: gold, lives, the wave (or how long until it), and
    // what OK would do where the cursor is.
    int here = cell[cy * GW + cx];
    snprintf(t, sizeof t, "$%d", gold);
    d.setCursor(0, 0);
    d.setTextColor(INK(255, 215, 0));
    d.print(t);
    d.setTextColor(1);
    const uint16_t red = INK(255, 60, 90);
    d.fillTriangle(31, 2, 37, 2, 34, 6, red);        // a heart, near enough
    d.fillRect(31, 1, 3, 2, red);
    d.fillRect(35, 1, 3, 2, red);
    snprintf(t, sizeof t, "%d", lives);
    d.setCursor(40, 0);
    d.print(t);
    if (in_wave) snprintf(t, sizeof t, "W%d", wave);
    else snprintf(t, sizeof t, "W%d:%d", wave + 1, (int)((int32_t)(wave_at - ms) > 0 ? (wave_at - ms) / 1000 + 1 : 0));
    d.setCursor(56, 0);
    d.print(t);
    if (here >= GUN && here <= FROST) {
        int l = lvl[cy * GW + cx];
        if (l >= 3) snprintf(t, sizeof t, "MAX");
        else snprintf(t, sizeof t, "UP %d", KINDS[here].cost * l);
    } else {
        snprintf(t, sizeof t, "%s %d", KINDS[sel].tag, KINDS[sel].cost);
    }
    d.setCursor(128 - (int)strlen(t) * 6, 0);
    d.print(t);

    for (int i = 0; i + 1 < NWP; i++) {              // the road: a dotted line down its middle
        int x = wp_x(i), y = wp_y(i), bx = wp_x(i + 1), by = wp_y(i + 1), n = 0;
        while (x != bx || y != by) {
            if (!(n++ & 3) && x >= 0 && x < 128) d.drawPixel(x, y, INK(110, 100, 80));
            x += (bx > x) - (bx < x);
            y += (by > y) - (by < y);
        }
    }
    for (int i = 0; i < CELLS; i++) {
        if (cell[i] < GUN || cell[i] > FROST) continue;
        int x = (i % GW) * T, y = TOP + (i / GW) * T;
        if (cell[i] == GUN) {
            const uint16_t c = INK(80, 220, 255);
            d.drawCircle(x + 4, y + 4, 3, c);
            d.drawPixel(x + 4, y + 4, c);
        } else if (cell[i] == BLAST) {
            const uint16_t c = INK(255, 150, 40);
            d.drawRect(x + 1, y + 1, 7, 7, c);
            d.fillRect(x + 3, y + 3, 3, 3, c);
        } else {
            const uint16_t c = INK(170, 205, 255);
            d.drawFastHLine(x + 1, y + 4, 7, c);
            d.drawFastVLine(x + 4, y + 1, 7, c);
            d.drawLine(x + 2, y + 2, x + 6, y + 6, c);
            d.drawLine(x + 6, y + 2, x + 2, y + 6, c);
        }
        for (int p = 1; p < lvl[i]; p++) d.drawPixel(x + (p == 1 ? 0 : 7), y, 1);     // its level: a dot a corner
    }
    for (auto& f : foes) {
        if (!f.on) continue;
        const uint16_t fc = f.slow ? INK(150, 190, 255) : f.kind == 2 ? INK(255, 70, 70) : f.kind == 1 ? INK(255, 230, 70) : INK(90, 230, 90);
        if (f.kind == 2) {
            d.fillRect(f.x - 3, f.y - 3, 7, 7, fc);
            d.drawPixel(f.x - 1, f.y - 1, 0);
            d.drawPixel(f.x + 1, f.y - 1, 0);
            d.drawFastHLine(f.x - 3, f.y - 5, std::max(1, 7 * f.hp / f.full), 1);
        } else if (f.kind == 1) {
            d.drawFastHLine(f.x - 2, f.y, 5, fc);
            d.drawFastVLine(f.x, f.y - 2, 5, fc);
            d.drawRect(f.x - 1, f.y - 1, 3, 3, fc);
        } else {
            d.drawRect(f.x - 2, f.y - 2, 5, 5, fc);
            if (f.hp * 2 > f.full) d.drawPixel(f.x, f.y, fc);       // hollow once half gone
        }
    }
    for (auto& f : flashes) {
        if (!f.t) continue;
        if (f.r) d.drawCircle(f.x1, f.y1, f.r - f.t * 2, INK(255, 170, 50));
        else d.drawLine(f.x0, f.y0, f.x1, f.y1, INK(255, 255, 170));
    }
    // The cursor: corners always, the whole square on the beat.
    int x = cx * T, y = TOP + cy * T;
    if ((ms / 250) & 1) d.drawRect(x, y, T, T, 1);
    else {
        for (int px : {x, x + T - 1})
            for (int py : {y, y + T - 1}) {
                d.drawPixel(px, py, 1);
                d.drawPixel(px == x ? px + 1 : px - 1, py, 1);
                d.drawPixel(px, py == y ? py + 1 : py - 1, 1);
            }
    }
    if (over) {
        snprintf(t, sizeof t, "wave %d  OK:again", wave);
        boxed(d, "GAME OVER", t);
    } else if (paused) {
        boxed(d, "PAUSED", "play: go on");
    }
}

}  // namespace towers
