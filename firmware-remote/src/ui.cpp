#include "ui.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#ifdef ROOK_GAMEPAD
#include <Adafruit_ST7735.h>
#include <SPI.h>
#endif
#include <Wire.h>
#include <algorithm>
#include <time.h>
#include "tetris.h"

namespace {

#ifdef ROOK_GAMEPAD
// The gamepad build's screen is a 0.96" 160 x 80 colour LCD (ST7735, the
// T-Dongle-S3's). It is drawn as the bridge's 128 x 64 one-bit picture, kept
// here, and shown one pixel for one in the middle of the LCD. It answers what
// the rest of this file asks of the OLED, and keeps colours for what draws
// with them (the games).
class PadScreen : public GFXcanvas16 {
  public:
    PadScreen() : GFXcanvas16(128, 64), spi(FSPI), tft(&spi, 4, 2, 1) {}
    bool begin(int, int) {
        spi.begin(5, -1, 3);
        tft.initR(INITR_MINI160x80_PLUGIN);
        tft.setRotation(1);
        tft.invertDisplay(false);
        tft.fillScreen(0);
        pinMode(38, OUTPUT);
        digitalWrite(38, LOW);       // the backlight: lit when low
        return getBuffer() != nullptr;
    }
    void clearDisplay() { GFXcanvas16::fillScreen(0); }
    void display() { tft.drawRGBBitmap(16, 8, getBuffer(), 128, 64); }
    void ssd1306_command(uint8_t c) { digitalWrite(38, c == SSD1306_DISPLAYOFF ? HIGH : LOW); }
    // What is drawn says 0 (off), 1 (lit: white here), SSD1306_INVERSE (the pixel
    // turned over: the menu's highlight), or a colour of its own (ink.h).
    static uint16_t ink(uint16_t c) { return c == 1 ? 0xFFFF : c; }
    void drawPixel(int16_t x, int16_t y, uint16_t color) override {
        GFXcanvas16::drawPixel(x, y, color == SSD1306_INVERSE ? (uint16_t)~getPixel(x, y) : ink(color));
    }
    void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color) override {
        if (color != SSD1306_INVERSE) return GFXcanvas16::drawFastHLine(x, y, w, ink(color));
        for (int16_t i = 0; i < w; i++) drawPixel(x + i, y, color);
    }
    void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color) override {
        if (color != SSD1306_INVERSE) return GFXcanvas16::drawFastVLine(x, y, h, ink(color));
        for (int16_t i = 0; i < h; i++) drawPixel(x, y + i, color);
    }
    void fillScreen(uint16_t color) override { GFXcanvas16::fillScreen(ink(color)); }
    // The picture as an SSD1306 holds it, lit or not (what ui::snapshot hands out).
    void pages(std::string& out) {
        out.assign(128 * 64 / 8, '\0');
        const uint16_t* b = getBuffer();
        for (int y = 0; y < 64; y++)
            for (int x = 0; x < 128; x++)
                if (b[y * 128 + x]) out[x + (y / 8) * 128] |= (char)(1 << (y & 7));
    }

  private:
    SPIClass spi;
    Adafruit_ST7735 tft;
};
PadScreen oled;
#elif defined(ROOK_BRIDGE)
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
#else
Adafruit_SSD1306 oled(128, 32, &Wire, -1);
#endif
bool ok = false, dirty = true;
bool anim = false;             // the last frame had a scrolling line: keep redrawing
uint32_t last_draw = 0, scroll_t0 = 0;
SemaphoreHandle_t mu;
std::string status[4];
std::string net_name;          // the Wi-Fi network in use
std::string agent_text;
uint32_t agent_until = 0;      // 0 = until replaced; set when agent_text is shown
int agent_top = 0;             // the first line of agent_text on screen (it scrolls when long)
bool agent_on = false, agent_big = false;
std::string note;
uint32_t note_until = 0;
bool note_over = false;        // the note is drawn over the menu as well
bool ask_on = false;
std::string ask_prompt;
std::vector<std::string> ask_opts;
int ask_sel = 0;
bool menu_on = false;
std::string menu_title;
std::vector<std::string> menu_items;
std::vector<int8_t> menu_bars;      // a signal strength (0..4) a row; -1 or past the end: none
int menu_sel = 0;
bool game_on = false;
void (*canvas_fn)(Adafruit_GFX&) = nullptr;
std::string canvas_name;
bool dark = false;             // the panel is switched off (the remote was left alone)
int menu_first = 0, menu_hl = -1, menu_was_first = 0;   // the menu's first row on screen; where its highlight is, sliding
std::vector<std::string> tab_names;   // the menu's tabs (none: a plain menu with a title)
int tab_on = 0;                       // the tab open
int tab_hl_x = -1, tab_hl_w = 0;      // where the strip's highlight is, sliding
void (*tab_body)(Adafruit_GFX&) = nullptr;   // the open tab draws itself (under the strip)
GFXcanvas1* body_cv = nullptr;
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

// fit characters of s: the start, and when s is longer a window that walks
// along it, resting a moment at each end.
std::string window(const std::string& s, int fit, uint32_t ms) {
    int over = (int)s.size() - fit;
    if (over <= 0 || fit <= 0) return s.substr(0, std::max(0, fit));
    anim = true;
    int p = (int)((ms - scroll_t0) / 280) % (over + 10);
    return s.substr(p < 5 ? 0 : std::min(p - 5, over), fit);
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
const uint8_t IC_UPD[] PROGMEM = {0x18, 0x18, 0x18, 0x7E, 0x3C, 0x18, 0x00, 0x7E};
const uint8_t IC_RING[] PROGMEM = {0x00, 0x3C, 0x42, 0x42, 0x42, 0x42, 0x3C, 0x00};
const uint8_t IC_GEAR[] PROGMEM = {0x18, 0x7E, 0x66, 0xC3, 0xC3, 0x66, 0x7E, 0x18};

// The tabs' pictures: Devices, Assistant, Games, Settings.
const uint8_t* tab_icon(const std::string& name) {
    if (name == "Devices") return IC_TV;
    if (name == "Assistant") return IC_CHAT;
    if (name == "Games") return IC_GAME;
    if (name == "Settings") return IC_GEAR;
    return IC_DOT;
}

const uint8_t* icon_for(const std::string& label) {
    std::string l;
    for (char c : label) l += (char)tolower((unsigned char)c);
    auto has = [&](const char* w) { return l.find(w) != std::string::npos; };
    if (has("tetris") || has("swarm") || has("2048") || has("towers") || has("game")) return IC_GAME;
    if (has("wi-fi") || has("wifi")) return IC_WIFI;
    if (has("forget")) return IC_X;
    if (has(" bt") || has("bluetooth")) return IC_BT;
    if (has("update")) return IC_UPD;
    if (has("find") || has("scan")) return IC_FIND;
    if (has("assistant")) return IC_CHAT;
    if (has(" (")) return IC_RING;               // found, but nothing here drives it: name (what it answered as)
    bool devices_tab = !tab_names.empty() && tab_names[tab_on] == "Devices";
    if (has("tv") || has("roku") || has("cast") || menu_title == "DEVICES" || devices_tab) return IC_TV;
    return IC_DOT;
}

// Moves v a step towards to, halfway each frame (one pixel at the end).
void slide(int& v, int to) {
    if (v == to) return;
    int step = (to - v) / 2;
    v += step ? step : (to > v ? 1 : -1);
    anim = true;
}

// The strip of tabs along the top: on the wide screen the open tab has its
// picture and its name, the others their picture; on the narrow one, pictures
// only, 8 pixels each. The highlight inverts the open one and slides between
// them. Answers the strip's height (the rule under it included).
int tab_strip() {
    int n = (int)tab_names.size();
    if (W < 100) {
        int cell = W / n;
        for (int i = 0; i < n; i++) oled.drawBitmap(i * cell + (cell - 8) / 2, 1, tab_icon(tab_names[i]), 8, 8, SSD1306_WHITE);
        if (tab_hl_x < 0) tab_hl_x = tab_on * cell;
        slide(tab_hl_x, tab_on * cell);
        oled.fillRect(tab_hl_x, 0, cell, 10, SSD1306_INVERSE);
        oled.drawFastHLine(0, 10, W, SSD1306_WHITE);
        return 12;
    }
    std::vector<int> w(n), x(n);
    int used = 0;
    for (int i = 0; i < n; i++) {
        w[i] = 8 + 8 + (i == tab_on ? 3 + (int)tab_names[i].size() * 6 : 0);
        used += w[i];
    }
    int gap = n > 1 ? std::max(0, (W - used) / (n - 1)) : 0, cx = std::max(0, (W - used - gap * (n - 1)) / 2);
    for (int i = 0; i < n; i++) {
        x[i] = cx;
        oled.drawBitmap(cx + 4, 1, tab_icon(tab_names[i]), 8, 8, SSD1306_WHITE);
        if (i == tab_on) {
            oled.setCursor(cx + 15, 2);
            oled.print(tab_names[i].c_str());
        }
        cx += w[i] + gap;
    }
    if (tab_hl_x < 0) { tab_hl_x = x[tab_on]; tab_hl_w = w[tab_on]; }
    slide(tab_hl_x, x[tab_on]);
    slide(tab_hl_w, w[tab_on]);
    // The highlight inverts what is under it; its corners are put back, to round it.
    oled.fillRect(tab_hl_x, 0, tab_hl_w, 11, SSD1306_INVERSE);
    for (int px : {tab_hl_x, tab_hl_x + tab_hl_w - 1})
        for (int py : {0, 10}) oled.drawPixel(px, py, SSD1306_INVERSE);
    oled.drawFastHLine(0, 12, W, SSD1306_WHITE);
    return 13;
}

// A tab that draws itself (the conversation) gets the screen under the strip.
void tab_body_draw(int y0) {
    int h = H - y0;
    if (!body_cv || body_cv->width() != W || body_cv->height() != h) {
        delete body_cv;
        body_cv = new GFXcanvas1(W, h);
    }
    if (!body_cv->getBuffer()) return;
    body_cv->fillScreen(0);
    tab_body(*body_cv);
    oled.drawBitmap(0, y0, body_cv->getBuffer(), W, h, SSD1306_WHITE, SSD1306_BLACK);
}

// The menu: a title bar with where you are in the list, rows with a picture
// each, a highlight that slides to the row chosen, and a mark down the right
// edge when there is more than fits. (The narrow screen keeps the plain list.)
void menu_draw(uint32_t ms) {
    if (!tab_names.empty() && tab_body) {
        int y0 = tab_strip() + (W < 100 ? 0 : 1);
        tab_body_draw(y0);
        return;
    }
    if (W < 100) {
        int y = 0;
        if (!tab_names.empty()) {
            y = tab_strip();
        } else if (!menu_title.empty()) {
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
    if (!tab_names.empty()) {
        tab_strip();
    } else {
        oled.fillRect(0, 0, W, BAR, SSD1306_WHITE);
        oled.setTextColor(SSD1306_BLACK);
        oled.setCursor(3, 2);
        oled.print(menu_title.substr(0, 12).c_str());
        char where[12];
        snprintf(where, sizeof where, "%d/%d", menu_sel + 1, n);
        oled.setCursor(W - 3 - (int)strlen(where) * 6, 2);
        oled.print(where);
        oled.setTextColor(SSD1306_WHITE);
    }
    for (int r = 0; r < rows && menu_first + r < n; r++) {
        const std::string& item = menu_items[menu_first + r];
        int y = Y0 + r * ROW;
        oled.drawBitmap(3, y + 1, icon_for(item), 8, 8, SSD1306_WHITE);
        oled.setCursor(15, y + 2);
        int fit = (W - 4 - 16) / 6;
        int lv = menu_first + r < (int)menu_bars.size() ? menu_bars[menu_first + r] : -1;
        if (lv >= 0) {                   // four bars at the right, as many filled as it is strong
            fit -= 2;
            for (int b = 0; b < 4; b++) {
                int bh = 2 + b * 2, bx = W - 18 + b * 3;
                if (b < lv) oled.fillRect(bx, y + 9 - bh, 2, bh, SSD1306_WHITE);
                else oled.drawPixel(bx, y + 8, SSD1306_WHITE);
            }
        }
        oled.print((menu_first + r == menu_sel ? window(item, fit, ms) : item.substr(0, fit)).c_str());
    }
    int target = Y0 + (menu_sel - menu_first) * ROW;
    if (menu_hl < 0 || menu_first != menu_was_first) menu_hl = target;     // the list itself moved: no slide
    menu_was_first = menu_first;
    slide(menu_hl, target);
    // The highlight inverts what is under it; its corners are put back, to round it.
    oled.fillRect(0, menu_hl, W - 4, ROW - 1, SSD1306_INVERSE);
    for (int cx : {0, W - 5})
        for (int cy : {menu_hl, menu_hl + ROW - 2}) oled.drawPixel(cx, cy, SSD1306_INVERSE);
    if (n > rows) {
        int h = std::max(4, (H - Y0) * rows / n);
        oled.drawFastVLine(W - 1, Y0 + (H - Y0 - h) * menu_first / (n - rows), h, SSD1306_WHITE);
    }
}

const uint8_t IC_ROOK[] PROGMEM = {0xDB, 0xFF, 0x7E, 0x3C, 0x3C, 0x3C, 0x7E, 0xFF};

// The wide screen at rest: a bar with the Wi-Fi network and what else the
// remote is joined to (a rook: the band; Wi-Fi; a Bluetooth remote), the
// device it controls large in a frame, and the last key pressed in a pill.
// A note (a key just pressed, something that happened) takes the frame for
// its moment, and the device moves to the bottom row.
void home_bar(uint32_t ms) {
    const std::string& link = status[0];
    bool bt = status[1].size() > 4, band = link == "band", wifi = band || link == "hub?" || link == "clock";
    oled.fillRect(0, 0, W, 11, SSD1306_WHITE);
    for (int cx : {0, W - 1})
        for (int cy : {0, 10}) oled.drawPixel(cx, cy, SSD1306_BLACK);
    int x = W - 11;
    if (bt) { oled.drawBitmap(x, 1, IC_BT, 8, 8, SSD1306_BLACK); x -= 11; }
    if (wifi) { oled.drawBitmap(x, 1, IC_WIFI, 8, 8, SSD1306_BLACK); x -= 11; }
    if (band) { oled.drawBitmap(x, 1, IC_ROOK, 8, 8, SSD1306_BLACK); x -= 11; }
    else if (wifi && ((ms / 500) & 1)) { oled.drawBitmap(x, 1, IC_ROOK, 8, 8, SSD1306_BLACK); x -= 11; }   // no hub yet: it blinks
    else if (wifi) x -= 11;
    if (!band) anim = true;
    std::string name = wifi && !net_name.empty() ? net_name : link == "wifi-" ? "no Wi-Fi" : link == "wifi?" ? "set up Wi-Fi" : link;
    oled.setTextColor(SSD1306_BLACK);
    oled.setCursor(3, 2);
    oled.print(window(name, (x + 8 - 3) / 6, ms).c_str());
    oled.setTextColor(SSD1306_WHITE);
}

// Something going on (an update, a look around the network): its name between
// rules, what it says large, and how far along it is.
bool card_on = false;
std::string card_title, card_body;
uint32_t card_until = 0;       // a busy card nobody ended goes away by itself
int card_pct = -1;             // 0..100: that far; -2: busy, no telling how far; -1: no bar

void card_draw(uint32_t ms) {
    home_bar(ms);
    int tw = (int)card_title.size() * 6, side = (W - tw) / 2 - 10;
    oled.setCursor((W - tw) / 2, 15);
    oled.print(card_title.c_str());
    if (side > 0) {
        oled.drawFastHLine(4, 18, side, SSD1306_WHITE);
        oled.drawFastHLine(W - 4 - side, 18, side, SSD1306_WHITE);
    }
    int n = (int)card_body.size(), y = card_pct == -1 ? 34 : 29;
    if (n * 12 <= W - 8) {
        oled.setTextSize(2);
        oled.setCursor((W - n * 12) / 2 + 1, y);
        oled.print(card_body.c_str());
        oled.setTextSize(1);
    } else {
        auto ls = wrap(card_body, 2);
        for (size_t i = 0; i < ls.size(); i++) {
            oled.setCursor(std::max(1, (W - (int)ls[i].size() * 6) / 2), y + (ls.size() > 1 ? 0 : 4) + (int)i * 9);
            oled.print(ls[i].c_str());
        }
    }
    if (card_pct == -1) return;
    oled.drawRoundRect(4, 51, W - 8, 10, 4, SSD1306_WHITE);
    if (card_pct >= 0) {
        int w = (W - 14) * std::min(card_pct, 100) / 100;
        if (w > 2) oled.fillRoundRect(7, 54, w, 4, 1, SSD1306_WHITE);
    } else {                                  // a block going back and forth
        int span = W - 14 - 26, p = (int)(ms / 25) % (2 * span);
        oled.fillRoundRect(7 + (p < span ? p : 2 * span - p), 54, 26, 4, 1, SSD1306_WHITE);
        anim = true;
    }
}

// The time in the bottom right corner, once the clock is set. How wide it came out (0: not drawn).
int home_clock() {
    time_t now = time(nullptr);
    if (now <= 1700000000) return 0;
    struct tm t;
    localtime_r(&now, &t);
    char hm[8];
    int h = t.tm_hour % 12;
    snprintf(hm, sizeof hm, "%d:%02d", h ? h : 12, t.tm_min);
    int w = (int)strlen(hm) * 6;
    oled.setCursor(W - w - 2, 53);
    oled.print(hm);
    anim = true;                     // the minute turns over on its own
    return w;
}

void home_draw(uint32_t ms, bool noted) {
    const std::string &target = status[2], &key = status[3];
    home_bar(ms);
    oled.drawRoundRect(0, 15, W, 30, 5, SSD1306_WHITE);
    auto big = [&](const std::string& t) {
        int n = (int)t.size();
        if (n && n * 12 <= W - 8) {
            oled.setTextSize(2);
            oled.setCursor((W - n * 12) / 2 + 1, 23);
            oled.print(t.c_str());
            oled.setTextSize(1);
            return;
        }
        auto ls = wrap(t, 3);
        int y = 30 - (int)ls.size() * 4 - (ls.size() > 1 ? 1 : 0);
        for (auto& l : ls) {
            oled.setCursor(std::max(4, (W - (int)l.size() * 6) / 2), y);
            oled.print(l.substr(0, COLS - 1).c_str());
            y += 9;
        }
    };
    if (noted) {
        std::string t = note;
        if (t.find('\n') != std::string::npos && t.size() <= 10)      // "BT\ngone": short enough for one large line
            std::replace(t.begin(), t.end(), '\n', ' ');
        big(t);
        int clock = home_clock();            // the time stays where it was
        oled.drawBitmap(1, 52, IC_TV, 8, 8, SSD1306_WHITE);
        oled.setCursor(13, 53);
        oled.print(target.substr(0, (W - 13 - (clock ? clock + 8 : 0)) / 6).c_str());
        return;
    }
    big(target.empty() ? std::string("no device") : target);
    if (!key.empty()) {
        std::string k = key.substr(0, 10);
        int w = (int)k.size() * 6 + 9;
        oled.fillRoundRect(0, 50, w, 13, 6, SSD1306_WHITE);
        oled.setTextColor(SSD1306_BLACK);
        oled.setCursor(5, 53);
        oled.print(k.c_str());
        oled.setTextColor(SSD1306_WHITE);
    }
    // The time where the menu hint was; the hint until the clock is set.
    if (home_clock()) {
    } else if (key.size() <= 8) {
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
        if (note_over && note_until && (int32_t)(note_until - ms) > 0) {
            // A word about what was just done in it, in a box over the menu.
            auto ls = wrap(note, W < 100 ? 4 : 2);
            int h = (int)ls.size() * 9 + 6, y = (H - h) / 2;
            oled.fillRect(0, y, W, h, SSD1306_BLACK);
            oled.drawRect(0, y, W, h, SSD1306_WHITE);
            int ly = y + 4;
            for (auto& l : ls) {
                oled.setCursor(std::max(1, (W - (int)l.size() * 6) / 2), ly);
                oled.print(l.substr(0, COLS).c_str());
                ly += 9;
            }
        }
    } else if (card_on && W >= 100) {
        card_draw(ms);
    } else if (W >= 100 && note_until && (int32_t)(note_until - ms) > 0) {
        home_draw(ms, true);
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
        home_draw(ms, false);
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
    if (card_on && card_until && (int32_t)(ms - card_until) >= 0) { card_on = false; dirty = true; }
    if (anim && ms - last_draw >= 40) dirty = true;
    if (dirty && !dark) {
        dirty = false;
        draw();
        last_draw = millis();
    }
    xSemaphoreGive(mu);
}

void redraw() { dirty = true; }

void network(const std::string& ssid) {
    xSemaphoreTake(mu, portMAX_DELAY);
    if (net_name != ssid) { net_name = ssid; dirty = true; }
    xSemaphoreGive(mu);
}

void status_line(int line, const std::string& t) {
    if (line < 0 || line >= 4) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    if (status[line] != t) { status[line] = t; dirty = true; }
    xSemaphoreGive(mu);
}

void card(const std::string& title, const std::string& body, int percent) {
    if (W < 100) {                   // the narrow screen says it in plain text
        text(title.empty() ? std::string() : title + "\n" + body, 0);
        return;
    }
    xSemaphoreTake(mu, portMAX_DELAY);
    card_on = !title.empty();
    card_title = title;
    card_body = body;
    card_pct = percent;
    card_until = percent == -2 ? (millis() + 40000) | 1 : 0;
    dirty = true;
    xSemaphoreGive(mu);
}

void text(const std::string& t, uint32_t seconds) {
    xSemaphoreTake(mu, portMAX_DELAY);
    card_on = false;
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
    else if (card_on && !menu_on) s = card_title + "\n" + card_body;
    else if (menu_on && !tab_names.empty()) {
        for (size_t i = 0; i < tab_names.size(); i++)
            s += (i ? " | " : "") + ((int)i == tab_on ? "[" + tab_names[i] + "]" : tab_names[i]);
        if (tab_body) s += "\nchat";
        for (size_t i = 0; !tab_body && i < menu_items.size(); i++)
            s += std::string((int)i == menu_sel ? "\n> " : "\n  ") + menu_items[i];
    }
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

void flash(const std::string& t) { flash(t, 1500, false); }

void flash(const std::string& t, uint32_t ms, bool over_menu) {
    xSemaphoreTake(mu, portMAX_DELAY);
    note = t;
    note_until = millis() + ms;
    if (!note_until) note_until = 1;
    note_over = over_menu;
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
    card_on = false;
    menu_on = true;
    menu_title = title;
    menu_items = items;
    menu_bars.clear();
    menu_sel = selected >= 0 && selected < (int)items.size() ? selected : 0;
    menu_first = 0;
    menu_hl = -1;
    tab_names.clear();
    tab_body = nullptr;
    scroll_t0 = millis();
    dirty = true;
    xSemaphoreGive(mu);
}

void menu_tabs(const std::vector<std::string>& names, int active, const std::vector<std::string>& items,
               int selected, void (*body)(Adafruit_GFX&)) {
    xSemaphoreTake(mu, portMAX_DELAY);
    card_on = false;
    if (!menu_on || tab_names != names) tab_hl_x = -1;    // the strip just came up: no slide
    menu_on = true;
    menu_title = active >= 0 && active < (int)names.size() ? names[active] : "";
    tab_names = names;
    tab_on = active >= 0 && active < (int)names.size() ? active : 0;
    tab_body = body;
    menu_items = items;
    menu_bars.clear();
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

void menu_bars_set(const std::vector<int8_t>& bars) {
    xSemaphoreTake(mu, portMAX_DELAY);
    menu_bars = bars;
    dirty = true;
    xSemaphoreGive(mu);
}

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
#ifdef ROOK_GAMEPAD
    oled.pages(out);
#else
    out.assign((const char*)oled.getBuffer(), (size_t)w * h / 8);
#endif
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
