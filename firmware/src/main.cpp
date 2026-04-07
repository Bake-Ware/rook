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

AsyncWebServer server(HTTP_PORT);

void setup() {
    Serial.begin(115200);
    Serial.println("\n=== Rook KVM Bridge v0.2.0 ===");

    // USB composite: HID keyboard + CDC serial
    USB.VID(0x1209);
    USB.PID(0x0001);
    USB.productName("Rook KVM Bridge");
    USB.manufacturerName("Rook");
    CDCSerial.begin(115200);
    Keyboard.begin();
    USB.begin();

    // WiFi
    setupWifi();

    // TF Card
    if (initStorage()) {
        Serial.printf("TF card: %lluMB\n", getStorageTotalMB());
    } else {
        Serial.println("TF card: not found");
    }

    // HTTP + WebSocket
    setupHttpRoutes(server);
    setupWebSocket(server);
    server.begin();

    // Backlight on early
    pinMode(LCD_BL, OUTPUT);
    digitalWrite(LCD_BL, LOW);

    // LCD on core 0
    xTaskCreatePinnedToCore(displayTask, "display", 8192, NULL, 1, NULL, 0);

    Serial.println("Ready.");
}

void loop() {
    pollCDC();
    pushSerialToWs();
    wsSerial.cleanupClients();
}
