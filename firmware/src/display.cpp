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

        // WiFi
        tft.setCursor(4, 22);
        tft.setTextColor(wifiOk ? 0x67E0 : ST77XX_RED);
        tft.printf("WiFi: %s %s", mode, wifiOk ? "OK" : "DOWN");

        // IP
        tft.setCursor(4, 32);
        tft.setTextColor(ST77XX_WHITE);
        tft.print(ip);

        // TF card / storage mode
        tft.setCursor(4, 44);
        if (currentStorageMode() == STORAGE_MODE_MSC) {
            tft.setTextColor(0xFD20);  // orange — drive mounted on host
            tft.print("TF: USB drive");
        } else if (isStorageReady()) {
            tft.setTextColor(0x67E0);
            tft.printf("TF: %lluMB", getStorageTotalMB());
        } else {
            tft.setTextColor(0xB596);
            tft.print("TF: none");
        }

        // WS + HTTP + transfer + HID indicator
        tft.setCursor(4, 56);
        if (fileTransferActive) {
            tft.setTextColor(0x67E0);
            tft.printf("Xfer: %uB", (unsigned)fileTransferBytes);
        } else {
            tft.setTextColor(0xB596);
            tft.printf("WS:%d HTTP:%u", wsSerial.count(), getHttpRequestCount());
        }
        // HID kill-switch badge (right-aligned on row 56)
        if (!hidEnabled()) {
            tft.setCursor(118, 56);
            tft.setTextColor(ST77XX_RED);
            tft.print("HID!");
        }

        // Uptime
        unsigned long sec = millis() / 1000;
        tft.setCursor(4, 68);
        tft.setTextColor(0xB596);
        tft.printf("Up: %lum %lus", sec / 60, sec % 60);

        digitalWrite(LCD_BL, LOW);  // keep backlight alive
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
