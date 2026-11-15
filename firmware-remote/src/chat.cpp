#include "chat.h"
#include <Adafruit_GFX.h>
#include <algorithm>
#include <deque>
#include <vector>

namespace {

struct Msg { std::string who, text; };
const size_t KEEP = 12, MAX_TEXT = 700;
std::deque<Msg> msgs;
std::string status_line;
int busy = 0;                // what status_line says is going on: 1 listening, 2 thinking, 0 neither
uint32_t busy_t0 = 0;
const int HIST = 26;
uint8_t hist[HIST];          // the loudness heard lately, oldest first
int top = 0;                 // the first pixel row of the conversation on screen
bool stick = true;           // follow the newest (until the person scrolls up)
SemaphoreHandle_t mu;

// Lines of at most cols characters, broken at a space when one is in reach.
std::vector<std::string> wrap(const std::string& t, int cols) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i <= t.size() && out.size() < 200) {
        size_t nl = t.find('\n', i);
        std::string para = t.substr(i, nl == std::string::npos ? std::string::npos : nl - i);
        i = nl == std::string::npos ? t.size() + 1 : nl + 1;
        if (para.empty()) continue;
        while (!para.empty()) {
            if ((int)para.size() <= cols) {
                out.push_back(para);
                break;
            }
            int cut = cols, skip = 0;
            for (int k = cols; k > 1; k--)
                if (para[k] == ' ') { cut = k; skip = 1; break; }
            out.push_back(para.substr(0, cut));
            para.erase(0, cut + skip);
        }
    }
    if (out.empty()) out.push_back("");
    return out;
}

struct Box { int y, h, w; bool mine, bare; std::vector<std::string> lines; };

// Every message laid out from the top down; returns the height of it all.
int layout(int W, std::vector<Box>& boxes) {
    bool wide = W >= 64;
    int cols = wide ? (W - 16) / 6 : W / 6, y = 0;
    for (auto& m : msgs) {
        Box b;
        b.mine = m.who == "you";
        b.bare = !wide || m.who == "note";
        b.lines = wrap(m.text, b.bare && wide ? (W - 4) / 6 : cols);
        size_t longest = 0;
        for (auto& l : b.lines) longest = std::max(longest, l.size());
        b.w = (int)longest * 6 + (b.bare ? 0 : 7);
        b.h = (int)b.lines.size() * 8 + (b.bare ? 1 : 5);
        b.y = y;
        y += b.h + 3;
        boxes.push_back(b);
    }
    return y;
}

}  // namespace

namespace chat {

void begin() { mu = xSemaphoreCreateMutex(); }

void add(const std::string& who, const std::string& text) {
    if (text.empty()) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    msgs.push_back({who == "you" || who == "note" ? who : "agent", text.substr(0, MAX_TEXT)});
    while (msgs.size() > KEEP) msgs.pop_front();
    if (who != "you") status_line.clear();      // an answer (or a note about it): no longer waiting
    stick = true;
    xSemaphoreGive(mu);
}

void clear() {
    xSemaphoreTake(mu, portMAX_DELAY);
    msgs.clear();
    status_line.clear();
    top = 0;
    stick = true;
    xSemaphoreGive(mu);
}

void status(const std::string& s) {
    xSemaphoreTake(mu, portMAX_DELAY);
    status_line = s;
    stick = true;
    int now = s.rfind("Listening", 0) == 0 ? 1 : s.rfind("Thinking", 0) == 0 ? 2 : 0;
    if (now != busy) {
        busy = now;
        busy_t0 = millis();
        memset(hist, 0, sizeof hist);
    }
    xSemaphoreGive(mu);
}

void level(int percent) {
    xSemaphoreTake(mu, portMAX_DELAY);
    memmove(hist, hist + 1, HIST - 1);
    hist[HIST - 1] = (uint8_t)std::max(0, std::min(100, percent));
    xSemaphoreGive(mu);
}

void scroll(int pixels) {
    xSemaphoreTake(mu, portMAX_DELAY);
    top += pixels;               // draw() keeps it within the conversation
    stick = false;
    xSemaphoreGive(mu);
}

size_t count() {
    xSemaphoreTake(mu, portMAX_DELAY);
    size_t n = msgs.size();
    xSemaphoreGive(mu);
    return n;
}

void draw(Adafruit_GFX& d) {
    xSemaphoreTake(mu, portMAX_DELAY);
    int W = d.width(), H = d.height();
    bool wide = W >= 64;
    d.setTextSize(1);
    d.setTextWrap(false);
    uint32_t ms = millis();
    int secs = (int)((ms - busy_t0) / 1000);
    char clock[8];
    snprintf(clock, sizeof clock, "%d:%02d", secs / 60, secs % 60);
    if (busy == 1 && W >= 100) {
        // Listening has the whole screen: a microphone, how long, what it hears
        // as bars moving left, and how to send it.
        d.setTextColor(1);
        d.fillRoundRect(3, 1, 5, 8, 2, 1);                   // the microphone
        d.drawLine(1, 6, 1, 8, 1);
        d.drawLine(9, 6, 9, 8, 1);
        d.drawFastHLine(2, 9, 7, 1);
        d.drawFastVLine(5, 10, 2, 1);
        d.setCursor(15, 2);
        d.print("Listening");
        d.setCursor(W - (int)strlen(clock) * 6 - 1, 2);
        d.print(clock);
        if ((ms / 400) & 1) d.fillCircle(W - (int)strlen(clock) * 6 - 8, 5, 2, 1);      // recording
        const int MID = 31, X0 = (W - HIST * 5 + 2) / 2;
        for (int i = 0; i < HIST; i++) {
            int h = 2 + hist[i] * 26 / 100;
            d.fillRoundRect(X0 + i * 5, MID - h / 2, 3, h, 1, 1);
        }
        const char* hint = "any key to send";
        d.setCursor((W - (int)strlen(hint) * 6) / 2, H - 9);
        d.print(hint);
        xSemaphoreGive(mu);
        return;
    }
    bool strip = busy == 2 && W >= 100;                      // thinking: a taller strip, with dots that take turns
    int view = H - (status_line.empty() ? 0 : strip ? 14 : 10);
    std::vector<Box> boxes;
    int total = layout(W, boxes);
    int last = std::max(0, total - view);
    if (stick || top > last) top = last;
    if (top < 0) top = 0;
    if (top == last) stick = true;               // back at the newest: follow it again
    if (msgs.empty() && status_line.empty()) {
        d.setTextColor(1);
        auto hint = wrap("Press the Assistant key and talk", W / 6);
        for (size_t i = 0; i < hint.size(); i++) {
            d.setCursor(1, 8 + (int)i * 9);
            d.print(hint[i].c_str());
        }
    }
    for (auto& b : boxes) {
        int y = b.y - top;
        if (y + b.h <= 0 || y >= view) continue;
        int x = b.bare ? (wide ? (W - b.w) / 2 : 0) : b.mine ? W - 4 - b.w : 0;
        if (!b.bare) {
            if (b.mine) d.fillRoundRect(x, y, b.w, b.h, 3, 1);
            else d.drawRoundRect(x, y, b.w, b.h, 3, 1);
            // the tail, at the speaker's bottom corner
            if (b.mine) d.fillTriangle(x + b.w - 2, y + b.h - 4, x + b.w + 2, y + b.h - 1, x + b.w - 6, y + b.h - 1, 1);
            else d.drawLine(x, y + b.h - 4, x, y + b.h + 1, 1);
        }
        d.setTextColor(!b.bare && b.mine ? 0 : 1);
        for (size_t i = 0; i < b.lines.size(); i++) {
            int ly = y + (b.bare ? 0 : 3) + (int)i * 8;
            if (ly + 8 <= 0 || ly >= view) continue;
            d.setCursor(x + (b.bare ? 0 : 4), ly);
            d.print(b.lines[i].c_str());
        }
    }
    d.setTextColor(1);
    if (!status_line.empty()) {                  // what is happening now, on its own strip
        d.fillRect(0, view, W, H - view, 0);
        d.drawFastHLine(0, view, W, 1);
        if (strip) {
            for (int i = 0; i < 3; i++) {
                int ph = (int)((ms / 110 + (3 - i) * 2) % 9);            // each rises a little after the one before
                int up = ph < 2 ? ph * 2 : ph < 4 ? (4 - ph) * 2 : 0;
                d.fillCircle(6 + i * 9, view + 9 - up, 2, 1);
            }
            d.setCursor(34, view + 4);
            d.print("thinking");
            d.setCursor(W - (int)strlen(clock) * 6 - 3, view + 4);
            d.print(clock);
        } else {
            d.setCursor(1, view + 2);
            d.print(status_line.substr(0, W / 6).c_str());
        }
    }
    if (total > view) {                          // where this is in the whole, down the right edge
        int h = std::max(4, view * view / total);
        d.drawFastVLine(W - 1, (view - h) * top / std::max(1, last), h, 1);
    }
    xSemaphoreGive(mu);
}

void fill(JsonArray a) {
    xSemaphoreTake(mu, portMAX_DELAY);
    for (auto& m : msgs) {
        JsonObject o = a.add<JsonObject>();
        o["who"] = m.who;
        o["text"] = m.text;
    }
    xSemaphoreGive(mu);
}

}  // namespace chat
