#include "storage.h"
#include "SD_MMC.h"
#include "FS.h"
#include "config.h"

static bool sdReady = false;

bool initStorage() {
    SD_MMC.setPins(SDMMC_CLK, SDMMC_CMD, SDMMC_D0, SDMMC_D1, SDMMC_D2, SDMMC_D3);
    sdReady = SD_MMC.begin("/sdcard", false, true);  // 4-bit, format if needed
    if (sdReady) {
        // Create drop directory if it doesn't exist
        if (!SD_MMC.exists("/drop")) {
            SD_MMC.mkdir("/drop");
        }
    }
    return sdReady;
}

bool isStorageReady() { return sdReady; }

uint64_t getStorageTotalMB() {
    return sdReady ? SD_MMC.totalBytes() / (1024 * 1024) : 0;
}

uint64_t getStorageUsedMB() {
    return sdReady ? SD_MMC.usedBytes() / (1024 * 1024) : 0;
}
