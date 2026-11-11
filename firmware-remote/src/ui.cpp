#include "ui.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Wire.h>

namespace {

Adafruit_SSD1306 oled(128, 32, &Wire, -1);
bool ok = false, dirty = true;
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
const int COLS = 21, ROWS = 4;

std::vector<std::string> wrap(const std::string& t, size_t maxlines) {
    std::vector<std::string> out;
    std::string line;
    for (size_t i = 0; i <= t.size() && out.size() < maxlines; i++) {
        char c = i < t.size() ? t[i] : '\n';
        if (c == '\n' || (int)line.size() == COLS) {
            out.push_back(line);
            line.clear();
            if (c == '\n') continue;
        }
        if (c != '\n') line += c;
    }
    return out;
}

void draw() {
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);
    uint32_t ms = millis();
    if (ask_on) {
        auto p = wrap(ask_prompt, 2);
        for (size_t i = 0; i < p.size(); i++) { oled.setCursor(0, i * 8); oled.print(p[i].c_str()); }
        int rows = ROWS - (int)p.size();
        int first = ask_sel - rows + 1 < 0 ? 0 : ask_sel - rows + 1;
        for (int r = 0; r < rows && first + r < (int)ask_opts.size(); r++) {
            int i = first + r;
            int y = (p.size() + r) * 8;
            if (i == ask_sel) {
                oled.fillRect(0, y, 128, 8, SSD1306_WHITE);
                oled.setTextColor(SSD1306_BLACK);
            }
            oled.setCursor(0, y);
            oled.print((std::string(i == ask_sel ? ">" : " ") + ask_opts[i]).substr(0, COLS).c_str());
            oled.setTextColor(SSD1306_WHITE);
        }
    } else if (note_until && (int32_t)(note_until - ms) > 0) {
        auto l = wrap(note, 2);
        oled.setCursor(0, 0);
        oled.print(status[0].substr(0, COLS).c_str());
        oled.setTextSize(l.size() == 1 && l[0].size() <= 10 ? 2 : 1);
        for (size_t i = 0; i < l.size(); i++) { oled.setCursor(0, 12 + i * 10); oled.print(l[i].c_str()); }
    } else if (agent_on && agent_big) {
        oled.setTextSize(3);
        oled.setCursor(0, 4);
        oled.print(agent_text.substr(0, 7).c_str());
    } else if (agent_on) {
        auto l = wrap(agent_text, ROWS);
        for (size_t i = 0; i < l.size(); i++) { oled.setCursor(0, i * 8); oled.print(l[i].c_str()); }
    } else {
        for (int i = 0; i < ROWS; i++) { oled.setCursor(0, i * 8); oled.print(status[i].substr(0, COLS).c_str()); }
    }
    oled.display();
}

}  // namespace

namespace ui {

bool begin(uint8_t addr) {
    mu = xSemaphoreCreateMutex();
    ok = oled.begin(SSD1306_SWITCHCAPVCC, addr);
    return ok;
}

void tick() {
    if (!ok) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    uint32_t ms = millis();
    if (agent_on && agent_until && (int32_t)(ms - agent_until) >= 0) { agent_on = false; dirty = true; }
    if (note_until && (int32_t)(ms - note_until) >= 0) { note_until = 0; dirty = true; }
    if (dirty) { dirty = false; draw(); }
    xSemaphoreGive(mu);
}

void status_line(int line, const std::string& t) {
    if (line < 0 || line >= ROWS) return;
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
    else if (agent_on) s = agent_text;
    else for (int i = 0; i < ROWS; i++) s += status[i] + (i < ROWS - 1 ? "\n" : "");
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
    dirty = true;
    xSemaphoreGive(mu);
}

void ask_move(int d) {
    xSemaphoreTake(mu, portMAX_DELAY);
    int n = (int)ask_opts.size();
    if (n) ask_sel = ((ask_sel + d) % n + n) % n;
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

}  // namespace ui
