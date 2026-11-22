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
static uint32_t boots_ = 0, crashes_ = 0, brownouts_ = 0;
static volatile bool checking_ = false, found_ = false;
// Where a heap check first failed, kept across the restart (RTC memory is not
// cleared by a panic). magic says it was written by check().
struct HeapNote { uint32_t magic; uint32_t up_s; char where[40]; };
static RTC_NOINIT_ATTR HeapNote heap_note;
static const uint32_t HEAP_MAGIC = 0x48454150;   // "HEAP"
// The last fault of each kind, this boot (counts) and as it stood when the
// chip restarted (kept in RTC memory, moved to the settings at the next boot).
struct FaultNote {
    uint32_t magic, up_s, ooms, throws, oom_size;
    char oom_task[16], throw_task[16], last[48];   // last: what happened last, in words
};
static RTC_NOINIT_ATTR FaultNote fault;
static const uint32_t FAULT_MAGIC = 0x46415554;  // "FAUT"
static std::string last_fault_;

static void task_name(char out[16]) {
    const char* n = pcTaskGetName(nullptr);
    strlcpy(out, n ? n : "?", 16);
}

static void fault_begin() {                      // this boot's record, fresh
    memset(&fault, 0, sizeof fault);
    fault.magic = FAULT_MAGIC;
}


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

// A crash: the program's fault. A brownout (the supply sagged: the battery
// low, or the IR LED and the radio drawing at once) is counted apart.
static bool abnormal(esp_reset_reason_t r) {
    return r == ESP_RST_PANIC || r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT || r == ESP_RST_WDT;
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
    std::string b = settings::kv("boots"), c = settings::kv("crashes"), bo = settings::kv("brownouts");
    boots_ = (b.empty() ? 0 : (uint32_t)strtoul(b.c_str(), nullptr, 10)) + 1;
    crashes_ = (c.empty() ? 0 : (uint32_t)strtoul(c.c_str(), nullptr, 10)) + (abnormal(r) ? 1 : 0);
    brownouts_ = (bo.empty() ? 0 : (uint32_t)strtoul(bo.c_str(), nullptr, 10)) + (r == ESP_RST_BROWNOUT ? 1 : 0);
    settings::set_kv("boots", std::to_string(boots_));
    if (abnormal(r)) settings::set_kv("crashes", std::to_string(crashes_));
    if (r == ESP_RST_BROWNOUT) settings::set_kv("brownouts", std::to_string(brownouts_));
    read_summary();
    // A heap check that failed before this start: kept in the settings until the next one fails.
    if (heap_note.magic == HEAP_MAGIC) {
        heap_note.where[sizeof heap_note.where - 1] = 0;
        settings::set_kv("heapbad", std::string(heap_note.where) + " (" + std::to_string(heap_note.up_s) + " s up, boot " +
                                        std::to_string(boots_ - 1) + ")");
    }
    heap_note.magic = 0;
    heap_bad_ = settings::kv("heapbad");
    // What went wrong last before this start (an allocation failed, a throw, terminate): kept until the next.
    if (fault.magic == FAULT_MAGIC && (fault.ooms || fault.throws || fault.last[0])) {
        fault.last[sizeof fault.last - 1] = 0;
        char b[160];
        snprintf(b, sizeof b, "%s (%u s up, boot %u; %u failed allocations, %u throws)", fault.last[0] ? fault.last : "-",
                 (unsigned)fault.up_s, (unsigned)(boots_ - 1), (unsigned)fault.ooms, (unsigned)fault.throws);
        settings::set_kv("lastfault", b);
    }
    fault_begin();
    last_fault_ = settings::kv("lastfault");
    checking_ = settings::kv("heapcheck") == "1";
}

void note_oom(size_t size) {
    if (fault.magic != FAULT_MAGIC) fault_begin();
    fault.ooms++;
    fault.oom_size = (uint32_t)size;
    fault.up_s = millis() / 1000;
    task_name(fault.oom_task);
    snprintf(fault.last, sizeof fault.last, "out of memory: %u bytes in %.15s", (unsigned)size, fault.oom_task);
}

void note_throw() {
    if (fault.magic != FAULT_MAGIC) fault_begin();
    fault.throws++;
    fault.up_s = millis() / 1000;
    task_name(fault.throw_task);
    char was[48];
    strlcpy(was, fault.last, sizeof was);
    if (strncmp(was, "out of memory", 13) == 0)       // the throw that follows a failed allocation says so
        snprintf(fault.last, sizeof fault.last, "throw after %.30s", was);
    else
        snprintf(fault.last, sizeof fault.last, "throw in %.15s", fault.throw_task);
}

void note_terminate() {
    if (fault.magic != FAULT_MAGIC) fault_begin();
    char t[16], was[48];
    task_name(t);
    strlcpy(was, fault.last, sizeof was);
    snprintf(fault.last, sizeof fault.last, "terminate in %.15s: %.24s", t, was);
    esp_rom_printf("terminate in %s (%s)\n", t, was);
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
    o["crashes"] = crashes_;          // the program's (panic, watchdog); before 0.12.7 brownouts were counted here too
    o["brownouts"] = brownouts_;
    // The battery: the XIAO ESP32-C3 has no divider from its battery pads to an
    // ADC pin, so its voltage cannot be read without adding one.
    o["battery"] = "not measured (no divider on the XIAO C3)";
    if (checking_) o["heap_check"] = true;
    if (fault.ooms) {
        o["oom"] = fault.ooms;                    // allocations that failed, this boot
        char b[48];
        snprintf(b, sizeof b, "%u bytes in %.15s", (unsigned)fault.oom_size, fault.oom_task);
        o["oom_last"] = b;
    }
    if (fault.throws) o["throws"] = fault.throws;
    if (!last_fault_.empty()) o["last_fault"] = last_fault_;
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
