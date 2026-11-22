// Host stand-ins for what lib/rookcore2's worker and band use of the Arduino
// core and FreeRTOS, so test/worker_test.cpp runs them on a PC. Single
// threaded: mutexes do nothing, no task is ever made.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <chrono>

// The test moves time on with host_skip(ms) (a resend waits 15 s).
inline uint32_t& host_skipped() {
    static uint32_t s = 0;
    return s;
}
inline void host_skip(uint32_t ms) { host_skipped() += ms; }
inline uint32_t millis() {
    using namespace std::chrono;
    static auto t0 = steady_clock::now();
    return (uint32_t)duration_cast<milliseconds>(steady_clock::now() - t0).count() + host_skipped();
}
inline void delay(uint32_t) {}

struct HostEsp {
    uint32_t free_heap = 40000, max_block = 20000;    // what the test says the chip has
    uint32_t getFreeHeap() { return free_heap; }
    uint32_t getMaxAllocHeap() { return max_block; }
    const char* getChipModel() { return "host"; }
    int getChipCores() { return 1; }
};
extern HostEsp ESP;

typedef void* SemaphoreHandle_t;
typedef void* TaskHandle_t;
typedef int BaseType_t;
#define portMAX_DELAY 0xFFFFFFFFu
#define pdPASS 1
#define pdTRUE 1
#define pdFAIL 0
enum eNotifyAction { eSetValueWithOverwrite };
// Mutexes that count how many are held at once (single threaded, so a take
// always succeeds): the network stand-in checks that no lock but the band's
// own is held while a packet goes out (host_held()).
struct HostMutex { int held = 0; };
inline int& host_held() {
    static int n = 0;
    return n;
}
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return new HostMutex(); }
inline SemaphoreHandle_t xSemaphoreCreateRecursiveMutex() { return new HostMutex(); }
inline int xSemaphoreTake(SemaphoreHandle_t m, uint32_t) {
    if (m && ((HostMutex*)m)->held++ == 0) host_held()++;
    return 1;
}
inline int xSemaphoreGive(SemaphoreHandle_t m) {
    if (m && --((HostMutex*)m)->held == 0) host_held()--;
    return 1;
}
inline int xSemaphoreTakeRecursive(SemaphoreHandle_t m, uint32_t t) { return xSemaphoreTake(m, t); }
inline int xSemaphoreGiveRecursive(SemaphoreHandle_t m) { return xSemaphoreGive(m); }
inline BaseType_t xTaskCreate(void (*)(void*), const char*, uint32_t, void*, int, TaskHandle_t*) { return pdFAIL; }
inline BaseType_t xTaskNotify(TaskHandle_t, uint32_t, eNotifyAction) { return pdPASS; }
inline BaseType_t xTaskNotifyWait(uint32_t, uint32_t, uint32_t*, uint32_t) { return 0; }
inline TaskHandle_t xTaskGetCurrentTaskHandle() { return nullptr; }
inline void vTaskDelete(TaskHandle_t) {}

class IPAddress {
public:
    IPAddress() : v_(0) {}
    explicit IPAddress(uint32_t v) : v_(v) {}
    bool operator==(const IPAddress& o) const { return v_ == o.v_; }
    uint32_t v_;
};
