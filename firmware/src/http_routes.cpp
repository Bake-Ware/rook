#include "http_routes.h"
#include "hid.h"
#include "serial_buf.h"
#include "storage.h"
#include "config.h"
#include "device_mode.h"
#include "settings.h"
#include <ArduinoJson.h>
#include <AsyncJson.h>
#include <WiFi.h>
#include <Update.h>
#include "SD_MMC.h"

// HTTP Basic auth gate for admin endpoints.
static bool requireAuth(AsyncWebServerRequest* req) {
    const auto& s = getSettings();
    if (req->authenticate(s.admin_user.c_str(), s.admin_pass.c_str()))
        return true;
    req->requestAuthentication("rook-config");
    return false;
}

static String escapeAttr(const String& v) {
    String out;
    out.reserve(v.length() + 8);
    for (size_t i = 0; i < v.length(); i++) {
        char c = v[i];
        if (c == '"') out += "&quot;";
        else if (c == '<') out += "&lt;";
        else if (c == '>') out += "&gt;";
        else if (c == '&') out += "&amp;";
        else out += c;
    }
    return out;
}

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

    // POST /consumer — HID Consumer Control (volume, mute, media keys), gated by kill-switch
    // Body: {"key": "volume_up"} or {"code": 233}
    auto consumerHandler = [](AsyncWebServerRequest* req, JsonVariant& json) {
        hit();
        if (!hidEnabled()) return sendError(req, 503, "hid_disabled");
        uint16_t code = 0;
        const char* name = json["key"];
        if (name) code = lookupConsumerKey(name);
        if (!code && json["code"].is<uint16_t>()) code = json["code"].as<uint16_t>();
        if (!code) return sendError(req, 400, "unknown consumer key");
        Consumer.press(code);
        delay(50);
        Consumer.release();
        JsonDocument resp;
        resp["ok"] = true;
        resp["code"] = code;
        sendJson(req, 200, resp);
    };
    server.addHandler(new AsyncCallbackJsonWebHandler("/consumer", consumerHandler));
    server.addHandler(new AsyncCallbackJsonWebHandler("/telesthete/channel/consumer", consumerHandler));

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
        if (!requireAuth(req)) return;
        hit();
        JsonDocument resp;
        resp["ok"] = true;
        resp["message"] = "rebooting into ROM bootloader (VID:PID 303A:1001)";
        sendJson(req, 200, resp);
        delay(150);  // let HTTP response flush
        rebootIntoBootloader();  // does not return
    });

    // GET /config — HTML config page (HTTP Basic auth: admin user/pass)
    server.on("/config", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (!requireAuth(req)) return;
        hit();
        const auto& s = getSettings();
        String body =
            "<!doctype html><html><head><title>Rook Config</title>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>body{font-family:system-ui,sans-serif;max-width:520px;"
            "margin:1.5em auto;padding:0 1em;background:#111;color:#eee}"
            "h1{font-size:1.3em;margin:0 0 .5em}"
            "fieldset{border:1px solid #444;border-radius:6px;margin:1em 0;padding:.8em 1em}"
            "legend{padding:0 .4em;color:#0c9}"
            "label{display:block;margin:.6em 0 .2em;font-size:.9em}"
            "input{width:100%;padding:.45em;box-sizing:border-box;background:#222;"
            "color:#eee;border:1px solid #555;border-radius:4px;font-size:1em}"
            "button{background:#0a7;color:#fff;padding:.6em 1.2em;border:0;"
            "border-radius:4px;cursor:pointer;font-size:1em;margin-top:.6em}"
            "button.warn{background:#a33}"
            ".row{display:flex;gap:.6em}.row>*{flex:1}"
            "small{color:#888}</style></head><body>"
            "<h1>Rook Config</h1>"
            "<small>Device: rook-kvm v" ROOK_FW_VERSION " — saving reboots the device.</small>"
            "<form method=POST action=/config>"
            "<fieldset><legend>Access Point</legend>"
            "<label>SSID<input name=ap_ssid value=\"" + escapeAttr(s.ap_ssid) + "\"></label>"
            "<label>Password (>=8 chars for WPA2)<input name=ap_pass value=\"" + escapeAttr(s.ap_pass) + "\"></label>"
            "</fieldset>"
            "<fieldset><legend>Station (home WiFi)</legend>"
            "<label>SSID<input name=sta_ssid value=\"" + escapeAttr(s.sta_ssid) + "\"></label>"
            "<label>Password<input name=sta_pass value=\"" + escapeAttr(s.sta_pass) + "\"></label>"
            "</fieldset>"
            "<fieldset><legend>Admin (Basic Auth on /config and /factory_reset)</legend>"
            "<label>Username<input name=admin_user value=\"" + escapeAttr(s.admin_user) + "\"></label>"
            "<label>Password<input name=admin_pass value=\"" + escapeAttr(s.admin_pass) + "\"></label>"
            "</fieldset>"
            "<div class=row><button type=submit>Save &amp; Reboot</button>"
            "<button class=warn formaction=/factory_reset formmethod=POST>Factory Reset</button>"
            "</div></form></body></html>";
        req->send(200, "text/html", body);
    });

    // POST /config — form-encoded save handler
    server.on("/config", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!requireAuth(req)) return;
        hit();
        DeviceSettings s = getSettings();
        auto pull = [&](const char* k, String& dst) {
            if (req->hasParam(k, true))
                dst = req->getParam(k, true)->value();
        };
        pull("ap_ssid",    s.ap_ssid);
        pull("ap_pass",    s.ap_pass);
        pull("sta_ssid",   s.sta_ssid);
        pull("sta_pass",   s.sta_pass);
        pull("admin_user", s.admin_user);
        pull("admin_pass", s.admin_pass);
        updateSettings(s);
        req->send(200, "text/html",
            "<html><body style='font-family:system-ui;background:#111;color:#eee;"
            "padding:2em;text-align:center'><h2>Saved. Rebooting...</h2>"
            "<p>If the AP SSID/pass or admin creds changed, reconnect with the new values.</p>"
            "</body></html>");
        delay(300);
        ESP.restart();
    });

    // POST /factory_reset — wipe NVS, reboot with compile-time defaults.
    server.on("/factory_reset", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!requireAuth(req)) return;
        hit();
        factoryResetSettings();
        req->send(200, "text/html",
            "<html><body style='font-family:system-ui;background:#111;color:#eee;"
            "padding:2em;text-align:center'><h2>Factory reset. Rebooting...</h2>"
            "</body></html>");
        delay(300);
        ESP.restart();
    });

    // POST /ota — wireless firmware update. Accepts raw firmware.bin in body
    // OR as multipart form upload field "firmware". Streams to Update lib.
    // After success, device reboots into new image automatically.
    server.on(
        "/ota",
        HTTP_POST,
        [](AsyncWebServerRequest* req) {
            if (!requireAuth(req)) return;
            hit();
            bool ok = !Update.hasError();
            JsonDocument resp;
            resp["ok"] = ok;
            if (ok) {
                resp["message"] = "rebooting into new firmware";
                resp["written"] = (uint32_t)Update.size();
            } else {
                resp["error"] = Update.errorString();
            }
            AsyncWebServerResponse* r = req->beginResponse(ok ? 200 : 500,
                "application/json", resp.as<String>());
            r->addHeader("Connection", "close");
            req->send(r);
            if (ok) {
                // Reboot shortly after response flushes
                delay(200);
                ESP.restart();
            }
        },
        // Multipart upload handler
        [](AsyncWebServerRequest* req, String filename, size_t index,
           uint8_t* data, size_t len, bool final) {
            if (index == 0) {
                Serial.printf("OTA start: %s\n", filename.c_str());
                if (!Update.begin(UPDATE_SIZE_UNKNOWN, U_FLASH)) {
                    Update.printError(Serial);
                }
            }
            if (len && !Update.hasError()) {
                if (Update.write(data, len) != len) Update.printError(Serial);
            }
            if (final) {
                if (Update.end(true)) {
                    Serial.printf("OTA done: %u bytes\n", (unsigned)(index + len));
                } else {
                    Update.printError(Serial);
                }
            }
        },
        // Raw body handler (for clients posting firmware.bin as request body)
        [](AsyncWebServerRequest* req, uint8_t* data, size_t len, size_t index, size_t total) {
            if (index == 0) {
                Serial.printf("OTA raw body start: total=%u\n", (unsigned)total);
                size_t expect = total ? total : UPDATE_SIZE_UNKNOWN;
                if (!Update.begin(expect, U_FLASH)) {
                    Update.printError(Serial);
                }
            }
            if (len && !Update.hasError()) {
                if (Update.write(data, len) != len) Update.printError(Serial);
            }
            if (total && index + len >= total) {
                if (Update.end(true)) {
                    Serial.printf("OTA raw done: %u bytes\n", (unsigned)(index + len));
                } else {
                    Update.printError(Serial);
                }
            }
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
