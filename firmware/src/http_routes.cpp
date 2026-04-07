#include "http_routes.h"
#include "hid.h"
#include "serial_buf.h"
#include "storage.h"
#include "config.h"
#include <ArduinoJson.h>
#include <AsyncJson.h>
#include <WiFi.h>
#include "SD_MMC.h"

static uint32_t httpRequestCount = 0;
static unsigned long lastHttpHit = 0;
uint32_t getHttpRequestCount() { return httpRequestCount; }
unsigned long getLastHttpHit() { return lastHttpHit; }

static void hit() { httpRequestCount++; lastHttpHit = millis(); }

// ---- Helpers ----
static void sendJson(AsyncWebServerRequest* req, int code, JsonDocument& doc) {
    String resp;
    serializeJson(doc, resp);
    req->send(code, "application/json", resp);
}

static void sendError(AsyncWebServerRequest* req, int code, const char* msg) {
    JsonDocument doc;
    doc["error"] = msg;
    sendJson(req, code, doc);
}

// ---- Handlers ----
static void handleStatus(AsyncWebServerRequest* req) {
    hit();
    JsonDocument doc;
    doc["device"] = TELESTHETE_NAME;
    doc["version"] = "0.2.0";
    doc["uptime_ms"] = millis();
    doc["wifi_mode"] = (WiFi.getMode() == WIFI_AP) ? "AP" : "STA";
    doc["ip"] = (WiFi.getMode() == WIFI_AP)
        ? WiFi.softAPIP().toString()
        : WiFi.localIP().toString();
    doc["serial_buffered"] = serialBufLen;
    doc["storage"] = isStorageReady() ? "ok" : "none";
    if (isStorageReady()) {
        doc["storage_total_mb"] = getStorageTotalMB();
        doc["storage_used_mb"] = getStorageUsedMB();
    }
    sendJson(req, 200, doc);
}

static void handleSerialRead(AsyncWebServerRequest* req) {
    hit();
    taskENTER_CRITICAL(&bufMux);
    size_t len = serialBufLen;
    char* copy = nullptr;
    if (len > 0) {
        copy = (char*)malloc(len + 1);
        if (copy) { memcpy(copy, serialBuf, len); copy[len] = '\0'; }
    }
    taskEXIT_CRITICAL(&bufMux);

    JsonDocument doc;
    doc["data"] = copy ? copy : "";
    doc["length"] = len;
    if (copy) free(copy);
    sendJson(req, 200, doc);
}

static void handleSerialClear(AsyncWebServerRequest* req) {
    hit();
    taskENTER_CRITICAL(&bufMux);
    serialBufLen = 0;
    taskEXIT_CRITICAL(&bufMux);
    JsonDocument doc;
    doc["ok"] = true;
    sendJson(req, 200, doc);
}

// ---- File/Drop Handlers ----
static void handleDropList(AsyncWebServerRequest* req) {
    hit();
    if (!isStorageReady()) return sendError(req, 503, "no storage");

    File root = SD_MMC.open("/drop");
    if (!root || !root.isDirectory()) return sendError(req, 500, "drop dir missing");

    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    File f = root.openNextFile();
    while (f) {
        JsonObject obj = arr.add<JsonObject>();
        obj["name"] = String(f.name());
        obj["size"] = f.size();
        obj["isDir"] = f.isDirectory();
        f = root.openNextFile();
    }
    sendJson(req, 200, doc);
}

static void handleDropDownload(AsyncWebServerRequest* req) {
    hit();
    if (!isStorageReady()) return sendError(req, 503, "no storage");

    // Extract path after /telesthete/drop/ or /files/
    String uri = req->url();
    String path;
    if (uri.startsWith("/telesthete/drop/"))
        path = "/drop/" + uri.substring(17);
    else if (uri.startsWith("/files/"))
        path = "/drop/" + uri.substring(7);
    else
        return sendError(req, 400, "bad path");

    if (!SD_MMC.exists(path)) return sendError(req, 404, "not found");
    req->send(SD_MMC, path, "application/octet-stream");
}

static void handleDropDelete(AsyncWebServerRequest* req) {
    hit();
    if (!isStorageReady()) return sendError(req, 503, "no storage");

    String uri = req->url();
    String path;
    if (uri.startsWith("/telesthete/drop/"))
        path = "/drop/" + uri.substring(17);
    else if (uri.startsWith("/files/"))
        path = "/drop/" + uri.substring(7);
    else
        return sendError(req, 400, "bad path");

    if (!SD_MMC.exists(path)) return sendError(req, 404, "not found");
    SD_MMC.remove(path);
    JsonDocument doc;
    doc["ok"] = true;
    sendJson(req, 200, doc);
}

// ---- JSON Body Handlers (use AsyncCallbackJsonWebHandler) ----
static void setupJsonRoutes(AsyncWebServer& server) {
    // POST /type + /telesthete/channel/type
    auto typeHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        const char* text = json["text"];
        if (!text) return sendError(req, 400, "missing 'text'");
        int delayMs = json["delay_ms"] | DEFAULT_KEY_DELAY_MS;
        size_t len = strlen(text);
        for (size_t i = 0; i < len; i++) {
            Keyboard.write((uint8_t)text[i]);
            if (delayMs > 0) delay(delayMs);
        }
        JsonDocument resp;
        resp["typed"] = len;
        sendJson(req, 200, resp);
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/type", typeHandler));
    server.addHandler(new AsyncCallbackJsonWebHandler("/telesthete/channel/type", typeHandler));

    // POST /key + /telesthete/channel/key
    auto keyHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        JsonArray mods = json["modifiers"].as<JsonArray>();
        if (mods) {
            for (JsonVariant m : mods) {
                uint8_t k = lookupModifierKey(m.as<const char*>());
                if (k) Keyboard.press(k);
            }
        }
        const char* key = json["key"];
        if (key) {
            if (strlen(key) == 1)
                Keyboard.press((uint8_t)key[0]);
            else {
                uint8_t special = lookupSpecialKey(key);
                if (special) Keyboard.press(special);
            }
        }
        delay(50);
        Keyboard.releaseAll();
        JsonDocument resp;
        resp["ok"] = true;
        sendJson(req, 200, resp);
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/key", keyHandler));
    server.addHandler(new AsyncCallbackJsonWebHandler("/telesthete/channel/key", keyHandler));

    // POST /serial + /telesthete/stream/write
    auto serialWriteHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        const char* data = json["data"];
        if (!data) return sendError(req, 400, "missing 'data'");
        size_t written = CDCSerial.write((const uint8_t*)data, strlen(data));
        JsonDocument resp;
        resp["written"] = written;
        sendJson(req, 200, resp);
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/serial", serialWriteHandler));
    server.addHandler(new AsyncCallbackJsonWebHandler("/telesthete/stream/write", serialWriteHandler));
}

// ---- Route Registration ----
void setupHttpRoutes(AsyncWebServer& server) {
    // Status
    server.on("/status", HTTP_GET, handleStatus);
    server.on("/telesthete/board", HTTP_GET, handleStatus);

    // Serial read
    server.on("/serial", HTTP_GET, handleSerialRead);
    server.on("/telesthete/stream/read", HTTP_GET, handleSerialRead);

    // Serial clear
    server.on("/serial/clear", HTTP_POST, handleSerialClear);
    server.on("/telesthete/stream/clear", HTTP_POST, handleSerialClear);

    // JSON body routes
    setupJsonRoutes(server);

    // Catch-all handles drop routes + 404
    server.onNotFound([](AsyncWebServerRequest* req) {
        String uri = req->url();
        // Drop list
        if ((uri == "/telesthete/drop" || uri == "/files") && req->method() == HTTP_GET) {
            handleDropList(req);
        // Drop download
        } else if ((uri.startsWith("/telesthete/drop/") || uri.startsWith("/files/")) && req->method() == HTTP_GET) {
            handleDropDownload(req);
        // Drop delete
        } else if ((uri.startsWith("/telesthete/drop/") || uri.startsWith("/files/")) && req->method() == HTTP_DELETE) {
            handleDropDelete(req);
        } else {
            req->send(404, "application/json", "{\"error\":\"not found\"}");
        }
    });
}
