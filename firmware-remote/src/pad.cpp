// The gamepad build's input: a game controller on the ESP32-S3's USB port (the
// S3 is the host), standing where the bridge has a Bluetooth remote. It fills
// bt.h's side of the bargain (keys by name, the key held now) so the rest of
// the firmware does not know the difference, and adds what only a pad has
// (pad.h): several keys held at once for the games, a light bar, motors.
// A DualShock 4 is the pad it knows.
#ifdef ROOK_GAMEPAD
#include "pad.h"
#include <stdarg.h>
#include "bt.h"
#include "tetris.h"
extern "C" {
#include "usb/usb_host.h"
}

namespace {

std::function<void(const char*)> log_;

void logf(const char* fmt, ...) {
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (log_) log_(b);
}

// What can be held, a bit each, and the name each goes by as a key.
enum : uint32_t {
    UP = 1, DOWN = 2, LEFT = 4, RIGHT = 8, CROSS = 16, CIRCLE = 32, SQUARE = 64, TRIANGLE = 128,
    L1 = 256, R1 = 512, L2 = 1024, R2 = 2048, SHARE = 4096, OPTIONS = 8192, L3 = 16384, R3 = 32768,
    PSKEY = 65536, TOUCH = 131072,
};
const int NKEYS = 18;
const char* const NAME[NKEYS] = {"up", "down", "left", "right", "ok", "back", "mute", "play", "vol_down", "vol_up",
                                 "ch_down", "ch_up", "input", "guide", "assistant", "profile", "home", "power"};

portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
volatile bool on = false;
volatile uint32_t bits = 0, reports = 0;
uint16_t vid = 0, pid = 0;
uint8_t last[64];
volatile uint8_t last_len = 0;
uint8_t queue[16];                   // keys pressed, not yet taken (indexes into NAME)
volatile uint8_t q_head = 0, q_tail = 0;

usb_host_client_handle_t client;
usb_device_handle_t dev;
usb_transfer_t *xfer = nullptr, *xfer_out = nullptr;
uint8_t hid_if = 0;
volatile int new_addr = -1;
volatile bool gone = false, out_busy = false;

bool is_ds4() { return vid == 0x054C && (pid == 0x05C4 || pid == 0x09CC); }

void push(int key) {
    uint8_t next = (q_head + 1) & 15;
    if (next == q_tail) return;      // full: the press is lost
    queue[q_head] = (uint8_t)key;
    q_head = next;
}

// A DualShock 4's report 1: what is held. The left stick counts as the d-pad.
uint32_t ds4_bits(const uint8_t* r) {
    static const uint32_t HAT[8] = {UP, UP | RIGHT, RIGHT, DOWN | RIGHT, DOWN, DOWN | LEFT, LEFT, UP | LEFT};
    uint32_t b = (r[5] & 15) < 8 ? HAT[r[5] & 15] : 0;
    if (r[1] < 56) b |= LEFT;
    if (r[1] > 200) b |= RIGHT;
    if (r[2] < 56) b |= UP;
    if (r[2] > 200) b |= DOWN;
    if (r[5] & 0x10) b |= SQUARE;
    if (r[5] & 0x20) b |= CROSS;
    if (r[5] & 0x40) b |= CIRCLE;
    if (r[5] & 0x80) b |= TRIANGLE;
    if (r[6] & 0x01) b |= L1;
    if (r[6] & 0x02) b |= R1;
    if (r[6] & 0x04) b |= L2;
    if (r[6] & 0x08) b |= R2;
    if (r[6] & 0x10) b |= SHARE;
    if (r[6] & 0x20) b |= OPTIONS;
    if (r[6] & 0x40) b |= L3;
    if (r[6] & 0x80) b |= R3;
    if (r[7] & 0x01) b |= PSKEY;
    if (r[7] & 0x02) b |= TOUCH;
    return b;
}

void on_report(usb_transfer_t* t) {
    if (t->status == USB_TRANSFER_STATUS_COMPLETED && t->actual_num_bytes > 0) {
        int n = t->actual_num_bytes > 64 ? 64 : t->actual_num_bytes;
        uint32_t now = is_ds4() && n >= 10 && t->data_buffer[0] == 1 ? ds4_bits(t->data_buffer) : 0;
        portENTER_CRITICAL(&mux);
        memcpy(last, t->data_buffer, n);
        last_len = n;
        uint32_t pressed = reports ? now & ~bits : 0;        // what is held as it is plugged in is not a press
        bits = now;
        for (int k = 0; k < NKEYS; k++)
            if (pressed & (1u << k)) push(k);
        portEXIT_CRITICAL(&mux);
        reports++;
    } else if (t->status == USB_TRANSFER_STATUS_NO_DEVICE || t->status == USB_TRANSFER_STATUS_CANCELED) {
        return;
    }
    if (on) usb_host_transfer_submit(t);
}

void on_out(usb_transfer_t*) { out_busy = false; }

void on_client(const usb_host_client_event_msg_t* m, void*) {
    if (m->event == USB_HOST_CLIENT_EVENT_NEW_DEV) new_addr = m->new_dev.address;
    else if (m->event == USB_HOST_CLIENT_EVENT_DEV_GONE) gone = true;
}

// Open what was plugged in and start reading its first HID interface.
void open_device(int addr) {
    if (dev) return;
    if (usb_host_device_open(client, addr, &dev) != ESP_OK) { dev = nullptr; return; }
    const usb_device_desc_t* dd;
    usb_host_get_device_descriptor(dev, &dd);
    vid = dd->idVendor;
    pid = dd->idProduct;
    const usb_config_desc_t* cfg;
    usb_host_get_active_config_descriptor(dev, &cfg);
    const uint8_t* p = (const uint8_t*)cfg;
    int total = cfg->wTotalLength, cur_if = -1, cur_alt = 0, cur_class = 0, in_if = -1, ep_in = 0, mps = 0, ep_out = 0;
    for (int at = 0; at + 2 <= total && p[at]; at += p[at]) {
        int type = p[at + 1];
        if (type == 4 && at + 9 <= total) {
            cur_if = p[at + 2];
            cur_alt = p[at + 3];
            cur_class = p[at + 5];
        } else if (type == 5 && at + 7 <= total && cur_class == 3 && cur_alt == 0 && (p[at + 3] & 3) == 3) {
            int ep = p[at + 2];
            if ((ep & 0x80) && in_if < 0) {
                in_if = cur_if;
                ep_in = ep;
                mps = (p[at + 4] | (p[at + 5] << 8)) & 0x7FF;
            } else if (!(ep & 0x80) && cur_if == in_if) {
                ep_out = ep;
            }
        }
    }
    logf("pad: %04x:%04x, HID interface %d%s", vid, pid, in_if, is_ds4() ? " (a DualShock 4)" : "");
    if (in_if < 0 || usb_host_interface_claim(client, dev, in_if, 0) != ESP_OK) return;
    hid_if = (uint8_t)in_if;
    if (mps < 8) mps = 8;
    if (usb_host_transfer_alloc(mps, 0, &xfer) != ESP_OK) { xfer = nullptr; return; }
    xfer->device_handle = dev;
    xfer->bEndpointAddress = ep_in;
    xfer->callback = on_report;
    xfer->context = nullptr;
    xfer->num_bytes = mps;
    out_busy = false;
    if (ep_out && usb_host_transfer_alloc(64, 0, &xfer_out) == ESP_OK) {
        xfer_out->device_handle = dev;
        xfer_out->bEndpointAddress = ep_out;
        xfer_out->callback = on_out;
        xfer_out->context = nullptr;
    } else {
        xfer_out = nullptr;
    }
    reports = 0;
    bits = 0;
    on = true;
    usb_host_transfer_submit(xfer);
}

void close_device() {
    on = false;
    bits = 0;
    if (dev) {
        usb_host_interface_release(client, dev, hid_if);
        if (xfer) usb_host_transfer_free(xfer);
        if (xfer_out) usb_host_transfer_free(xfer_out);
        xfer = xfer_out = nullptr;
        usb_host_device_close(client, dev);
        dev = nullptr;
    }
    logf("pad: gone");
    vid = pid = 0;
    last_len = 0;
}

void lib_task(void*) {
    usb_host_config_t c;
    memset(&c, 0, sizeof c);
    c.intr_flags = ESP_INTR_FLAG_LEVEL1;
    esp_err_t e = usb_host_install(&c);
    logf("pad: usb host %s", esp_err_to_name(e));
    for (;;) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
    }
}

void client_task(void*) {
    vTaskDelay(pdMS_TO_TICKS(300));          // the library first
    usb_host_client_config_t c;
    memset(&c, 0, sizeof c);
    c.max_num_event_msg = 5;
    c.async.client_event_callback = on_client;
    usb_host_client_register(&c, &client);
    for (;;) {
        usb_host_client_handle_events(client, pdMS_TO_TICKS(200));
        if (gone) {
            gone = false;
            close_device();
        }
        if (new_addr >= 0) {
            int a = new_addr;
            new_addr = -1;
            open_device(a);
        }
    }
}

// What the pad is told: its light bar, its motors (until rumble_until).
uint8_t want[5] = {0, 0, 24, 0, 0}, sent[5] = {1, 1, 1, 1, 1};
uint32_t rumble_until = 0, last_sent = 0;

bool output() {
    if (!on || !xfer_out || !is_ds4() || out_busy) return false;
    uint8_t* d = xfer_out->data_buffer;
    memset(d, 0, 32);
    d[0] = 0x05;                     // a DualShock 4's output report
    d[1] = 0x07;                     // sets: motors, light bar, its blinking
    d[4] = want[3];
    d[5] = want[4];
    d[6] = want[0];
    d[7] = want[1];
    d[8] = want[2];
    xfer_out->num_bytes = 32;
    out_busy = true;
    if (usb_host_transfer_submit(xfer_out) != ESP_OK) {
        out_busy = false;
        return false;
    }
    return true;
}

}  // namespace

namespace pad {

uint16_t game_keys(bool flat) {
    uint32_t b = bits;
    uint16_t k = 0;
    if (b & LEFT) k |= tetris::LEFT;
    if (b & RIGHT) k |= tetris::RIGHT;
    if (b & DOWN) k |= tetris::DOWN;
    if (b & UP) k |= tetris::ROTATE;
    if (b & CROSS) k |= tetris::DROP;
    if (b & OPTIONS) k |= tetris::PAUSE;
    if (b & (SHARE | PSKEY)) k |= tetris::QUIT;
    if (flat) {                      // 2048, Towers: a cursor and a few choices
        if (b & (L1 | R1 | TRIANGLE)) k |= tetris::ROTATE_BACK;
        if (b & SQUARE) k |= tetris::HOLD;
    } else {                         // Tetris, Swarm
        if (b & (CIRCLE | TRIANGLE | R2)) k |= tetris::ROTATE;
        if (b & (SQUARE | L2)) k |= tetris::ROTATE_BACK;
        if (b & (L1 | R1)) k |= tetris::HOLD;
    }
    return k;
}

void light(uint8_t r, uint8_t g, uint8_t b) {
    want[0] = r;
    want[1] = g;
    want[2] = b;
}

void rumble(uint8_t weak, uint8_t strong, uint32_t ms) {
    want[3] = weak;
    want[4] = strong;
    rumble_until = (millis() + ms) | 1;
}

void tick() {
    uint32_t ms = millis();
    if (rumble_until && (int32_t)(ms - rumble_until) >= 0) {
        rumble_until = 0;
        want[3] = want[4] = 0;
    }
    if (!on) {
        sent[0] = want[0] + 1;       // a pad plugged in again is told afresh
        return;
    }
    if ((memcmp(sent, want, 5) || ms - last_sent > 3000) && output()) {
        memcpy(sent, want, 5);
        last_sent = ms;
    }
}

}  // namespace pad

// ---- bt.h, answered by the pad ------------------------------------------------

namespace bt {

void begin(std::function<void(const char*)> log) {
    log_ = log;
    xTaskCreatePinnedToCore(lib_task, "usb_lib", 4096, nullptr, 10, nullptr, 0);
    xTaskCreatePinnedToCore(client_task, "usb_client", 6144, nullptr, 9, nullptr, 0);
}

void pair(uint32_t, const std::string&, bool) {}
std::string pair_error() { return ""; }
void look(bool) {}
bool looking() { return false; }
std::vector<Heard> heard() { return {}; }
void pause(bool) {}
void forget() {}
bool forget(const std::string&) { return false; }
std::vector<BondInfo> bonded() { return {}; }
bool bonds_full() { return false; }
std::string oldest_bond() { return ""; }
bool saved() { return true; }        // nothing to pair: whatever is plugged in is it
std::string state() { return on ? "connected" : "waiting"; }

std::string name() {
    if (!vid) return "";
    if (is_ds4()) return "DualShock 4";
    char b[20];
    snprintf(b, sizeof b, "pad %04x:%04x", vid, pid);
    return b;
}

std::string address() { return "usb"; }

std::string last_report() {
    uint8_t r[16];
    int n;
    portENTER_CRITICAL(&mux);
    n = last_len > 16 ? 16 : last_len;
    memcpy(r, last, n);
    portEXIT_CRITICAL(&mux);
    std::string s;
    char b[4];
    for (int i = 0; i < n; i++) {
        snprintf(b, sizeof b, "%02x", r[i]);
        s += b;
    }
    return s;
}

// A key pressed since the last call. A direction held goes on pressing itself
// (after 400 ms, every 140), so a list can be run down.
bool next_key(std::string& name) {
    static uint32_t dir_since = 0, dir_next = 0, dir_was = 0;
    int key = -1;
    portENTER_CRITICAL(&mux);
    if (q_tail != q_head) {
        key = queue[q_tail];
        q_tail = (q_tail + 1) & 15;
    }
    portEXIT_CRITICAL(&mux);
    uint32_t ms = millis(), dir = bits & (UP | DOWN | LEFT | RIGHT);
    if (dir != dir_was) {
        dir_was = dir;
        dir_since = ms;
        dir_next = ms + 400;
    } else if (key < 0 && dir && !(dir & (dir - 1)) && (int32_t)(ms - dir_next) >= 0) {       // one direction, held
        dir_next = ms + 140;
        key = __builtin_ctz(dir);
    }
    (void)dir_since;
    if (key < 0) return false;
    name = NAME[key];
    return true;
}

std::string held() {
    uint32_t b = bits;
    return b ? NAME[__builtin_ctz(b)] : "";
}

bool voice_ready() { return false; }  // no microphone on it (yet)
bool voice_open() { return false; }
void voice_start() {}
void voice_stop() {}
int voice_level() { return 0; }
int voice_codec() { return 1; }
bool voice_spoke() { return false; }
bool voice_take(std::string&, bool&, size_t) { return false; }

std::string services() {
    char b[40];
    snprintf(b, sizeof b, "usb hid %04x:%04x", vid, pid);
    return on ? b : "";
}

std::string voice_info() { return ""; }
bool find_ready() { return false; }
void find(int) {}
std::string find_info() { return ""; }

bool ir_ready() { return false; }     // no IR service on a USB pad
static std::vector<std::string> no_ir() { return {"the pad has no IR service"}; }
std::vector<std::string> ir_write(const std::string&, const std::string&) { return no_ir(); }
std::vector<std::string> ir_program(uint16_t, const std::string&) { return no_ir(); }
std::vector<std::string> ir_suppress(const std::vector<uint16_t>&) { return no_ir(); }
std::vector<IrEvent> ir_events(uint32_t) { return {}; }
void ir_forget_events() {}

}  // namespace bt
#endif  // ROOK_GAMEPAD
