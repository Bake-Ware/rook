#include <Arduino.h>
#include "USB.h"
#include "config.h"
#include "hid.h"
#include "serial_buf.h"
#include "wifi_setup.h"
#include "storage.h"
#include "http_routes.h"
#include "ws.h"
#include "display.h"
#include "device_mode.h"
#include "button.h"
#include "msc.h"

AsyncWebServer server(HTTP_PORT);

void setup() {
    Serial.begin(115200);
    Serial.printf("\n=== Rook KVM Bridge v%s ===\n", ROOK_FW_VERSION);

    // Read persistent storage mode FIRST — it determines USB composite shape.
    StorageMode mode = loadStorageMode();
    Serial.printf("Storage mode (boot): %s\n", storageModeName(mode));

    // USB composite — base classes always present, MSC conditionally added.
    USB.VID(0x1209);
    USB.PID(0x0001);
    USB.productName("Rook KVM Bridge");
    USB.manufacturerName("Rook");
    CDCSerial.begin(115200);
    Keyboard.begin();

    if (mode == STORAGE_MODE_MSC) {
        bool ok = initMSC();
        Serial.printf("MSC init: %s\n", ok ? "ok" : "failed");
        // If MSC fails (no card / SDMMC error), fall through with HID+CDC only.
    }

    USB.begin();

    // WiFi (permanent APSTA)
    setupWifi();

    // Filesystem (only in internal mode — MSC mode keeps the card at block level)
    if (mode == STORAGE_MODE_INTERNAL) {
        if (initStorage()) {
            Serial.printf("TF card: %lluMB\n", getStorageTotalMB());
        } else {
            Serial.println("TF card: not found");
        }
    } else {
        Serial.println("TF card: owned by host (MSC)");
    }

    // Button (GPIO 0) — short press toggles storage mode (reboots)
    setupButton();

    // HTTP + WebSocket
    setupHttpRoutes(server);
    setupWebSocket(server);
    server.begin();

    // Backlight on (active low)
    pinMode(LCD_BL, OUTPUT);
    digitalWrite(LCD_BL, LOW);

    // LCD task on core 0
    xTaskCreatePinnedToCore(displayTask, "display", 8192, NULL, 1, NULL, 0);

    Serial.println("Ready.");
}

void loop() {
    pollCDC();
    pushSerialToWs();
    wsSerial.cleanupClients();
    pollButton();
}
