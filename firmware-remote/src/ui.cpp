#include "ui.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Wire.h>
#include <algorithm>
#include "tetris.h"

namespace {

#ifdef ROOK_BRIDGE
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
#else
Adafruit_SSD1306 oled(128, 32, &Wire, -1);
#endif
bool ok = false, dirty = true;
bool anim = false;             // the last frame had a scrolling line: keep redrawing
uint32_t last_draw = 0, scroll_t0 = 0;
SemaphoreHandle_t mu;
std::string status[4];
std::string agent_text;
uint32_t agent_until = 0;      // 0 = until replaced; set when agent_text is shown
int agent_top = 0;             // the first line of agent_text on screen (it scrolls when long)
bool agent_on = false, agent_big = false;
std::string note;
uint32_t note_until = 0;
bool ask_on = false;
std::string ask_prompt;
std::vector<std::string> ask_opts;
int ask_sel = 0;
bool menu_on = false;
std::string menu_title;
std::vector<std::string> menu_items;
int menu_sel = 0;
bool game_on = false;
void (*canvas_fn)(Adafruit_GFX&) = nullptr;
std::string canvas_name;
bool dark = false;             // the panel is switched off (the remote was left alone)
int menu_first = 0, menu_hl = -1, menu_was_first = 0;   // the menu's first row on screen; where its highlight is, sliding
#ifdef ROOK_BRIDGE
const int W = 128, H = 64, COLS = 21, ROWS = 8, PITCH = 10;     // 0.96" 128x64, landscape
#else
const int W = 32, H = 128, COLS = 5, ROWS = 16, PITCH = 10;
#endif

// Lines of at most COLS characters: broken at a space, or after - or _, when
// one is in reach, else mid-word.
std::vector<std::string> wrap(const std::string& t, size_t maxlines) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i <= t.size() && out.size() < maxlines) {
        size_t nl = t.find('\n', i);
        std::string para = t.substr(i, nl == std::string::npos ? std::string::npos : nl - i);
        i = nl == std::string::npos ? t.size() + 1 : nl + 1;
        if (para.empty()) {
            out.push_back("");
            continue;
        }
        while (!para.empty() && out.size() < maxlines) {
            if ((int)para.size() <= COLS) {
                out.push_back(para);
                break;
            }
            int cut = COLS, skip = 0;
            for (int k = COLS; k > 1; k--) {
                if (para[k] == ' ') { cut = k; skip = 1; break; }
                if (para[k - 1] == '-' || para[k - 1] == '_') { cut = k; break; }
            }
            out.push_back(para.substr(0, cut));
            para.erase(0, cut + skip);
        }
    }
    return out;
}

// One line at y; one too wide for the screen slides left and back.
void line(const std::string& s, int y, uint32_t ms) {
    int over = (int)s.size() * 6 - 1 - (W - 1);
    int x = 1;
    if (over > 0) {
        anim = true;
        int p = ((ms - scroll_t0) / 60) % (over + 40);
        x = 1 - (p < 20 ? 0 : (p - 20 < over ? p - 20 : over));
    }
    oled.setCursor(x, y);
    oled.print(s.c_str());
}

void lines(const std::vector<std::string>& l, int y, int pitch) {
    for (size_t i = 0; i < l.size(); i++) {
        oled.setCursor(1, y + i * pitch);
        oled.print(l[i].c_str());
    }
}

// Options from y down, the chosen one inverted (and sliding when too long).
void list(int y0, const std::vector<std::string>& opts, int sel, uint32_t ms) {
    int rows = (H - y0) / PITCH;
    int first = sel - rows + 1 < 0 ? 0 : sel - rows + 1;
    for (int r = 0; r < rows && first + r < (int)opts.size(); r++) {
        int i = first + r, y = y0 + r * PITCH;
        if (i == sel) {
            oled.fillRect(0, y, W, PITCH - 1, SSD1306_WHITE);
            oled.setTextColor(SSD1306_BLACK);
            line(opts[i], y + 1, ms);
            oled.setTextColor(SSD1306_WHITE);
        } else {
            oled.setCursor(1, y + 1);
            oled.print(opts[i].substr(0, COLS).c_str());
        }
    }
}

// 8x8 pictures for the menu's rows, picked by what the row says.
const uint8_t IC_TV[] PROGMEM = {0x7E, 0x81, 0x81, 0x81, 0x81, 0x7E, 0x18, 0x3C};
const uint8_t IC_GAME[] PROGMEM = {0x00, 0x7E, 0xFF, 0xDB, 0xFF, 0xE7, 0xC3, 0x00};
const uint8_t IC_WIFI[] PROGMEM = {0x3C, 0x42, 0x99, 0x24, 0x00, 0x18, 0x18, 0x00};
const uint8_t IC_BT[] PROGMEM = {0x10, 0x18, 0x54, 0x38, 0x38, 0x54, 0x18, 0x10};
const uint8_t IC_FIND[] PROGMEM = {0x38, 0x44, 0x82, 0x82, 0x44, 0x3C, 0x06, 0x03};
const uint8_t IC_CHAT[] PROGMEM = {0x7E, 0x81, 0x81, 0x81, 0x7E, 0x30, 0x20, 0x00};
const uint8_t IC_X[] PROGMEM = {0x00, 0x42, 0x24, 0x18, 0x18, 0x24, 0x42, 0x00};
const uint8_t IC_DOT[] PROGMEM = {0x00, 0x3C, 0x7E, 0x7E, 0x7E, 0x7E, 0x3C, 0x00};

const uint8_t* icon_for(const std::string& label) {
    std::string l;
    for (char c : label) l += (char)tolower((unsigned char)c);
    auto has = [&](const char* w) { return l.find(w) != std::string::npos; };
    if (has("tetris") || has("game")) return IC_GAME;
    if (has("wi-fi") || has("wifi")) return IC_WIFI;
    if (has("forget")) return IC_X;
    if (has(" bt") || has("bluetooth")) return IC_BT;
    if (has("find") || has("scan")) return IC_FIND;
    if (has("assistant")) return IC_CHAT;
    if (has("tv") || has("roku") || has("cast")) return IC_TV;
    return IC_DOT;
}

// The menu: a title bar with where you are in the list, rows with a picture
// each, a highlight that slides to the row chosen, and a mark down the right
// edge when there is more than fits. (The narrow screen keeps the plain list.)
void menu_draw(uint32_t ms) {
    if (W < 100) {
        int y = 0;
        if (!menu_title.empty()) {
            oled.setCursor(1, 0);
            oled.print(menu_title.substr(0, COLS).c_str());
            oled.drawFastHLine(0, 9, W, SSD1306_WHITE);
            y = 12;
        }
        list(y, menu_items, menu_sel, ms);
        return;
    }
    const int BAR = 11, Y0 = 13, ROW = 12;
    int n = (int)menu_items.size(), rows = (H - Y0) / ROW;
    if (menu_sel < menu_first) menu_first = menu_sel;
    if (menu_sel >= menu_first + rows) menu_first = menu_sel - rows + 1;
    menu_first = std::max(0, std::min(menu_first, n - rows));
    oled.fillRect(0, 0, W, BAR, SSD1306_WHITE);
    oled.setTextColor(SSD1306_BLACK);
    oled.setCursor(3, 2);
    oled.print(menu_title.substr(0, 12).c_str());
    char where[12];
    snprintf(where, sizeof where, "%d/%d", menu_sel + 1, n);
    oled.setCursor(W - 3 - (int)strlen(where) * 6, 2);
    oled.print(where);
    oled.setTextColor(SSD1306_WHITE);
    for (int r = 0; r < rows && menu_first + r < n; r++) {
        const std::string& item = menu_items[menu_first + r];
        int y = Y0 + r * ROW;
        oled.drawBitmap(3, y + 1, icon_for(item), 8, 8, SSD1306_WHITE);
        oled.setCursor(15, y + 2);
        oled.print(item.substr(0, (W - 4 - 16) / 6).c_str());
    }
    int target = Y0 + (menu_sel - menu_first) * ROW;
    if (menu_hl < 0 || menu_first != menu_was_first) menu_hl = target;     // the list itself moved: no slide
    menu_was_first = menu_first;
    if (menu_hl != target) {
        int step = (target - menu_hl) / 2;
        menu_hl += step ? step : (target > menu_hl ? 1 : -1);
        anim = true;
    }
    // The highlight inverts what is under it; its corners are put back, to round it.
    oled.fillRect(0, menu_hl, W - 4, ROW - 1, SSD1306_INVERSE);
    for (int cx : {0, W - 5})
        for (int cy : {menu_hl, menu_hl + ROW - 2}) oled.drawPixel(cx, cy, SSD1306_INVERSE);
    if (n > rows) {
        int h = std::max(4, (H - Y0) * rows / n);
        oled.drawFastVLine(W - 1, Y0 + (H - Y0 - h) * menu_first / (n - rows), h, SSD1306_WHITE);
    }
}

// The screen at rest (the wide one): a bar with the remote's name and what it
// is joined to (the band, Wi-Fi, a Bluetooth remote), the device it controls
// large in a frame, and the last key pressed in a pill.
void home_draw(uint32_t ms) {
    const std::string &link = status[0], &target = status[2], &key = status[3];
    bool bt = status[1].size() > 4, band = link == "band", wifi = band || link == "hub?" || link == "clock";
    oled.fillRect(0, 0, W, 11, SSD1306_WHITE);
    for (int cx : {0, W - 1})
        for (int cy : {0, 10}) oled.drawPixel(cx, cy, SSD1306_BLACK);
    oled.setTextColor(SSD1306_BLACK);
    oled.setCursor(3, 2);
    oled.print(band ? "ROOK" : link.substr(0, 8).c_str());
    int x = W - 11;
    if (bt) { oled.drawBitmap(x, 1, IC_BT, 8, 8, SSD1306_BLACK); x -= 11; }
    if (wifi) { oled.drawBitmap(x, 1, IC_WIFI, 8, 8, SSD1306_BLACK); x -= 10; }
    if (band) oled.fillCircle(x + 4, 5, 2, SSD1306_BLACK);
    else if ((ms / 500) & 1) oled.drawCircle(x + 4, 5, 2, SSD1306_BLACK);     // not on the band: it blinks
    if (!band) anim = true;
    oled.setTextColor(SSD1306_WHITE);
    oled.drawRoundRect(0, 15, W, 30, 5, SSD1306_WHITE);
    int n = (int)target.size();
    if (n && n * 12 <= W - 8) {
        oled.setTextSize(2);
        oled.setCursor((W - n * 12) / 2 + 1, 23);
        oled.print(target.c_str());
        oled.setTextSize(1);
    } else if (n * 6 <= W - 8) {
        oled.setCursor((W - n * 6) / 2, 26);
        oled.print(n ? target.c_str() : "no device");
    } else {
        oled.setCursor(5, 26);
        oled.print((target.substr(0, (W - 16) / 6 - 1) + "~").c_str());
    }
    if (!key.empty()) {
        std::string k = key.substr(0, 10);
        int w = (int)k.size() * 6 + 9;
        oled.fillRoundRect(0, 50, w, 13, 6, SSD1306_WHITE);
        oled.setTextColor(SSD1306_BLACK);
        oled.setCursor(5, 53);
        oled.print(k.c_str());
        oled.setTextColor(SSD1306_WHITE);
    }
    if (key.size() <= 8) {
        for (int i = 0; i < 3; i++) oled.drawFastHLine(W - 38, 54 + i * 2, 6, SSD1306_WHITE);     // the input key opens it
        oled.setCursor(W - 29, 53);
        oled.print("menu");
    }
}

void draw() {
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextWrap(false);
    oled.setTextColor(SSD1306_WHITE);
    uint32_t ms = millis();
    anim = false;
    if (ask_on) {
        auto p = wrap(ask_prompt, H / 16);
        lines(p, 0, 8);
        list(p.size() * 8 + 3, ask_opts, ask_sel, ms);
    } else if (canvas_fn) {
        canvas_fn(oled);
    } else if (game_on) {
        tetris::draw(oled);
    } else if (menu_on) {
        menu_draw(ms);
    } else if (note_until && (int32_t)(note_until - ms) > 0) {
        oled.setCursor(1, 0);
        oled.print(status[0].substr(0, COLS).c_str());
        oled.drawFastHLine(0, 9, W, SSD1306_WHITE);
        lines(wrap(note, (H - 16) / 9), 16, 9);
    } else if (agent_on && agent_big) {
        oled.setTextSize((int)agent_text.size() * 12 <= W ? 2 : 1);
        oled.setCursor(1, H / 2 - 12);
        oled.print(agent_text.substr(0, COLS).c_str());
    } else if (agent_on) {
        auto all = wrap(agent_text, 400);
        int n = (int)all.size(), top = std::max(0, std::min(agent_top, n - ROWS));
        lines(std::vector<std::string>(all.begin() + top, all.begin() + std::min(n, top + ROWS)), 0, 8);
        if (n > ROWS) {                       // where this page is in the whole, down the right edge
            int h = std::max(4, H * ROWS / n);
            oled.drawFastVLine(W - 1, (H - h) * top / (n - ROWS), h, 1);
        }
    } else if (W >= 100) {
        home_draw(ms);
    } else {
        static const int Y[4] = {0, 14, 24, 44};
        for (int i = 0; i < 4; i++) {
            if (i == 2) { line(status[i], Y[i], ms); continue; }      // the target: slides when long
            oled.setCursor(1, Y[i]);
            oled.print(status[i].substr(0, COLS).c_str());
        }
        oled.drawFastHLine(0, 9, W, SSD1306_WHITE);
    }
    oled.display();
}

}  // namespace

namespace ui {

bool begin(uint8_t addr, int rotation) {
    mu = xSemaphoreCreateMutex();
    ok = oled.begin(SSD1306_SWITCHCAPVCC, addr);
    oled.setRotation(rotation & 3);
    return ok;
}

void set_rotation(int rotation) {
    xSemaphoreTake(mu, portMAX_DELAY);
    oled.setRotation(rotation & 3);
    dirty = true;
    xSemaphoreGive(mu);
}

void tick() {
    if (!ok) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    uint32_t ms = millis();
    if (agent_on && agent_until && (int32_t)(ms - agent_until) >= 0) { agent_on = false; dirty = true; }
    if (note_until && (int32_t)(ms - note_until) >= 0) { note_until = 0; dirty = true; }
    if (anim && ms - last_draw >= 40) dirty = true;
    if (dirty && !dark) {
        dirty = false;
        draw();
        last_draw = millis();
    }
    xSemaphoreGive(mu);
}

void redraw() { dirty = true; }

void status_line(int line, const std::string& t) {
    if (line < 0 || line >= 4) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    if (status[line] != t) { status[line] = t; dirty = true; }
    xSemaphoreGive(mu);
}

void text(const std::string& t, uint32_t seconds) {
    xSemaphoreTake(mu, portMAX_DELAY);
    agent_text = t;
    agent_top = 0;
    agent_on = !t.empty();
    agent_big = false;
    agent_until = seconds ? millis() + seconds * 1000 : 0;
    if (agent_until == 0 && seconds) agent_until = 1;
    note_until = 0;
    dirty = true;
    xSemaphoreGive(mu);
}

bool text_scroll(int delta) {
    xSemaphoreTake(mu, portMAX_DELAY);
    int n = agent_on && !agent_big ? (int)wrap(agent_text, 400).size() : 0;
    bool can = n > ROWS;
    if (can) {
        agent_top = std::max(0, std::min(agent_top + delta, n - ROWS));
        if (agent_until) agent_until = millis() + 90000;      // being read: it stays
        dirty = true;
    }
    xSemaphoreGive(mu);
    return can;
}

std::string shown() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string s;
    if (ask_on) s = ask_prompt;
    else if (canvas_fn) s = canvas_name;
    else if (game_on) s = "Tetris";
    else if (menu_on) {
        s = menu_title;
        for (size_t i = 0; i < menu_items.size(); i++) s += std::string((int)i == menu_sel ? "\n> " : "\n  ") + menu_items[i];
    }
    else if (agent_on) s = agent_text;
    else for (int i = 0; i < 4; i++) s += status[i] + (i < 3 ? "\n" : "");
    xSemaphoreGive(mu);
    return s;
}

void big(const std::string& t) {
    xSemaphoreTake(mu, portMAX_DELAY);
    agent_text = t;
    agent_on = true;
    agent_big = true;
    agent_until = 0;
    note_until = 0;
    dirty = true;
    xSemaphoreGive(mu);
}

void flash(const std::string& t) {
    xSemaphoreTake(mu, portMAX_DELAY);
    note = t;
    note_until = millis() + 1500;
    if (!note_until) note_until = 1;
    dirty = true;
    xSemaphoreGive(mu);
}

void ask_begin(const std::string& prompt, const std::vector<std::string>& options) {
    xSemaphoreTake(mu, portMAX_DELAY);
    ask_on = true;
    ask_prompt = prompt;
    ask_opts = options;
    ask_sel = 0;
    scroll_t0 = millis();
    dirty = true;
    xSemaphoreGive(mu);
}

void ask_move(int d) {
    xSemaphoreTake(mu, portMAX_DELAY);
    int n = (int)ask_opts.size();
    if (n) ask_sel = ((ask_sel + d) % n + n) % n;
    scroll_t0 = millis();
    dirty = true;
    xSemaphoreGive(mu);
}

int ask_selected() { return ask_sel; }

void ask_end() {
    xSemaphoreTake(mu, portMAX_DELAY);
    ask_on = false;
    dirty = true;
    xSemaphoreGive(mu);
}

bool asking() { return ask_on; }

void menu_begin(const std::string& title, const std::vector<std::string>& items, int selected) {
    xSemaphoreTake(mu, portMAX_DELAY);
    menu_on = true;
    menu_title = title;
    menu_items = items;
    menu_sel = selected >= 0 && selected < (int)items.size() ? selected : 0;
    menu_first = 0;
    menu_hl = -1;
    scroll_t0 = millis();
    dirty = true;
    xSemaphoreGive(mu);
}

void menu_move(int d) {
    xSemaphoreTake(mu, portMAX_DELAY);
    int n = (int)menu_items.size();
    if (n) menu_sel = ((menu_sel + d) % n + n) % n;
    scroll_t0 = millis();
    dirty = true;
    xSemaphoreGive(mu);
}

int menu_selected() { return menu_sel; }

void menu_end() {
    xSemaphoreTake(mu, portMAX_DELAY);
    menu_on = false;
    dirty = true;
    xSemaphoreGive(mu);
}

void canvas(void (*draw)(Adafruit_GFX&), const char* name) {
    xSemaphoreTake(mu, portMAX_DELAY);
    canvas_fn = draw;
    canvas_name = name ? name : "";
    dirty = true;
    xSemaphoreGive(mu);
}

bool snapshot(std::string& out, int& w, int& h, int& rotation, bool& is_dark) {
    if (!ok) return false;
    xSemaphoreTake(mu, portMAX_DELAY);
#ifdef ROOK_BRIDGE
    w = 128; h = 64;
#else
    w = 128; h = 32;
#endif
    rotation = oled.getRotation();
    is_dark = dark;
    out.assign((const char*)oled.getBuffer(), (size_t)w * h / 8);
    xSemaphoreGive(mu);
    return true;
}

void sleep(bool on) {
    if (!ok) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    if (on != dark) {
        dark = on;
        oled.ssd1306_command(on ? SSD1306_DISPLAYOFF : SSD1306_DISPLAYON);
        dirty = true;                // what changed while it was dark is drawn when it comes back
    }
    xSemaphoreGive(mu);
}

void game(bool on) {
    xSemaphoreTake(mu, portMAX_DELAY);
    game_on = on;
    dirty = true;
    xSemaphoreGive(mu);
}

}  // namespace ui
