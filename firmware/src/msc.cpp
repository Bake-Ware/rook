#include "msc.h"
#include "config.h"
#include "USB.h"
#include "USBMSC.h"
#include "driver/sdmmc_host.h"
#include "driver/sdmmc_defs.h"
#include "sdmmc_cmd.h"
#include <stdlib.h>

static USBMSC msc;
static sdmmc_card_t* msc_card = nullptr;
static uint32_t msc_sector_size = 512;

static int32_t mscOnRead(uint32_t lba, uint32_t offset, void* buffer, uint32_t bufsize) {
    if (!msc_card) return -1;
    size_t sectors = bufsize / msc_sector_size;
    if (sdmmc_read_sectors(msc_card, buffer, lba, sectors) != ESP_OK) return -1;
    return (int32_t)bufsize;
}

static int32_t mscOnWrite(uint32_t lba, uint32_t offset, uint8_t* buffer, uint32_t bufsize) {
    if (!msc_card) return -1;
    size_t sectors = bufsize / msc_sector_size;
    if (sdmmc_write_sectors(msc_card, buffer, lba, sectors) != ESP_OK) return -1;
    return (int32_t)bufsize;
}

static bool mscOnStartStop(uint8_t power_condition, bool start, bool load_eject) {
    // Accept all host-side stop/start/eject requests. We don't gate access on
    // them; the SD card stays available as long as the firmware is in MSC mode.
    return true;
}

bool initMSC() {
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.flags = SDMMC_HOST_FLAG_4BIT;
    host.max_freq_khz = SDMMC_FREQ_DEFAULT;

    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.clk = (gpio_num_t)SDMMC_CLK;
    slot.cmd = (gpio_num_t)SDMMC_CMD;
    slot.d0  = (gpio_num_t)SDMMC_D0;
    slot.d1  = (gpio_num_t)SDMMC_D1;
    slot.d2  = (gpio_num_t)SDMMC_D2;
    slot.d3  = (gpio_num_t)SDMMC_D3;
    slot.width = 4;
    slot.flags = SDMMC_SLOT_FLAG_INTERNAL_PULLUP;

    if (sdmmc_host_init() != ESP_OK) return false;
    if (sdmmc_host_init_slot(SDMMC_HOST_SLOT_1, &slot) != ESP_OK) return false;

    msc_card = (sdmmc_card_t*)malloc(sizeof(sdmmc_card_t));
    if (!msc_card) return false;
    if (sdmmc_card_init(&host, msc_card) != ESP_OK) {
        free(msc_card);
        msc_card = nullptr;
        return false;
    }

    msc_sector_size = msc_card->csd.sector_size ? msc_card->csd.sector_size : 512;
    uint32_t sector_count = (uint32_t)(msc_card->csd.capacity);  // capacity is in sectors per IDF docs for SDMMC

    msc.vendorID("Rook");
    msc.productID("KVM TF");
    msc.productRevision("1.0");
    msc.onRead(mscOnRead);
    msc.onWrite(mscOnWrite);
    msc.onStartStop(mscOnStartStop);
    msc.mediaPresent(true);
    msc.begin(sector_count, msc_sector_size);
    return true;
}

void teardownMSC() {
    if (msc_card) {
        free(msc_card);
        msc_card = nullptr;
    }
    sdmmc_host_deinit();
}
