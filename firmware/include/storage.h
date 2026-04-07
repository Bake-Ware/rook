#pragma once
#include <Arduino.h>

bool initStorage();
bool isStorageReady();
uint64_t getStorageTotalMB();
uint64_t getStorageUsedMB();
