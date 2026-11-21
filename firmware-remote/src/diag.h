// Crashes, seen without a cable: why the remote last started (esp_reset_reason
// as a word), how many times it has started and how many of those followed a
// crash (kept in the settings), and when a core dump is in its coredump
// partition, a one-line summary of it (the task, where it stopped, the code
// addresses on its stack) that addr2line turns into a place in the build.
// The dump itself is read out in pieces small enough for the band, and erased.
#pragma once
#include <ArduinoJson.h>
#include <string>

namespace diag {

void begin();                          // once, early in setup(): counts this start, reads the summary
void fill(JsonObject o);               // reset, boots, crashes, coredump {size, summary}
bool coredump_size(size_t& size);      // false: none
// length bytes of the dump from offset, as base64. False with err.
bool coredump_read(size_t offset, size_t& length, std::string& b64, std::string& err);   // length: what was read
bool coredump_erase(std::string& err);

// Heap checks (off unless set on: remote.map heap_check): check(where) walks
// every heap (heap_caps_check_integrity_all, a few ms) and the first place one
// is found broken is kept in RTC memory (no allocation, survives the restart
// the corruption will cause) and reported by remote.state as heap_bad.
void set_heap_check(bool on);
bool heap_check_on();
void check(const char* where);

// Faults, kept in RTC memory (no allocation; it survives the restart they
// may cause) and reported by remote.state after it: an allocation that failed
// (oom: its size and task), a C++ exception thrown (throw: its task; one that
// is caught is harmless, but on a C3 before 0.12.6 every throw was fatal),
// std::terminate. Safe from any task, and from inside operator new.
void note_oom(size_t size);
void note_throw();
void note_terminate();
void install_fault_hooks();             // std::set_terminate; call first thing in setup()

}  // namespace diag
