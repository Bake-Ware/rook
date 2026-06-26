#include "display.h"
#include "config.h"
#include "serial_buf.h"
#include "storage.h"
#include "http_routes.h"
#include "ws.h"
#include "device_mode.h"
#include "telesthete.h"
#include "ble_hid.h"
#include <WiFi.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include "esp_freertos_hooks.h"

static SPIClass lcdSPI(FSPI);
static Adafruit_ST7735 tft(&lcdSPI, LCD_CS, LCD_DC, LCD_RST);

// 1Hz ring buffers (80 samples = 80s history).
static uint8_t netGraph[80] = {0};
static uint8_t cpuGraph[80] = {0};
static int graphIdx = 0;
static uint32_t prevHttpCount = 0;

// CPU% via FreeRTOS idle-hook counters (one per core). Calibrated to a
// running max — load% = (max - delta) / max * 100. First ~few seconds
// of fully-idle ticks establish the baseline.
static volatile uint32_t idleCount0 = 0;
static volatile uint32_t idleCount1 = 0;
static uint32_t prevIdle0 = 0;
static uint32_t prevIdle1 = 0;
static uint32_t idleMax = 1;
static bool idleHooksRegistered = false;

static bool IRAM_ATTR idleHookCore0() { idleCount0++; return true; }
static bool IRAM_ATTR idleHookCore1() { idleCount1++; return true; }

void displayTask(void* param) {
    lcdSPI.begin(LCD_SCLK, -1, LCD_MOSI);
    tft.initR(INITR_MINI160x80_PLUGIN);
    tft.setRotation(2);  // portrait 80x160 (180° from rotation 0)
    tft.invertDisplay(false);
    tft.setTextWrap(false);
    tft.fillScreen(ST77XX_BLACK);

    // Backlight on (active low)
    pinMode(LCD_BL, OUTPUT);
    digitalWrite(LCD_BL, LOW);

    if (!idleHooksRegistered) {
        esp_register_freertos_idle_hook_for_cpu(idleHookCore0, 0);
        esp_register_freertos_idle_hook_for_cpu(idleHookCore1, 1);
        idleHooksRegistered = true;
    }

    for (;;) {
        wifi_mode_t wm = WiFi.getMode();
        bool staConnected = (WiFi.status() == WL_CONNECTED);
        bool wifiOk = (wm == WIFI_AP) || (wm == WIFI_AP_STA) || staConnected;
        String ip = staConnected
            ? WiFi.localIP().toString()
            : WiFi.softAPIP().toString();
        const char* mode = staConnected ? "STA" : "AP";

        tft.fillScreen(ST77XX_BLACK);

        // Header — inverted lime bar with black "R00K" wordmark
        tft.fillRect(0, 0, 80, 18, 0x67E0);
        tft.setTextSize(2);
        tft.setTextColor(ST77XX_BLACK);
        tft.setCursor(2, 2);
        tft.print("R00K");
        tft.setTextSize(1);
        // Left: connected SSID (truncated). Replaces firmware version on
        // the chrome — version is still in /status.
        tft.setCursor(2, 20);
        if (staConnected) {
            tft.setTextColor(0x67E0);
            String ssid = WiFi.SSID();
            if (ssid.length() > 8) ssid = ssid.substring(0, 8);
            tft.print(ssid);
        } else {
            tft.setTextColor(0xB596);
            tft.print("no-sta");
        }
        tft.setCursor(62, 20);
        tft.setTextColor(wifiOk ? 0x67E0 : ST77XX_RED);
        tft.print(mode);

        // IP — single line, size 1 (6x8 px). Last octet in lime.
        tft.setTextSize(1);
        int lastDot = ip.lastIndexOf('.');
        String pre = (lastDot > 0) ? ip.substring(0, lastDot + 1) : ip;
        String last = (lastDot > 0) ? ip.substring(lastDot + 1) : String("");
        int ipW = (int)ip.length() * 6;
        int ipX = (80 - ipW) / 2;
        if (ipX < 0) ipX = 0;
        tft.setCursor(ipX, 32);
        tft.setTextColor(ST77XX_WHITE);
        tft.print(pre);
        tft.setTextColor(0x67E0);
        tft.print(last);

        // Compact status row at y=44
        unsigned long sec = millis() / 1000;
        tft.setCursor(2, 44);
        if (fileTransferActive) {
            tft.setTextColor(0x67E0);
            tft.printf("X:%uB", (unsigned)fileTransferBytes);
        } else {
            tft.setTextColor(0xB596);
            tft.printf("%lum%02lus W%d H%u",
                       sec / 60, sec % 60,
                       wsSerial.count(), getHttpRequestCount());
        }
        // HID kill-switch badge (right edge)
        if (!hidEnabled()) {
            tft.setCursor(70, 44);
            tft.setTextColor(ST77XX_RED);
            tft.print("X");
        }

        // Hub-status badge — single letter after the STA mode indicator on
        // the top row. Lime = OK, red = no recent traffic.
        bool hubOk = telestheteHubOk();
        tft.setCursor(74, 20);
        tft.setTextColor(hubOk ? 0x67E0 : ST77XX_RED);
        tft.print(hubOk ? "H" : "h");

        // BLE HID dot — solid blue when a host is paired+connected, blinking
        // 1Hz while advertising and waiting. We toggle a static flag each
        // render instead of sampling millis(), so the 1s redraw period
        // doesn't alias.
        static bool ble_blink_phase = false;
        ble_blink_phase = !ble_blink_phase;
        bool bleConn = bleHidConnected();
        bool show = bleConn || ble_blink_phase;
        if (show) tft.fillCircle(53, 9, 4, 0x041F);  // ST77XX blue

        // ---- Sample collection ----
        // Network: HTTP request delta + current WS connection count.
        uint32_t curHttp = getHttpRequestCount();
        uint32_t deltaHttp = curHttp - prevHttpCount;
        prevHttpCount = curHttp;
        uint32_t netSample = deltaHttp + (uint32_t)wsSerial.count();
        if (netSample > 255) netSample = 255;
        netGraph[graphIdx] = (uint8_t)netSample;

        // CPU: idle-hook counters across both cores. Higher tick rate = idler.
        uint32_t i0 = idleCount0;
        uint32_t i1 = idleCount1;
        uint32_t dIdle = (i0 - prevIdle0) + (i1 - prevIdle1);
        prevIdle0 = i0;
        prevIdle1 = i1;
        if (dIdle > idleMax) idleMax = dIdle;
        uint8_t cpuPct = 0;
        if (idleMax > 0 && dIdle < idleMax) {
            cpuPct = (uint8_t)(100UL * (idleMax - dIdle) / idleMax);
        }
        cpuGraph[graphIdx] = cpuPct;

        graphIdx = (graphIdx + 1) % 80;

        // ---- Graph drawing helper inlined twice ----
        // Net graph: y=56..105 (h=50), lime/amber autoscaled to ring max.
        {
            const int gx = 0, gy = 56, gw = 80, gh = 50;
            tft.drawFastHLine(gx, gy, gw, 0x2104);
            tft.drawFastHLine(gx, gy + gh - 1, gw, 0x4208);
            uint8_t mx = 1;
            for (int i = 0; i < gw; i++) if (netGraph[i] > mx) mx = netGraph[i];
            for (int i = 0; i < gw; i++) {
                int sIdx = (graphIdx + i) % gw;
                uint8_t v = netGraph[sIdx];
                int barH = (int)(((uint32_t)v * (gh - 2)) / mx);
                if (barH > 0) {
                    uint16_t color = (v > (mx * 3 / 4)) ? 0xFD20 : 0x67E0;
                    tft.drawFastVLine(gx + i, gy + gh - 1 - barH, barH, color);
                }
            }
            tft.setTextColor(0xB596);
            tft.setCursor(2, gy + 2);
            tft.print("NET");
        }

        // CPU graph: y=110..155 (h=46), cyan/amber/red, 0..100% absolute scale.
        {
            const int gx = 0, gy = 110, gw = 80, gh = 46;
            tft.drawFastHLine(gx, gy, gw, 0x2104);
            tft.drawFastHLine(gx, gy + gh - 1, gw, 0x4208);
            for (int i = 0; i < gw; i++) {
                int sIdx = (graphIdx + i) % gw;
                uint8_t v = cpuGraph[sIdx];
                int barH = (int)((uint32_t)v * (gh - 2) / 100);
                if (barH > 0) {
                    uint16_t color = (v >= 75) ? 0xF800 : (v >= 40 ? 0xFD20 : 0x07FF);
                    tft.drawFastVLine(gx + i, gy + gh - 1 - barH, barH, color);
                }
            }
            tft.setTextColor(0xB596);
            tft.setCursor(2, gy + 2);
            tft.printf("CPU %u%%", (unsigned)cpuPct);
        }

        digitalWrite(LCD_BL, LOW);  // keep backlight alive
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
