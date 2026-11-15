#include "swarm.h"
#include <Adafruit_GFX.h>
#include <esp_random.h>
#include "tetris.h"

namespace {

const int W = 64, H = 128;
const int COLS = 5, ROWS = 4, N = COLS * ROWS;
const int CELL_X = 11, CELL_Y = 10, FORM_X = (W - COLS * CELL_X) / 2 + 2, FORM_Y = 18;
const int FOE_W = 7, FOE_H = 6, SHIP_W = 9, SHIP_H = 6, SHIP_Y = 118;
const uint32_t STEP_MS = 33;

enum : uint8_t { GONE, FORM, DIVE, BACK };
struct Foe { uint8_t state, hp, boss; int16_t x, y, vx; };     // x, y: sixteenths of a pixel, top left
struct Shot { bool on; int16_t x, y; };                         // pixels
struct Boom { int16_t x, y; uint8_t t; };

Foe foes[N];
Shot shots[2], bombs[4];
Boom booms[4];
uint8_t star_x[12], star_y[12];
int ship_x = 0, lives = 0, stage = 1, sway = 0, sway_dir = 1, enter_y = 0, frame = 0, fire_wait = 0;
uint32_t points = 0, next_step = 0, dead_until = 0, safe_until = 0, clear_at = 0, next_dive = 0;
bool over = false, paused = false, quit_asked = false;
uint16_t prev_keys = 0;

// 7 wide (a byte a row, the high bit left), two frames each.
const uint8_t BUG[2][6] PROGMEM = {{0x44, 0x38, 0xFE, 0xBA, 0x82, 0x00}, {0x82, 0x7C, 0xFE, 0x38, 0x44, 0x00}};
const uint8_t BOSS[2][6] PROGMEM = {{0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x82}, {0x38, 0x7C, 0xD6, 0xFE, 0x54, 0x44}};
// 9 wide: two bytes a row.
const uint8_t SHIP[12] PROGMEM = {0x08, 0x00, 0x08, 0x00, 0x1C, 0x00, 0x9C, 0x80, 0xFF, 0x80, 0xDD, 0x80};

int rnd(int n) { return (int)(esp_random() % (uint32_t)n); }

int slot_x(int i) { return FORM_X + (i % COLS) * CELL_X + sway; }
int slot_y(int i) { return FORM_Y + (i / COLS) * CELL_Y + enter_y; }
int foe_x(int i) { return foes[i].state == FORM ? slot_x(i) : foes[i].x / 16; }
int foe_y(int i) { return foes[i].state == FORM ? slot_y(i) : foes[i].y / 16; }

void boom(int x, int y) {
    Boom* b = &booms[0];
    for (auto& c : booms)
        if (c.t < b->t) b = &c;
    *b = {(int16_t)x, (int16_t)y, 8};
}

void new_stage(uint32_t ms) {
    for (int i = 0; i < N; i++) foes[i] = {FORM, (uint8_t)(i < COLS ? 2 : 1), (uint8_t)(i < COLS), 0, 0, 0};
    for (auto& s : shots) s.on = false;
    for (auto& b : bombs) b.on = false;
    enter_y = -64;
    sway = 0;
    clear_at = 0;
    next_dive = ms + 2600;
}

void step(uint32_t ms, uint16_t keys) {
    frame++;
    for (int i = 0; i < 12; i++)
        if ((frame & 1) || i < 4) star_y[i] = (uint8_t)((star_y[i] + 1) % H);       // a few fall faster: nearer
    if (enter_y < 0) enter_y += 2;
    if (!(frame & 3)) {
        sway += sway_dir;
        if (sway >= 4 || sway <= -4) sway_dir = -sway_dir;
    }
    bool alive = (int32_t)(ms - dead_until) >= 0;
    if (alive) {
        if (keys & tetris::LEFT) ship_x -= 2;
        if (keys & tetris::RIGHT) ship_x += 2;
        ship_x = ship_x < 0 ? 0 : ship_x > W - SHIP_W ? W - SHIP_W : ship_x;
        if (keys & (tetris::DROP | tetris::ROTATE | tetris::ROTATE_BACK | tetris::DOWN)) {
            if (fire_wait > 0) fire_wait--;
            else
                for (auto& s : shots)
                    if (!s.on) {
                        s = {true, (int16_t)(ship_x + SHIP_W / 2), (int16_t)(SHIP_Y - 3)};
                        fire_wait = 7;           // held: one about every quarter second
                        break;
                    }
        } else {
            fire_wait = 0;
        }
    }
    for (auto& s : shots)
        if (s.on && (s.y -= 5) < 9) s.on = false;
    for (auto& b : bombs)
        if (b.on && (b.y += 2 + stage / 4) > H) b.on = false;

    // One leaves the formation and comes for the ship, more often each stage.
    if (alive && enter_y == 0 && !clear_at && (int32_t)(ms - next_dive) >= 0) {
        int at = rnd(N);
        for (int k = 0; k < N; k++) {
            int i = (at + k) % N;
            if (foes[i].state != FORM) continue;
            foes[i].x = (int16_t)(slot_x(i) * 16);
            foes[i].y = (int16_t)(slot_y(i) * 16);
            foes[i].vx = (int16_t)(rnd(2) ? 20 : -20);
            foes[i].state = DIVE;
            break;
        }
        int gap = 2300 - stage * 220;
        next_dive = ms + (gap < 500 ? 500 : gap) + rnd(700);
    }
    int top = 26 + stage * 2, fall = 26 + stage * 3;
    if (top > 44) top = 44;
    if (fall > 52) fall = 52;
    for (int i = 0; i < N; i++) {
        Foe& f = foes[i];
        if (f.state == DIVE) {
            f.vx += f.x / 16 < ship_x + 1 ? 3 : -3;
            f.vx = f.vx > top ? top : f.vx < -top ? -top : f.vx;
            f.x += f.vx;
            f.y += fall;
            if (f.x < 0) { f.x = 0; f.vx = -f.vx; }
            if (f.x > (W - FOE_W) * 16) { f.x = (W - FOE_W) * 16; f.vx = -f.vx; }
            if (alive && f.y / 16 < 92 && !rnd(34))
                for (auto& b : bombs)
                    if (!b.on) {
                        b = {true, (int16_t)(f.x / 16 + 3), (int16_t)(f.y / 16 + FOE_H)};
                        break;
                    }
            if (f.y / 16 > H) {                  // past the ship: round again, in from the top
                f.state = BACK;
                f.y = -12 * 16;
            }
        } else if (f.state == BACK) {
            int tx = slot_x(i) * 16, ty = slot_y(i) * 16;
            f.x += f.x < tx - 24 ? 24 : f.x > tx + 24 ? -24 : tx - f.x;
            f.y += 30;
            if (f.y >= ty) f.state = FORM;
        }
    }

    // What hit what.
    for (auto& s : shots) {
        if (!s.on) continue;
        for (int i = 0; i < N; i++) {
            Foe& f = foes[i];
            if (f.state == GONE) continue;
            int x = foe_x(i), y = foe_y(i);
            if (s.x < x - 1 || s.x > x + FOE_W || s.y > y + FOE_H || s.y + 4 < y) continue;
            s.on = false;
            if (--f.hp) break;                   // the top row takes two
            bool diving = f.state != FORM;
            points += f.boss ? (diving ? 400 : 150) : (diving ? 100 : 50);
            f.state = GONE;
            boom(x + 3, y + 3);
            break;
        }
    }
    if (alive && (int32_t)(ms - safe_until) >= 0) {
        bool hit = false;
        for (auto& b : bombs)
            if (b.on && b.x >= ship_x && b.x < ship_x + SHIP_W && b.y + 2 >= SHIP_Y + 1 && b.y <= SHIP_Y + SHIP_H) {
                b.on = false;
                hit = true;
            }
        for (int i = 0; i < N; i++) {
            Foe& f = foes[i];
            if (f.state != DIVE) continue;
            int x = f.x / 16, y = f.y / 16;
            if (x + FOE_W <= ship_x || x >= ship_x + SHIP_W || y + FOE_H <= SHIP_Y + 1 || y >= SHIP_Y + SHIP_H) continue;
            f.state = GONE;
            boom(x + 3, y + 3);
            hit = true;
        }
        if (hit) {
            boom(ship_x + 4, SHIP_Y + 3);
            boom(ship_x + 1, SHIP_Y + 1);
            for (auto& b : bombs) b.on = false;
            dead_until = ms + 1100;
            safe_until = ms + 2900;
            if (--lives <= 0) over = true;
        }
    }
    bool any = false;
    for (auto& f : foes) any = any || f.state != GONE;
    if (!any) {
        if (!clear_at) clear_at = ms + 1000;
        else if ((int32_t)(ms - clear_at) >= 0) {
            points += 500;
            stage++;
            new_stage(ms);
        }
    }
    for (auto& b : booms)
        if (b.t) b.t--;
}

void centred(Adafruit_GFX& d, const char* t, int y) {
    d.setCursor((W - (int)strlen(t) * 6) / 2 + 1, y);
    d.print(t);
}

}  // namespace

namespace swarm {

void start(uint32_t ms) {
    points = 0;
    lives = 3;
    stage = 1;
    ship_x = (W - SHIP_W) / 2;
    over = paused = quit_asked = false;
    dead_until = safe_until = 0;
    fire_wait = 7;                   // the key that started it is not a shot
    prev_keys = 0xFFFF;
    for (int i = 0; i < 12; i++) {
        star_x[i] = (uint8_t)rnd(W);
        star_y[i] = (uint8_t)rnd(H);
    }
    for (auto& b : booms) b.t = 0;
    new_stage(ms);
    next_step = ms;
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
    if (pressed & (tetris::PAUSE | tetris::HOLD)) {
        paused = !paused;
        next_step = ms;
        return true;
    }
    if (paused || (int32_t)(ms - next_step) < 0) return false;
    next_step = ms - next_step > 200 ? ms + STEP_MS : next_step + STEP_MS;      // far behind: don't race to catch up
    step(ms, keys);
    return true;
}

bool quit() { return quit_asked; }

uint32_t score() { return points; }

void draw(Adafruit_GFX& d) {
    uint8_t was = d.getRotation();
    d.setRotation((was + 1) & 3);            // a quarter turn clockwise: 64 wide, 128 tall
    d.setTextSize(1);
    d.setTextWrap(false);
    d.setTextColor(1);
    uint32_t ms = millis();
    for (int i = 0; i < 12; i++) d.drawPixel(star_x[i], star_y[i], 1);
    d.fillRect(0, 0, W, 9, 0);
    d.setCursor(0, 0);
    d.print((unsigned long)points);
    for (int i = 0; i < lives - 1 && i < 3; i++) {        // the ships in reserve
        int x = W - 7 - i * 8;
        d.drawFastVLine(x + 3, 1, 2, 1);
        d.drawFastHLine(x + 1, 3, 5, 1);
        d.drawFastHLine(x, 4, 7, 1);
    }
    int f2 = (frame / 10) & 1;
    for (int i = 0; i < N; i++) {
        if (foes[i].state == GONE) continue;
        int wing = foes[i].state == FORM ? f2 : (frame / 3) & 1;      // diving: wings faster
        d.drawBitmap(foe_x(i), foe_y(i), foes[i].boss ? BOSS[wing] : BUG[wing], FOE_W, FOE_H, 1);
        if (foes[i].boss && foes[i].hp < 2) d.drawPixel(foe_x(i) + 3, foe_y(i) + 2, 0);      // hit once: it shows
    }
    bool alive = (int32_t)(ms - dead_until) >= 0, safe = (int32_t)(ms - safe_until) < 0;
    if (!over && alive && !(safe && (frame & 2))) d.drawBitmap(ship_x, SHIP_Y, SHIP, SHIP_W, SHIP_H, 1);
    for (auto& s : shots)
        if (s.on) d.drawFastVLine(s.x, s.y, 4, 1);
    for (auto& b : bombs)
        if (b.on) {
            d.drawPixel(b.x + ((b.y / 2) & 1), b.y, 1);
            d.drawPixel(b.x + (((b.y / 2) + 1) & 1), b.y + 1, 1);
            d.drawPixel(b.x + ((b.y / 2) & 1), b.y + 2, 1);
        }
    for (auto& b : booms)
        if (b.t) {
            int r = (8 - b.t) / 2 + 1;
            d.drawCircle(b.x, b.y, r, 1);
            if (b.t > 4) d.drawPixel(b.x, b.y, 1);
        }
    if (over) {
        d.fillRect(4, 44, W - 8, 44, 0);
        d.drawRoundRect(4, 44, W - 8, 44, 4, 1);
        centred(d, "GAME", 49);
        centred(d, "OVER", 58);
        char b[12];
        snprintf(b, sizeof b, "%lu", (unsigned long)points);
        centred(d, b, 69);
        centred(d, "OK:again", 78);
    } else if (paused) {
        d.fillRect(8, 54, W - 16, 15, 0);
        d.drawRoundRect(8, 54, W - 16, 15, 4, 1);
        centred(d, "PAUSED", 58);
    } else if (enter_y < 0) {
        char b[12];
        snprintf(b, sizeof b, "STAGE %d", stage);
        centred(d, b, 74);
    }
    d.setRotation(was);
}

}  // namespace swarm
