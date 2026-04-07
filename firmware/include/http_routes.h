#pragma once
#include <ESPAsyncWebServer.h>

void setupHttpRoutes(AsyncWebServer& server);
uint32_t getHttpRequestCount();
unsigned long getLastHttpHit();
