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

inline uint32_t millis() {
    using namespace std::chrono;
    static auto t0 = steady_clock::now();
    return (uint32_t)duration_cast<milliseconds>(steady_clock::now() - t0).count();
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
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return (void*)1; }
inline int xSemaphoreTake(SemaphoreHandle_t, uint32_t) { return 1; }
inline int xSemaphoreGive(SemaphoreHandle_t) { return 1; }
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
