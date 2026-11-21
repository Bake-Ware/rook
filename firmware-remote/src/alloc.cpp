// The program's operator new and delete, and a wrapper round __cxa_throw. A
// failed allocation is recorded (its size and task: diag::note_oom, kept in
// RTC memory across a restart) before std::bad_alloc is thrown; every throw is
// counted (diag::note_throw); std::terminate is recorded before it aborts.
// Since 0.12.6 a throw unwinds on the C3 too (src/eh_frame_end.c) and is
// caught where the worker catches it, but the paths whose sizes come from
// outside check the heap before they allocate, so it should not come to that.
// (new cannot return null: a std::string or vector would use it; the nothrow
// forms do return null, recorded the same way.)
#include <exception>
#include <new>
#include <stdlib.h>
#include "diag.h"

void* operator new(size_t n) {
    void* p = malloc(n ? n : 1);
    if (p) return p;
    diag::note_oom(n);
    throw std::bad_alloc();
}

void* operator new[](size_t n) { return operator new(n); }

void* operator new(size_t n, const std::nothrow_t&) noexcept {
    void* p = malloc(n ? n : 1);
    if (!p) diag::note_oom(n);
    return p;
}

void* operator new[](size_t n, const std::nothrow_t& t) noexcept { return operator new(n, t); }

void operator delete(void* p) noexcept { free(p); }
void operator delete[](void* p) noexcept { free(p); }
void operator delete(void* p, size_t) noexcept { free(p); }
void operator delete[](void* p, size_t) noexcept { free(p); }
void operator delete(void* p, const std::nothrow_t&) noexcept { free(p); }
void operator delete[](void* p, const std::nothrow_t&) noexcept { free(p); }

// Linked with -Wl,--wrap=__cxa_throw (platformio.ini): every throw, the C++
// runtime's own (std::__throw_bad_alloc, length_error...) included, comes here first.
extern "C" void __real___cxa_throw(void* obj, void* tinfo, void (*dest)(void*)) __attribute__((noreturn));
extern "C" void __wrap___cxa_throw(void* obj, void* tinfo, void (*dest)(void*)) {
    diag::note_throw();
    __real___cxa_throw(obj, tinfo, dest);
}

namespace diag {
// std::terminate (an exception nothing caught, or one thrown while unwinding): recorded, then abort.
[[noreturn]] static void on_terminate() {
    note_terminate();
    abort();
}
void install_fault_hooks() { std::set_terminate(on_terminate); }
}  // namespace diag
