#pragma once
#include <Arduino.h>

// Storage ownership mode — chosen at boot, persisted in NVS.
// Switching modes requires a reboot (USB descriptor changes between modes).
enum StorageMode {
    STORAGE_MODE_INTERNAL = 0,  // firmware owns SD card; SD_MMC mounted; /telesthete/drop works
    STORAGE_MODE_MSC      = 1,  // host owns SD card via USB Mass Storage; firmware sees no FS
};

// Load/save persistent mode (NVS namespace "rook").
StorageMode loadStorageMode();
void saveStorageMode(StorageMode m);
StorageMode currentStorageMode();

const char* storageModeName(StorageMode m);

// Reboots into the given mode. Returns only on failure (it won't).
void rebootIntoMode(StorageMode m);

// HID kill-switch — runtime bool, no reboot.
bool hidEnabled();
void setHidEnabled(bool on);

// Software-triggered reboot into the ROM bootloader (USB download mode).
// Sets the RTC "force download" flag and resets. After the chip comes
// back up enumerating as VID:PID 303A:1001, esptool can flash without
// the physical hold-BOOT-while-plugging dance.
void rebootIntoBootloader();
