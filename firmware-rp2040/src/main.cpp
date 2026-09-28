// Rook RP2040-Zero: USB CDC control, keyboard/mouse HID, optional I2C OLED.
#include <Arduino.h>
#include <Adafruit_TinyUSB.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>

static constexpr char VERSION[] = "0.1.0";
static const uint8_t HID_DESC[] = {
    TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(1)),
    TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(2)),
    TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(3)),
};
static Adafruit_USBD_HID hid;
static Adafruit_SSD1306 oled(128, ROOK_OLED_HEIGHT, &Wire, -1);
static bool oledOk = false;
static uint8_t oledAddress = 0;
static uint32_t lastDisplay = 0, requests = 0, lastHid = 0;
static bool hidActive = false;
static char line[1024];
static size_t used = 0;
static bool overflow = false;
static String displayText = "Waiting for host";

static bool report(uint8_t id, const void* data, uint8_t size) {
    uint32_t started = millis();
    while (!hid.ready() && millis() - started < 100) delay(1);
    bool sent = hid.ready() && hid.sendReport(id, data, size);
    if (sent) { lastHid = millis(); hidActive = true; }
    return sent;
}

static void initDisplay() {
    Wire.setSDA(ROOK_OLED_SDA);
    Wire.setSCL(ROOK_OLED_SCL);
    Wire.begin();
    Wire.setClock(100000);
    for (uint8_t address : {0x3c, 0x3d}) {
        Wire.beginTransmission(address);
        if (Wire.endTransmission() == 0) {
            oledAddress = address;
            oledOk = oled.begin(SSD1306_SWITCHCAPVCC, address, false, false);
            break;
        }
    }
    if (oledOk) {
        oled.clearDisplay();
        oled.setTextColor(SSD1306_WHITE);
        oled.setTextSize(1);
        oled.display();
    }
}

static void drawDisplay() {
    if (!oledOk || millis() - lastDisplay < 500) return;
    lastDisplay = millis();
    oled.clearDisplay();
    oled.setCursor(0, 0);
    oled.setTextSize(2);
    oled.println("ROOK");
    oled.setTextSize(1);
    oled.println("RP2040-Zero v0.1.0");
    oled.println(Serial ? "USB host connected" : "USB: waiting");
    oled.println(displayText);
    oled.display();
}

static void command(const char* input) {
    JsonDocument req, reply;
    auto error = deserializeJson(req, input);
    if (error || !req.is<JsonObject>()) {
        reply["ok"] = false;
        reply["error"] = "expected JSON object";
    } else {
        reply["id"] = req["id"];
        const String cmd = req["cmd"] | "";
        reply["ok"] = true;
        requests++;
        if (cmd == "status") {
            reply["device"] = "rook-rp2040-zero";
            reply["version"] = VERSION;
            reply["protocol"] = 1;
            reply["uptime_ms"] = millis();
            reply["requests"] = requests;
            reply["usb_mounted"] = TinyUSBDevice.mounted();
            reply["oled_connected"] = oledOk;
            reply["oled_address"] = oledAddress;
            reply["sda"] = ROOK_OLED_SDA;
            reply["scl"] = ROOK_OLED_SCL;
            reply["transport"] = "usb-host";
        } else if (cmd == "display") {
            if (!req["text"].is<const char*>() || strlen(req["text"]) > 80) {
                reply["ok"] = false;
                reply["error"] = "text must be at most 80 bytes";
            } else {
                displayText = req["text"].as<String>();
                reply["oled_connected"] = oledOk;
            }
        } else if (cmd == "display_probe") {
            // Probe after wiring/reconnecting the OLED without flashing again.
            initDisplay();
            reply["oled_connected"] = oledOk;
            reply["oled_address"] = oledAddress;
        } else if (cmd == "keyboard") {
            // A complete boot-keyboard report. The host MUST follow with a
            // release report; firmware also releases on serial disconnect.
            JsonArray keys = req["keys"].as<JsonArray>();
            bool valid = !keys.isNull() && keys.size() <= 6 && req["mods"].is<unsigned>()
                         && req["mods"].as<unsigned>() <= 255;
            uint8_t body[8] = {req["mods"].as<uint8_t>(), 0};
            size_t index = 2;
            for (JsonVariant key : keys) {
                if (!key.is<unsigned>() || key.as<unsigned>() > 255 || index >= 8) {
                    valid = false; break;
                }
                body[index++] = key.as<uint8_t>();
            }
            if (!valid) { reply["ok"] = false; reply["error"] = "invalid keyboard report"; }
            else reply["ok"] = report(1, body, sizeof(body));
        } else if (cmd == "mouse") {
            int8_t body[5] = {};
            const char* fields[] = {"buttons", "x", "y", "wheel", "pan"};
            bool valid = true;
            for (int i = 0; i < 5; i++) {
                if (!req[fields[i]].is<int>()) { valid = false; break; }
                int value = req[fields[i]].as<int>();
                if (value < (i == 0 ? 0 : -127) || value > (i == 0 ? 31 : 127)) {
                    valid = false; break;
                }
                body[i] = value;
            }
            if (!valid) { reply["ok"] = false; reply["error"] = "invalid mouse report"; }
            else reply["ok"] = report(2, body, sizeof(body));
        } else if (cmd == "consumer") {
            if (!req["usage"].is<unsigned>() || req["usage"].as<unsigned>() > 65535) {
                reply["ok"] = false; reply["error"] = "invalid consumer usage";
            } else {
                uint16_t usage = req["usage"];
                reply["ok"] = report(3, &usage, sizeof(usage));
            }
        } else if (cmd == "release") {
            uint8_t keys[8] = {}, mouse[5] = {};
            uint16_t consumer = 0;
            bool a = report(1, keys, sizeof(keys));
            bool b = report(2, mouse, sizeof(mouse));
            bool c = report(3, &consumer, sizeof(consumer));
            reply["ok"] = a && b && c;
        } else {
            reply["ok"] = false;
            reply["error"] = "unknown command";
        }
    }
    serializeJson(reply, Serial);
    Serial.println();
}

void setup() {
    TinyUSBDevice.setManufacturerDescriptor("Rook");
    TinyUSBDevice.setProductDescriptor("Rook RP2040 Zero");
    hid.setPollInterval(1);
    hid.setReportDescriptor(HID_DESC, sizeof(HID_DESC));
    hid.begin();
    Serial.begin(115200);
    initDisplay();
}

void loop() {
    static bool wasConnected = false;
    bool connected = bool(Serial);
    if (wasConnected && !connected) {
        uint8_t zero[8] = {};
        report(1, zero, 8); report(2, zero, 5); report(3, zero, 2);
        used = 0; overflow = false;
    }
    wasConnected = connected;
    if (hidActive && millis() - lastHid > 1000) {
        uint8_t zero[8] = {};
        bool a = report(1, zero, 8), b = report(2, zero, 5), c = report(3, zero, 2);
        hidActive = !(a && b && c);
    }
    // Bound work per loop so malformed traffic cannot starve USB/display.
    for (int budget = 0; budget < 128 && Serial.available(); budget++) {
        int c = Serial.read();
        if (c == '\n') {
            if (overflow) Serial.println("{\"ok\":false,\"error\":\"line too long\"}");
            else if (used) { line[used] = 0; command(line); }
            used = 0; overflow = false;
        } else if (c == 0) {
            overflow = true;
        } else if (c != '\r') {
            if (used < sizeof(line) - 1 && !overflow) line[used++] = c;
            else overflow = true;
        }
    }
    drawDisplay();
    delay(1);
}
