// The end of the C++ unwinder's table, which this Arduino core's link leaves
// out (GCC's crtend.o is not linked, so nothing ends .eh_frame with the zero
// word libgcc looks for). The first exception thrown after boot made libgcc
// walk past the table into the next image segment's header (its load address,
// read as a record length of about 1 GB): a load access fault in
// classify_object_over_fdes, or an assert in uw_init_context_1, so every
// throw on the C3 was fatal. The linker script puts .eh_frame_hdr straight
// after .eh_frame, 8-aligned, in the same flash segment: four zero bytes
// there end the table. (Xtensa builds link crtend and have their end already.)
#include <stdint.h>

#if defined(__riscv)
__attribute__((section(".eh_frame_hdr"), used, aligned(8))) const uint32_t rook_eh_frame_end[2] = {0, 0};
#endif
