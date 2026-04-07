#pragma once
#include <ESPAsyncWebServer.h>

extern AsyncWebSocket wsSerial;

void setupWebSocket(AsyncWebServer& server);
void pushSerialToWs();
void wsSendFileReady(const char* path, size_t size);
