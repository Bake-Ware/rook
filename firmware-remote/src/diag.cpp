#include "diag.h"
#include <Arduino.h>
#include <esp_core_dump.h>
#include <esp_flash.h>
#include <esp_attr.h>
#include <esp_heap_caps.h>
#include <esp_rom_sys.h>
#include <esp_system.h>
#include "rc_util.h"
#include "settings.h"

namespace diag {

static std::string reason_, summary_, heap_bad_;
static uint32_t boots_ = 0, crashes_ = 0;
static volatile bool checking_ = false, found_ = false;
// Where a heap check first failed, kept across the restart (RTC memory is not
// cleared by a panic). magic says it was written by check().
struct HeapNote { uint32_t magic; uint32_t up_s; char where[40]; };
static RTC_NOINIT_ATTR HeapNote heap_note;
static const uint32_t HEAP_MAGIC = 0x48454150;   // "HEAP"


static const char* reason_word(esp_reset_reason_t r) {
    switch (r) {
        case ESP_RST_POWERON: return "power_on";
        case ESP_RST_EXT: return "external";
        case ESP_RST_SW: return "software";
        case ESP_RST_PANIC: return "panic";
        case ESP_RST_INT_WDT: return "interrupt_watchdog";
        case ESP_RST_TASK_WDT: return "task_watchdog";
        case ESP_RST_WDT: return "watchdog";
        case ESP_RST_DEEPSLEEP: return "deep_sleep";
        case ESP_RST_BROWNOUT: return "brownout";
        case ESP_RST_SDIO: return "sdio";
        default: return "unknown";
    }
}

static bool abnormal(esp_reset_reason_t r) {
    return r == ESP_RST_PANIC || r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT ||
           r == ESP_RST_BROWNOUT;
}

static bool code_addr(uint32_t w) {
    return (w >= 0x42000000 && w < 0x42800000) || (w >= 0x40370000 && w < 0x403E0000);
}

// "task rook pc 0x403828a0 ..., at 0x... 0x..., build 7e979fd3": the summary
// the core dump keeps, read once (it is about 1.1 KB, freed after).
static void read_summary() {
    size_t addr = 0, size = 0;
    if (esp_core_dump_image_get(&addr, &size) != ESP_OK || !size) return;
    summary_ = "a core dump of " + std::to_string(size) + " bytes";
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH && CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF
    esp_core_dump_summary_t* s = (esp_core_dump_summary_t*)malloc(sizeof(esp_core_dump_summary_t));
    if (!s) return;
    if (esp_core_dump_get_summary(s) == ESP_OK) {
        char b[220];
        int n = snprintf(b, sizeof b, "task %.15s pc 0x%08x", s->exc_task, (unsigned)s->exc_pc);
#if defined(__riscv)
        n += snprintf(b + n, sizeof b - n, " cause %u ra 0x%08x", (unsigned)s->ex_info.mcause, (unsigned)s->ex_info.ra);
        // The code addresses on the crashed task's stack, innermost first (what addr2line wants).
        int k = 0;
        for (uint32_t i = 0; i + 4 <= s->exc_bt_info.dump_size && k < 12 && n < (int)sizeof b - 12; i += 4) {
            uint32_t w;
            memcpy(&w, s->exc_bt_info.stackdump + i, 4);
            if (!code_addr(w)) continue;
            n += snprintf(b + n, sizeof b - n, k ? " %08x" : ", at %08x", (unsigned)w);
            k++;
        }
#else
        n += snprintf(b + n, sizeof b - n, " cause %u", (unsigned)s->ex_info.exc_cause);
        for (uint32_t i = 0; i < s->exc_bt_info.depth && i < 12 && n < (int)sizeof b - 12; i++)
            n += snprintf(b + n, sizeof b - n, i ? " %08x" : ", at %08x", (unsigned)s->exc_bt_info.bt[i]);
#endif
        if (n < (int)sizeof b - 20) snprintf(b + n, sizeof b - n, ", build %.8s", (const char*)s->app_elf_sha256);
        summary_ = b;
    }
    free(s);
#endif
}

void begin() {
    esp_reset_reason_t r = esp_reset_reason();
    reason_ = reason_word(r);
    std::string b = settings::kv("boots"), c = settings::kv("crashes");
    boots_ = (b.empty() ? 0 : (uint32_t)strtoul(b.c_str(), nullptr, 10)) + 1;
    crashes_ = (c.empty() ? 0 : (uint32_t)strtoul(c.c_str(), nullptr, 10)) + (abnormal(r) ? 1 : 0);
    settings::set_kv("boots", std::to_string(boots_));
    if (abnormal(r)) settings::set_kv("crashes", std::to_string(crashes_));
    read_summary();
    // A heap check that failed before this start: kept in the settings until the next one fails.
    if (heap_note.magic == HEAP_MAGIC) {
        heap_note.where[sizeof heap_note.where - 1] = 0;
        settings::set_kv("heapbad", std::string(heap_note.where) + " (" + std::to_string(heap_note.up_s) + " s up, boot " +
                                        std::to_string(boots_ - 1) + ")");
    }
    heap_note.magic = 0;
    heap_bad_ = settings::kv("heapbad");
    checking_ = settings::kv("heapcheck") == "1";
}

void set_heap_check(bool on) {
    checking_ = on;
    settings::set_kv("heapcheck", on ? "1" : "");
}

bool heap_check_on() { return checking_; }

void check(const char* where) {
    if (!checking_ || found_) return;
    if (heap_caps_check_integrity_all(true)) return;
    found_ = true;                                  // the first is the one that matters
    heap_note.up_s = millis() / 1000;
    strlcpy(heap_note.where, where, sizeof heap_note.where);
    heap_note.magic = HEAP_MAGIC;
    esp_rom_printf("heap check failed at %s\n", where);
}

void fill(JsonObject o) {
    o["reset"] = reason_;
    o["boots"] = boots_;
    o["crashes"] = crashes_;
    if (checking_) o["heap_check"] = true;
    if (found_) o["heap_bad"] = std::string(heap_note.where) + " (now, " + std::to_string(heap_note.up_s) + " s up)";
    else if (!heap_bad_.empty()) o["heap_bad"] = heap_bad_;
    size_t size = 0;
    if (coredump_size(size)) {
        JsonObject d = o["coredump"].to<JsonObject>();
        d["size"] = (unsigned)size;
        if (!summary_.empty()) d["summary"] = summary_;
    }
}

bool coredump_size(size_t& size) {
    size_t addr = 0;
    size = 0;
    return esp_core_dump_image_get(&addr, &size) == ESP_OK && size > 0;
}

bool coredump_read(size_t offset, size_t& length, std::string& b64, std::string& err) {
    size_t addr = 0, size = 0;
    if (esp_core_dump_image_get(&addr, &size) != ESP_OK || !size) { err = "no core dump"; return false; }
    if (offset >= size) { err = "offset is past the end (" + std::to_string(size) + " bytes)"; return false; }
    if (length > size - offset) length = size - offset;
    if (ESP.getMaxAllocHeap() < length * 3 + 4096) { err = "worker error: out of memory for that piece; ask for less"; return false; }
    uint8_t* buf = (uint8_t*)malloc(length);
    if (!buf) { err = "worker error: out of memory"; return false; }
    esp_err_t e = esp_flash_read(nullptr, buf, addr + offset, length);
    if (e != ESP_OK) {
        free(buf);
        err = "flash read failed (" + std::to_string((int)e) + ")";
        return false;
    }
    b64 = rc::b64(buf, length);
    free(buf);
    return true;
}

bool coredump_erase(std::string& err) {
    esp_err_t e = esp_core_dump_image_erase();
    if (e != ESP_OK) { err = "erase failed (" + std::to_string((int)e) + ")"; return false; }
    summary_.clear();
    return true;
}

}  // namespace diag
