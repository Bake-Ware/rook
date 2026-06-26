#include "device_mode.h"
#include "config.h"
#include <Preferences.h>
#include "soc/rtc_cntl_reg.h"
#include "esp_system.h"

static Preferences prefs;
static StorageMode cachedMode = STORAGE_MODE_INTERNAL;
static bool hid_enabled = true;

static const char* NVS_NS = "rook";
static const char* NVS_KEY_MODE = "storage_mode";
static const char* NVS_KEY_VER  = "fw_version";

StorageMode loadStorageMode() {
    // Open for write so we can do schema migration if firmware version changed.
    prefs.begin(NVS_NS, false);
    String storedVer = prefs.getString(NVS_KEY_VER, "");
    if (storedVer != ROOK_FW_VERSION) {
        // First boot of this firmware version: reset mode to INTERNAL to
        // avoid persisting a bad state across upgrades. Bump version marker.
        prefs.putUChar(NVS_KEY_MODE, STORAGE_MODE_INTERNAL);
        prefs.putString(NVS_KEY_VER, ROOK_FW_VERSION);
    }
    uint8_t m = prefs.getUChar(NVS_KEY_MODE, STORAGE_MODE_INTERNAL);
    prefs.end();
    if (m != STORAGE_MODE_INTERNAL && m != STORAGE_MODE_MSC) m = STORAGE_MODE_INTERNAL;
    cachedMode = (StorageMode)m;
    return cachedMode;
}

void saveStorageMode(StorageMode m) {
    prefs.begin(NVS_NS, false);  // read-write
    prefs.putUChar(NVS_KEY_MODE, (uint8_t)m);
    prefs.end();
    cachedMode = m;
}

StorageMode currentStorageMode() { return cachedMode; }

const char* storageModeName(StorageMode m) {
    return (m == STORAGE_MODE_MSC) ? "msc" : "internal";
}

void rebootIntoMode(StorageMode m) {
    saveStorageMode(m);
    delay(150);  // let any in-flight HTTP response flush
    ESP.restart();
}

bool hidEnabled() { return hid_enabled; }
void setHidEnabled(bool on) { hid_enabled = on; }

void rebootIntoBootloader() {
    // Set RTC "force download mode" flag so the ROM bootloader stays in
    // download mode regardless of GPIO 0. Then reset. The chip comes back
    // up enumerating as 303A:1001 ready for esptool.
    REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    delay(100);
    esp_restart();
}
