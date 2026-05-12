#include "http_routes.h"
#include "hid.h"
#include "serial_buf.h"
#include "storage.h"
#include "config.h"
#include "device_mode.h"
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
    doc["version"] = ROOK_FW_VERSION;
    doc["uptime_ms"] = millis();

    wifi_mode_t wm = WiFi.getMode();
    const char* wmName = "OFF";
    if (wm == WIFI_AP_STA) wmName = "APSTA";
    else if (wm == WIFI_AP) wmName = "AP";
    else if (wm == WIFI_STA) wmName = "STA";
    doc["wifi_mode"] = wmName;
    doc["ip"] = WiFi.localIP().toString();
    doc["ap_ip"] = WiFi.softAPIP().toString();
    doc["serial_buffered"] = serialBufLen;

    doc["storage_mode"] = storageModeName(currentStorageMode());
    doc["hid_enabled"] = hidEnabled();

    if (currentStorageMode() == STORAGE_MODE_INTERNAL) {
        doc["storage"] = isStorageReady() ? "ok" : "none";
        if (isStorageReady()) {
            doc["storage_total_mb"] = getStorageTotalMB();
            doc["storage_used_mb"] = getStorageUsedMB();
        }
    } else {
        doc["storage"] = "host_owned";
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

// ---- Mode ----
static void handleModeGet(AsyncWebServerRequest* req) {
    hit();
    JsonDocument doc;
    doc["mode"] = storageModeName(currentStorageMode());
    sendJson(req, 200, doc);
}

static void handleHidGet(AsyncWebServerRequest* req) {
    hit();
    JsonDocument doc;
    doc["enabled"] = hidEnabled();
    sendJson(req, 200, doc);
}

// ---- File/Drop Handlers ----
static void handleDropList(AsyncWebServerRequest* req) {
    hit();
    if (currentStorageMode() == STORAGE_MODE_MSC) return sendError(req, 503, "card_in_msc");
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
    if (currentStorageMode() == STORAGE_MODE_MSC) return sendError(req, 503, "card_in_msc");
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
    req->send(SD_MMC, path, "application/octet-stream");
}

static void handleDropDelete(AsyncWebServerRequest* req) {
    hit();
    if (currentStorageMode() == STORAGE_MODE_MSC) return sendError(req, 503, "card_in_msc");
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

// ---- JSON Body Handlers ----
static void setupJsonRoutes(AsyncWebServer& server) {
    // POST /type — HID type, gated by kill-switch
    auto typeHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        if (!hidEnabled()) return sendError(req, 503, "hid_disabled");
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

    // POST /key — HID key combo, gated by kill-switch
    auto keyHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        if (!hidEnabled()) return sendError(req, 503, "hid_disabled");
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

    // POST /serial — write to CDC
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

    // POST /mode — switch storage mode (reboots).
    // Body: {"mode": "internal" | "msc"}
    auto modeHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        const char* m = json["mode"];
        if (!m) return sendError(req, 400, "missing 'mode'");
        StorageMode target;
        if (strcmp(m, "internal") == 0)      target = STORAGE_MODE_INTERNAL;
        else if (strcmp(m, "msc") == 0)      target = STORAGE_MODE_MSC;
        else return sendError(req, 400, "bad mode");

        JsonDocument resp;
        resp["mode"] = storageModeName(target);
        resp["rebooting"] = true;
        sendJson(req, 200, resp);
        // schedule reboot after this response has had a chance to flush
        delay(50);
        rebootIntoMode(target);  // does not return
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/mode", modeHandler));

    // POST /hid — toggle HID kill-switch.
    // Body: {"enabled": true|false}
    auto hidSetHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        if (!json["enabled"].is<bool>()) return sendError(req, 400, "missing 'enabled' bool");
        bool on = json["enabled"].as<bool>();
        setHidEnabled(on);
        JsonDocument resp;
        resp["enabled"] = on;
        sendJson(req, 200, resp);
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/hid", hidSetHandler));
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

    // Mode / HID kill-switch (GET)
    server.on("/mode", HTTP_GET, handleModeGet);
    server.on("/hid", HTTP_GET, handleHidGet);

    // POST /flash_mode — software-trigger ROM bootloader entry.
    // Caller flashes via esptool without holding GPIO 0.
    server.on("/flash_mode", HTTP_POST, [](AsyncWebServerRequest* req) {
        hit();
        JsonDocument resp;
        resp["ok"] = true;
        resp["message"] = "rebooting into ROM bootloader (VID:PID 303A:1001)";
        sendJson(req, 200, resp);
        delay(150);  // let HTTP response flush
        rebootIntoBootloader();  // does not return
    });

    // JSON body routes (type, key, serial-write, mode POST, hid POST)
    setupJsonRoutes(server);

    // Catch-all handles drop routes + 404
    server.onNotFound([](AsyncWebServerRequest* req) {
        String uri = req->url();
        if ((uri == "/telesthete/drop" || uri == "/files") && req->method() == HTTP_GET) {
            handleDropList(req);
        } else if ((uri.startsWith("/telesthete/drop/") || uri.startsWith("/files/")) && req->method() == HTTP_GET) {
            handleDropDownload(req);
        } else if ((uri.startsWith("/telesthete/drop/") || uri.startsWith("/files/")) && req->method() == HTTP_DELETE) {
            handleDropDelete(req);
        } else {
            req->send(404, "application/json", "{\"error\":\"not found\"}");
        }
    });
}
