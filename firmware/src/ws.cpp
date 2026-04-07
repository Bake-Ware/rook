#include "ws.h"
#include "serial_buf.h"
#include <ArduinoJson.h>

AsyncWebSocket wsSerial("/telesthete/stream");
static AsyncWebSocketClient* activeClient = nullptr;

static void onWsEvent(AsyncWebSocket* server, AsyncWebSocketClient* client,
                      AwsEventType type, void* arg, uint8_t* data, size_t len) {
    if (type == WS_EVT_CONNECT) {
        activeClient = client;
    } else if (type == WS_EVT_DISCONNECT) {
        if (activeClient == client) activeClient = nullptr;
    } else if (type == WS_EVT_DATA) {
        AwsFrameInfo* info = (AwsFrameInfo*)arg;
        if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {
            JsonDocument doc;
            if (!deserializeJson(doc, data, len)) {
                const char* msgType = doc["type"];
                if (!msgType) return;
                if (strcmp(msgType, "data") == 0) {
                    const char* payload = doc["payload"];
                    if (payload) CDCSerial.write((const uint8_t*)payload, strlen(payload));
                } else if (strcmp(msgType, "clear") == 0) {
                    taskENTER_CRITICAL(&bufMux);
                    serialBufLen = 0;
                    taskEXIT_CRITICAL(&bufMux);
                }
            }
        }
    }
}

void setupWebSocket(AsyncWebServer& server) {
    wsSerial.onEvent(onWsEvent);
    server.addHandler(&wsSerial);
}

void pushSerialToWs() {
    if (!activeClient || !activeClient->canSend()) return;

    taskENTER_CRITICAL(&bufMux);
    size_t len = serialBufLen;
    if (len == 0) {
        taskEXIT_CRITICAL(&bufMux);
        return;
    }
    // Copy and clear
    char* tmp = (char*)malloc(len + 1);
    if (!tmp) { taskEXIT_CRITICAL(&bufMux); return; }
    memcpy(tmp, serialBuf, len);
    tmp[len] = '\0';
    serialBufLen = 0;
    taskEXIT_CRITICAL(&bufMux);

    JsonDocument doc;
    doc["type"] = "data";
    doc["payload"] = tmp;
    String msg;
    serializeJson(doc, msg);
    activeClient->text(msg);
    free(tmp);
}

void wsSendFileReady(const char* path, size_t size) {
    if (!activeClient || !activeClient->canSend()) return;
    JsonDocument doc;
    doc["type"] = "file_ready";
    doc["path"] = path;
    doc["size"] = size;
    String msg;
    serializeJson(doc, msg);
    activeClient->text(msg);
}
