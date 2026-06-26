#pragma once
#include <Arduino.h>

// Mass Storage Class — exposes the SD card as a USB block device to the host.
// Only valid when boot mode is STORAGE_MODE_MSC. In STORAGE_MODE_INTERNAL,
// these are not called and SD_MMC owns the card filesystem-side.
bool initMSC();   // init SDMMC host + card at block level, register USBMSC
void teardownMSC();
