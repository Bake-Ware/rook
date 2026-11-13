#include "ui.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Wire.h>
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
    } else if (game_on) {
        tetris::draw(oled);
    } else if (menu_on) {
        int y = 0;
        if (!menu_title.empty()) {
            oled.setCursor(1, 0);
            oled.print(menu_title.substr(0, COLS).c_str());
            oled.drawFastHLine(0, 9, W, SSD1306_WHITE);
            y = 12;
        }
        list(y, menu_items, menu_sel, ms);
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
        lines(wrap(agent_text, ROWS), 0, 8);
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
    if (anim && ms - last_draw >= 60) dirty = true;
    if (dirty) {
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
    agent_on = !t.empty();
    agent_big = false;
    agent_until = seconds ? millis() + seconds * 1000 : 0;
    if (agent_until == 0 && seconds) agent_until = 1;
    note_until = 0;
    dirty = true;
    xSemaphoreGive(mu);
}

std::string shown() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string s;
    if (ask_on) s = ask_prompt;
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

void game(bool on) {
    xSemaphoreTake(mu, portMAX_DELAY);
    game_on = on;
    dirty = true;
    xSemaphoreGive(mu);
}

}  // namespace ui
