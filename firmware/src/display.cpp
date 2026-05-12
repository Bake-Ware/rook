#include "display.h"
#include "config.h"
#include "serial_buf.h"
#include "storage.h"
#include "http_routes.h"
#include "ws.h"
#include "device_mode.h"
#include <WiFi.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>

static SPIClass lcdSPI(FSPI);
static Adafruit_ST7735 tft(&lcdSPI, LCD_CS, LCD_DC, LCD_RST);

void displayTask(void* param) {
    lcdSPI.begin(LCD_SCLK, -1, LCD_MOSI);
    tft.initR(INITR_MINI160x80_PLUGIN);
    tft.setRotation(1);
    tft.invertDisplay(false);
    tft.fillScreen(ST77XX_BLACK);

    // Backlight on (active low)
    pinMode(LCD_BL, OUTPUT);
    digitalWrite(LCD_BL, LOW);

    for (;;) {
        wifi_mode_t wm = WiFi.getMode();
        bool staConnected = (WiFi.status() == WL_CONNECTED);
        bool wifiOk = (wm == WIFI_AP) || (wm == WIFI_AP_STA) || staConnected;
        String ip = staConnected
            ? WiFi.localIP().toString()
            : WiFi.softAPIP().toString();
        const char* mode = staConnected ? "STA" : "AP";

        tft.fillScreen(ST77XX_BLACK);

        // Header
        tft.setTextSize(2);
        tft.setTextColor(0x67E0);  // lime
        tft.setCursor(4, 2);
        tft.print("ROOK");
        tft.setTextSize(1);
        tft.setTextColor(0xB596);
        tft.setCursor(76, 6);
        tft.printf("KVM v%s", ROOK_FW_VERSION);

        // WiFi badge (compact, right of header)
        tft.setTextSize(1);
        tft.setCursor(126, 6);
        tft.setTextColor(wifiOk ? 0x67E0 : ST77XX_RED);
        tft.print(mode);

        // IP — HUGE (size 3 = 18x24 px). Split into 2 lines at the last dot
        // so the final octet stands out.  e.g. "192.168.1." / "138"
        tft.setTextSize(3);
        tft.setTextColor(ST77XX_WHITE);
        int lastDot = ip.lastIndexOf('.');
        String top = (lastDot > 0) ? ip.substring(0, lastDot + 1) : ip;
        String bot = (lastDot > 0) ? ip.substring(lastDot + 1) : String("");
        int topX = (160 - (int)top.length() * 18) / 2;
        int botX = (160 - (int)bot.length() * 18) / 2;
        if (topX < 0) topX = 0;
        if (botX < 0) botX = 0;
        tft.setCursor(topX, 20);
        tft.print(top);
        // Last octet in lime for emphasis
        tft.setTextColor(0x67E0);
        tft.setCursor(botX, 46);
        tft.print(bot);

        tft.setTextSize(1);

        // Compact status row at the bottom (y=72): TF + WS/HTTP + HID badge
        unsigned long sec = millis() / 1000;
        tft.setCursor(4, 72);
        tft.setTextColor(0xB596);
        if (fileTransferActive) {
            tft.setTextColor(0x67E0);
            tft.printf("Xfer:%uB", (unsigned)fileTransferBytes);
        } else {
            tft.printf("%lum%02lus W:%d H:%u",
                       sec / 60, sec % 60,
                       wsSerial.count(), getHttpRequestCount());
        }
        // HID kill-switch badge (right edge)
        if (!hidEnabled()) {
            tft.setCursor(140, 72);
            tft.setTextColor(ST77XX_RED);
            tft.print("X");
        }

        digitalWrite(LCD_BL, LOW);  // keep backlight alive
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
