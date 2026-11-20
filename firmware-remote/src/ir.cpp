#include "ir.h"

#include <Arduino.h>
#include <driver/gpio.h>
#include <new>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <soc/soc_caps.h>
#include "settings.h"

#if __has_include(<driver/rmt.h>) && defined(SOC_RMT_TX_CANDIDATES_PER_GROUP)
#define IR_HAVE_RMT 1
#include <driver/rmt.h>
#include <esp_rom_gpio.h>
#include <soc/gpio_sig_map.h>
#else
#define IR_HAVE_RMT 0
#endif

namespace ir {

static int g_pin = -1;
static SemaphoreHandle_t mu;
// A send holds the LED for at most MAX_TOTAL_US plus its own wait margin; the
// lock is never waited on for longer than that, so no caller can hang on it.
static const uint32_t LOCK_WAIT_MS = 2000;

static bool take(std::string& err) {
    if (!mu) mu = xSemaphoreCreateMutex();
    if (mu && xSemaphoreTake(mu, pdMS_TO_TICKS(LOCK_WAIT_MS)) == pdTRUE) return true;
    err = mu ? "IR busy: another IR call has held the LED for over 2 s; try again"
             : "IR: no memory for its lock";
    return false;
}

// Pins that are never the LED's: the screen's, the flash's, USB's.
static bool reserved(int p) {
#if defined(ROOK_GAMEPAD)
    // T-Dongle-S3: LCD SPI (1..5), backlight 38; USB 19/20; flash/PSRAM 26..37
    return (p >= 1 && p <= 5) || p == 38 || p == 19 || p == 20 || (p >= 26 && p <= 37);
#elif defined(ROOK_BRIDGE)
    // XIAO ESP32-C3: OLED I2C 6/7 (D4/D5); flash 11..17; USB 18/19
    return p == 6 || p == 7 || (p >= 11 && p <= 17) || p == 18 || p == 19;
#else
    // XIAO ESP32-S3 handheld: OLED/PCF I2C 5/6; USB 19/20; flash/PSRAM 26..37
    return p == 5 || p == 6 || p == 19 || p == 20 || (p >= 26 && p <= 37);
#endif
}

static bool strapping(int p) {
#if CONFIG_IDF_TARGET_ESP32C3
    return p == 2 || p == 8 || p == 9;
#elif CONFIG_IDF_TARGET_ESP32S3
    return p == 0 || p == 3 || p == 45 || p == 46;
#else
    return false;
#endif
}

// The pin as a plain GPIO output, LOW: level first, then the output path.
static void drive_low(int p) {
    if (p < 0) return;
    gpio_set_level((gpio_num_t)p, 0);
    gpio_config_t g = {};
    g.pin_bit_mask = 1ULL << p;
    g.mode = GPIO_MODE_OUTPUT;           // iomux to GPIO, output path to the GPIO register (off the RMT)
    g.pull_up_en = GPIO_PULLUP_DISABLE;
    g.pull_down_en = GPIO_PULLDOWN_ENABLE;
    g.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&g);
#if IR_HAVE_RMT
    esp_rom_gpio_connect_out_signal(p, SIG_GPIO_OUT_IDX, false, false);
#endif
    gpio_set_level((gpio_num_t)p, 0);
}

void begin() {
    if (!mu) mu = xSemaphoreCreateMutex();
    std::string v = settings::kv("ir_pin");
    int p = v.empty() ? -1 : atoi(v.c_str());
    if (p >= 0 && (!GPIO_IS_VALID_OUTPUT_GPIO(p) || reserved(p))) p = -1;
    g_pin = p;
    drive_low(g_pin);
}

bool available() { return IR_HAVE_RMT; }

int pin() { return g_pin; }

bool set_pin(int p, std::string& err, std::string& warn) {
    if (p < -1 || (p >= 0 && !GPIO_IS_VALID_OUTPUT_GPIO(p))) {
        err = "bad args: pin is not an output GPIO on this chip";
        return false;
    }
    if (p >= 0 && reserved(p)) {
        err = "bad args: GPIO " + std::to_string(p) + " is in use (screen, flash or USB)";
        return false;
    }
    if (!take(err)) return false;
    g_pin = p;
    drive_low(g_pin);            // a previous pin stays an output LOW until restart
    settings::set_kv("ir_pin", p < 0 ? "" : std::to_string(p));
    xSemaphoreGive(mu);
    if (p >= 0 && strapping(p))
        warn = "GPIO " + std::to_string(p) +
               " is a strapping pin: the LED circuit may change how the chip boots";
    return true;
}

#if IR_HAVE_RMT
static const rmt_channel_t CH = RMT_CHANNEL_0;
static const uint32_t MAX_TICKS = 32767;      // an RMT item's duration field (1 us ticks)
static bool g_installed = false;              // under mu

// Lays the durations out as RMT items (mark = level 1), splitting long
// spaces; out null: only counts. Never writes a zero duration except the end.
static size_t lay_out(const std::vector<uint32_t>& d, rmt_item32_t* out) {
    size_t half = 0;
    auto put = [&](uint32_t level, uint32_t ticks) {
        if (out) {
            rmt_item32_t& it = out[half / 2];
            if (half % 2 == 0) { it.level0 = level; it.duration0 = ticks; it.level1 = 0; it.duration1 = 0; }
            else { it.level1 = level; it.duration1 = ticks; }
        }
        half++;
    };
    for (size_t i = 0; i < d.size(); i++) {
        uint32_t left = d[i], level = (i % 2 == 0) ? 1 : 0;
        if (level == 0 && i + 1 == d.size()) break;   // a trailing space: nothing to send
        while (left > 0) {
            uint32_t t = left > MAX_TICKS ? MAX_TICKS : left;
            put(level, t);
            left -= t;
        }
    }
    return (half + 1) / 2;   // an odd count ends on duration1 = 0: the end marker
}

static bool transmit(uint32_t carrier_hz, const std::vector<uint32_t>& d, Sent& s, std::string& err) {
    if (g_pin < 0) { err = "no IR pin set: ir action pin {pin: <gpio>} first"; return false; }
    if (d.empty()) { err = "bad args: nothing to send"; return false; }
    if (carrier_hz < 20000 || carrier_hz > 100000) { err = "bad args: carrier is 20000..100000 Hz"; return false; }
    uint64_t total = 0;
    size_t sent_n = d.size() % 2 == 0 ? d.size() - 1 : d.size();   // a trailing space is not sent
    for (size_t i = 0; i < sent_n; i++) {
        if (d[i] == 0) { err = "bad args: a duration of 0 at " + std::to_string(i); return false; }
        if (i % 2 == 0 && d[i] > MAX_MARK_US) {
            err = "refused: mark " + std::to_string(i) + " is " + std::to_string(d[i]) + " us (max " +
                  std::to_string(MAX_MARK_US) + ")";
            return false;
        }
        total += d[i];
    }
    if (total > MAX_TOTAL_US) {
        err = "refused: " + std::to_string((uint32_t)std::min<uint64_t>(total, 0xFFFFFFFF)) +
              " us in all (max " + std::to_string(MAX_TOTAL_US) + ")";
        return false;
    }

    size_t n = lay_out(d, nullptr);
    rmt_item32_t* items = (rmt_item32_t*)calloc(n, sizeof(rmt_item32_t));
    if (!items) { err = "out of memory for " + std::to_string(n) + " RMT items"; return false; }
    lay_out(d, items);

    if (!take(err)) { free(items); return false; }
    int p = g_pin;
    if (p < 0) {
        xSemaphoreGive(mu);
        free(items);
        err = "no IR pin set";
        return false;
    }
    rmt_config_t c = RMT_DEFAULT_CONFIG_TX((gpio_num_t)p, CH);
    c.clk_div = 80;                                 // APB 80 MHz: 1 us ticks
    c.tx_config.carrier_en = true;
    c.tx_config.carrier_freq_hz = carrier_hz;
    c.tx_config.carrier_duty_percent = 33;
    c.tx_config.carrier_level = RMT_CARRIER_LEVEL_HIGH;
    c.tx_config.idle_level = RMT_IDLE_LEVEL_LOW;
    c.tx_config.idle_output_en = true;
    gpio_set_level((gpio_num_t)p, 0);
    // The driver is installed once and kept (installing and removing it per send
    // allocated and freed its state and interrupt every time, on a heap that is
    // short and in pieces). rmt_config points the channel at this send's pin
    // and carrier; drive_low takes the pin back to a plain GPIO after it.
    esp_err_t e = ESP_OK;
    bool stale = false;
    if (!g_installed) {
        e = rmt_config(&c);
        if (e == ESP_OK) e = rmt_driver_install(CH, 0, 0);
        if (e == ESP_OK) g_installed = true;
    } else {
        // The driver's write waits for the last send's end with no limit: make
        // sure it has ended (a lost end interrupt would leave it waiting forever).
        if (rmt_wait_tx_done(CH, 0) != ESP_OK) stale = true, e = ESP_ERR_INVALID_STATE;
        if (e == ESP_OK) e = rmt_config(&c);
    }
    if (e == ESP_OK) e = rmt_write_items(CH, items, (int)n, false);
    if (e == ESP_OK) e = rmt_wait_tx_done(CH, pdMS_TO_TICKS(total / 1000 + 300));
    if (e != ESP_OK && g_installed) {
        // Start over on the next send: stopped, and removed (it does not wait
        // for the send's end when the write did not ask to: rmt_write_items(..., false)).
        rmt_tx_stop(CH);
        rmt_driver_uninstall(CH);
        g_installed = false;
    }
    drive_low(p);                                   // never left HIGH, sent or not
    xSemaphoreGive(mu);
    free(items);

    if (stale) { err = "rmt: the last send never ended; the RMT is reset, send again"; return false; }
    if (e != ESP_OK) { err = std::string("rmt: ") + esp_err_to_name(e); return false; }
    s.pin = p;
    s.duration_us = (uint32_t)total;
    s.carrier_hz = carrier_hz;
    s.edges = sent_n;
    return true;
}
#else
static bool transmit(uint32_t, const std::vector<uint32_t>&, Sent&, std::string& err) {
    err = "this build has no RMT: IR transmit is not available";
    return false;
}
#endif

bool send_raw(uint32_t carrier_hz, const std::vector<uint32_t>& durations, Sent& s, std::string& err) {
    return transmit(carrier_hz, durations, s, err);
}

bool send_nec(uint16_t address, uint8_t command, int repeats, Sent& s, std::string& err) {
    if (repeats < 0) repeats = 0;
    if (repeats > 4) { err = "refused: at most 4 repeats (500 ms)"; return false; }
    uint32_t bits = address > 0xFF ? address : (uint32_t)(address & 0xFF) | (uint32_t)(~address & 0xFF) << 8;
    bits |= (uint32_t)command << 16 | (uint32_t)(uint8_t)~command << 24;
    std::vector<uint32_t> d;
    d.reserve(68 + repeats * 4);
    d.push_back(9000);
    d.push_back(4500);
    uint32_t frame = 9000 + 4500 + 560;
    for (int i = 0; i < 32; i++) {
        uint32_t sp = (bits >> i) & 1 ? 1690 : 560;
        d.push_back(560);
        d.push_back(sp);
        frame += 560 + sp;
    }
    d.push_back(560);
    for (int r = 0; r < repeats; r++) {
        d.push_back(108000 - frame);     // to the next frame's start, 108 ms after this one's
        d.push_back(9000);
        d.push_back(2250);
        d.push_back(560);
        frame = 9000 + 2250 + 560;
    }
    return transmit(38000, d, s, err);
}

bool send_nec_repeat(Sent& s, std::string& err) {
    std::vector<uint32_t> d{9000, 2250, 560};
    return transmit(38000, d, s, err);
}

// ---- the queue -------------------------------------------------------------------

struct Post {
    PostKind kind;
    uint8_t command;
    uint16_t address;
    uint32_t carrier;
    std::vector<uint32_t> d;
};
static QueueHandle_t posts;
static void (*fail_)(const std::string&) = nullptr;

void on_fail(void (*fail)(const std::string&)) { fail_ = fail; }

static void post_task(void* arg) {
    QueueHandle_t q = (QueueHandle_t)arg;
    for (;;) {
        Post* p = nullptr;
        if (xQueueReceive(q, &p, portMAX_DELAY) != pdTRUE || !p) continue;
        Sent s;
        std::string err;
        bool ok = p->kind == POST_NEC ? send_nec(p->address, p->command, 0, s, err)
                : p->kind == POST_NEC_REPEAT ? send_nec_repeat(s, err)
                : send_raw(p->carrier, p->d, s, err);
        delete p;
        if (!ok && fail_) fail_(err);
    }
}

bool post(PostKind kind, uint16_t address, uint8_t command, uint32_t carrier_hz, const std::vector<uint32_t>* durations) {
    if (!posts) {
        // The task takes the queue as its argument: it runs at once (a step above the loop).
        QueueHandle_t q = xQueueCreate(3, sizeof(Post*));
        if (!q) return false;
        if (xTaskCreate(post_task, "irtx", 3584, q, 2, nullptr) != pdPASS) {
            vQueueDelete(q);
            return false;
        }
        posts = q;
    }
    if (uxQueueSpacesAvailable(posts) == 0) return false;
    Post* p = new (std::nothrow) Post();
    if (!p) return false;
    p->kind = kind;
    p->address = address;
    p->command = command;
    p->carrier = carrier_hz;
    if (durations) p->d = *durations;
    if (xQueueSend(posts, &p, 0) != pdTRUE) {
        delete p;
        return false;
    }
    return true;
}

}  // namespace ir
