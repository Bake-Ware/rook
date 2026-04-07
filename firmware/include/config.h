#pragma once

// ---- WiFi Configuration ----
// Create secrets.h from secrets.h.example with your credentials
#include "secrets.h"

// ---- Server ----
#define HTTP_PORT 80

// ---- Serial Buffer ----
#define SERIAL_BUF_SIZE 16384

// ---- HID ----
#define DEFAULT_KEY_DELAY_MS 10

// ---- TF Card (SD_MMC 4-bit) ----
#define SDMMC_CLK   12
#define SDMMC_CMD   16
#define SDMMC_D0    14
#define SDMMC_D1    17
#define SDMMC_D2    21
#define SDMMC_D3    18

// ---- LCD Pins ----
#define LCD_CS    4
#define LCD_DC    2
#define LCD_RST   1
#define LCD_MOSI  3
#define LCD_SCLK  5
#define LCD_BL   38

// ---- File Transfer Protocol ----
#define FILE_MARKER_BEGIN "<<<ROOK_FILE:"
#define FILE_MARKER_END   "<<<ROOK_EOF>>>"
#define FILE_DROP_DIR     "/sdcard/drop"

// ---- Telesthete ----
#define TELESTHETE_NAME "rook-kvm"
