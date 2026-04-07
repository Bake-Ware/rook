# Rook KVM Bridge — Developer Log

**Hardware:** LilyGo T-Dongle-S3 (ESP32-S3, USB-A stick, 0.96" ST7735 LCD, microSD slot)  
**Date:** 2026-04-06  
**Status:** Functional — HID, serial, WebSocket, TF card, file transfer, LCD all working

---

## What This Is

A USB stick that plugs into any machine and gives Claude (or any MCP client) full keyboard and serial access over WiFi. The target machine sees a normal USB keyboard and serial port. The controlling machine talks to the dongle over HTTP/WebSocket. No drivers, no agents, no network required on the target.

```
Claude ──→ MCP Server ──→ WiFi ──→ ESP32-S3 ──→ USB HID (keystrokes)
                                              ──→ USB CDC (serial I/O)
                                              ──→ TF Card (file staging)
                                              ──→ LCD (status display)
```

The Python MCP server (`server/`) wraps the firmware API so Claude can call tools like `run_command`, `send_keystrokes`, `take_screenshot`, etc.

## Architecture

### USB Composite Device

The ESP32-S3's native USB OTG runs TinyUSB with two interfaces:
- **HID Keyboard** — types keystrokes on the target
- **CDC Serial** — bidirectional serial port (appears as `/dev/ttyACM0` on Linux targets)

Custom VID/PID `1209:0001` to avoid Windows driver cache conflicts with Espressif's default `303A:1001` (which Windows remembers as a JTAG device).

### Dual-Core FreeRTOS

- **Core 1** (default): WiFi, HTTP server, WebSocket, serial polling, file transfer
- **Core 0**: LCD display task (1Hz refresh, completely independent)

### Network API (ESPAsyncWebServer)

All routes return JSON. Telesthete-namespaced aliases exist for all endpoints.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Device status, WiFi, storage, uptime |
| POST | `/type` | Type text via HID (`{"text":"hello","delay_ms":10}`) |
| POST | `/key` | Key combo (`{"modifiers":["ctrl","alt"],"key":"delete"}`) |
| GET | `/serial` | Read CDC serial buffer |
| POST | `/serial` | Write to CDC serial (`{"data":"ls\n"}`) |
| POST | `/serial/clear` | Clear serial buffer |
| WS | `/telesthete/stream` | Real-time bidirectional serial over WebSocket |
| GET | `/telesthete/drop` | List files on TF card |
| GET | `/telesthete/drop/{name}` | Download file |
| DELETE | `/telesthete/drop/{name}` | Delete file |

WebSocket protocol is JSON: `{"type":"data","payload":"..."}` for serial data, `{"type":"file_ready","path":"...","size":N}` for transfer notifications.

### File Transfer Protocol

Files can be sent from the target through the CDC serial port to the TF card using a marker protocol:

```
<<<ROOK_FILE:filename.ext;base64>>>
<base64-encoded data>
<<<ROOK_EOF>>>
```

The firmware runs a byte-level state machine that detects markers across chunk boundaries, streams base64 decode in 4KB blocks via mbedtls, and writes to `/drop/` on the SD card. On completion it notifies WebSocket clients with a `file_ready` message.

This is how screenshots work — the target captures, compresses, base64-encodes, and pipes to the serial device. The firmware decodes and saves to TF card. The MCP server downloads over HTTP.

### TF Card (SD_MMC)

4-bit SDIO on dedicated pins (CLK=12, CMD=16, D0=14, D1=17, D2=21, D3=18) — no conflict with the LCD's SPI bus. Auto-formats to FAT32 on first boot if the card is unformatted or exFAT. Files stage in `/drop/`.

### LCD Display

80x160 ST7735 via SPI (FSPI peripheral). Shows: device name, WiFi mode/status, IP address, TF card capacity, WebSocket/HTTP connection counts, file transfer progress, and uptime. Updates every second on core 0.

Backlight is **active low** — `GPIO 38 LOW = on, HIGH = off`.

## Pin Map

| Function | GPIO |
|----------|------|
| LCD CS | 4 |
| LCD DC | 2 |
| LCD RST | 1 |
| LCD MOSI | 3 |
| LCD SCLK | 5 |
| LCD BL | 38 |
| SD CLK | 12 |
| SD CMD | 16 |
| SD D0 | 14 |
| SD D1 | 17 |
| SD D2 | 21 |
| SD D3 | 18 |
| USB D-/D+ | 19/20 (native) |

## Build

```bash
# Firmware
cd firmware
pio run -t upload          # flash via USB (hold boot button)

# MCP Server
cd server
pip install -e .
rook-kvm                   # or configure as MCP server in Claude Code
```

WiFi credentials go in `firmware/include/secrets.h` (copy from `secrets.h.example`).

## Gotchas and Hard-Won Lessons

**TFT_eSPI crashes the ESP32-S3.** The popular TFT library caused immediate boot loops on SPI init. Adafruit ST7735 with explicit `SPIClass(FSPI)` works perfectly.

**LCD backlight is active low.** The T-Dongle-S3's backlight pin (GPIO 38) is inverted from what you'd expect. `LOW` = on. We spent time debugging a "dead screen" that was just `HIGH` = off. Confirmed from LilyGo's own example code.

**Windows caches USB VID/PID to driver.** If the ESP32-S3 ever enumerated as JTAG (Espressif's default 303A:1001), Windows permanently associates that VID/PID with the JTAG driver. New composite device won't work. Fix: use a different VID/PID entirely (we use 1209:0001).

**HID keystrokes go to the physical console, not RDP.** USB HID is hardware-level input. If you're accessing the target via RDP, keystrokes appear on the physical monitor, not your RDP session. This isn't a bug — it's the whole point for air-gapped use.

**Serial file transfer: don't use subshell pipes.** `(printf marker; base64 file; printf eof) > /dev/ttyACM0` produces 0-byte files. The subshell buffers or reorders the output. Use separate sequential redirects instead: `printf marker > /dev/ttyACM0; base64 file > /dev/ttyACM0; printf eof > /dev/ttyACM0`.

**Serial device permissions reset on open.** Each `> /dev/ttyACM0` reopen can reset permissions on Linux. Run `sudo chmod 666 /dev/ttyACM0` before a batch of writes, or set up a udev rule.

**exFAT TF cards won't mount.** ESP32 SD_MMC only supports FAT32. Most 32GB+ cards ship as exFAT. `SD_MMC.begin("/sdcard", false, true)` auto-formats on first boot (takes ~2 minutes for 32GB).

**ESPAsyncWebServer regex routes are unreliable.** Even with `-DASYNCWEBSERVER_REGEX=1`, parameterized regex paths for file routes collide with other registrations. Solved by using `server.onNotFound()` as a catch-all dispatcher that checks URL prefixes manually.

## Module Map

```
firmware/
├── include/
│   ├── config.h          # Pin defs, protocol constants, includes secrets.h
│   ├── secrets.h          # WiFi credentials (gitignored)
│   ├── secrets.h.example  # Template for secrets.h
│   ├── display.h
│   ├── hid.h
│   ├── http_routes.h
│   ├── serial_buf.h
│   ├── storage.h
│   ├── wifi_setup.h
│   └── ws.h
├── src/
│   ├── main.cpp           # Setup/loop orchestration
│   ├── display.cpp        # LCD task (core 0, 1Hz)
│   ├── hid.cpp            # Key lookup tables
│   ├── http_routes.cpp    # All REST endpoints
│   ├── serial_buf.cpp     # CDC buffer + file transfer state machine
│   ├── storage.cpp        # SD_MMC init and helpers
│   ├── wifi_setup.cpp     # STA with AP fallback
│   └── ws.cpp             # WebSocket serial stream
└── platformio.ini

server/
├── rook_kvm/
│   ├── bridge.py          # HTTP + WebSocket client library
│   ├── server.py          # FastMCP tool definitions
│   ├── __init__.py
│   └── __main__.py
└── pyproject.toml
```

## Telesthete Naming

This project aligns with the Telesthete transport abstraction:

- **Stream** (`/telesthete/stream`) — real-time serial I/O via WebSocket
- **Channel** (`/telesthete/channel/*`) — HID keyboard commands
- **Board** (`/telesthete/board`) — device status dashboard
- **Drop** (`/telesthete/drop/*`) — file storage on TF card

All legacy paths (`/status`, `/type`, `/key`, `/serial`) remain as aliases.
