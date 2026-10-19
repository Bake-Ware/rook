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

## v0.5.0 — 2026-05-17 — Telesthete band worker

Dongle now self-registers as a worker on the Telesthete band at boot,
exposing `info.*` and `kvm.*` capabilities (host, uptime, ping, type, key,
consumer, hid.set/get). Reachable from the Claude MCP connector at
`https://mcp.bakeforge.com/mcp` without any LAN access to the dongle.

Wire: XSalsa20-Poly1305 (NaCl secretbox construction) byte-for-byte
compatible with the Python reference, 27-byte band-id header + 21-byte
fragmentation envelope per SPEC §6.4. Single-fragment send only for v1;
multi-fragment inbound is dropped with a debug log. Crypto is implemented
in-firmware with poly1305-donna-32 + an inline Salsa20 / HSalsa20 because
the ESP32 Arduino mbedTLS is built with `MBEDTLS_POLY1305_C` disabled.

WiFi: primary STA tries for 15s, then falls back to the optional phone
hotspot (set via `/config`). AP stays up the whole time as a rescue path.

Settings persisted in NVS, all overridable from `/config`: `hub_host`
(default `hub.bakeforge.com`), `hub_port` (7474), `band_psk`,
`worker_name` (auto = `rook-kvm-<mac suffix>`), `phone_ssid`,
`phone_pass`. DNS record `hub.bakeforge.com → 151.145.63.234` lives in
Cloudflare (TTL 300, unproxied).

LCD: added a single-char hub indicator `H` / `h` (lime / red) on the
top-row metadata strip — solid lime means the dongle has heard from any
band peer within the last 60s.

## v0.6.0-wip — 2026-06-03 — Mouse HID + realtime stream groundwork

Goal: live keyboard+trackball forwarding from a Linux source (CachyOS desktop)
into the target the dongle is plugged into, hotkey-toggled — a software KVM
where the dongle is the HID injector and video comes from a separate PCIe HDMI
capture card. Input device is an **Elecom HUGE** trackball (8 buttons, vertical
scroll). The user's old hardware KVM mangled it because it emulates a 5-button
USB boot mouse and drops the extra buttons / hi-res scroll.

This commit lays the HID foundation; the transport (LAN-direct UDP listener)
is the next chunk.

- **Custom 8-button mouse** (`hid.cpp/.h`, new `RookMouse : USBHIDDevice`).
  Hand-rolled report descriptor: 8 buttons + 16-bit relative X/Y + 8-bit wheel
  + 8-bit AC Pan (hwheel). Report ID 2 (`HID_REPORT_ID_MOUSE`), so it coexists
  with the existing keyboard (1) and consumer (4) on the one shared HID
  interface. We deliberately do NOT instantiate the stock `USBHIDMouse` (only 5
  buttons, no independent hwheel). `RookMouseReport` is a 7-byte packed struct;
  `static_assert` guards the size.
- **Raw report streaming entry point** `hidStreamReport(report_id, data, len)` —
  no JSON, no reply, no per-event malloc. Dispatches id 1→keyboard boot report
  (via `Keyboard.sendReport`), 2→mouse, 4→consumer. This is what the realtime
  LAN path will call per input event. Honors the `hidEnabled()` kill-switch.
- **`hidReleaseAll()`** — pushes all-zero keyboard + mouse reports. The source
  client sends this on every KVM toggle so nothing sticks down when input
  switches sides.
- **`kvm.mouse` band capability** (`telesthete.cpp`) — structured
  `{dx,dy,wheel,pan,buttons}` over the existing band for immediate bench
  testing before the realtime path exists. Added to the announced caps list +
  dispatch table.
- `Mouse.begin()` added to USB bring-up in `main.cpp` (after Keyboard/Consumer,
  before `USB.begin()`).

Re-enumeration note: VID/PID stay `1209:0001`; adding a third HID report only
changes the HID descriptor, which is class-driver territory on Windows — no
repeat of the JTAG driver-cache gotcha.

### Transport (landed)

- **Fixed-port UDP listener** (`telesthete.cpp`, `HID_STREAM_PORT` 7475). Own
  `WiFiUDP` socket + dedicated `hidStreamTask` pinned to core 1 at priority 2,
  tight poll (drains bursts, `vTaskDelay(1)` only when idle) for low jitter.
  Decrypts and calls `hidStreamReport` directly — no JSON, no reply, no per-event
  alloc. WebRTC was considered and rejected: source and dongle are always on the
  same LAN (the dongle's own radio makes the laptop's corp VPN irrelevant), so
  there's no NAT to traverse.
- **Separate HID-lane key**: `derive_hid_stream_key` = HKDF with `info`
  `"hid-stream"` (vs the band's `"encryption"`). The lane uses an explicit
  24-byte random nonce per frame instead of the band's seq-derived nonce, so the
  two lanes' keystreams can never collide under the shared PSK.
- **Lean frame**: `'R''H' || nonce[24] || secretbox(tag[16] || ct)`, plaintext
  `seq[8 BE] || report_id[1] || body`. Per-frame `seq` drops stale/reordered/dup
  reports (tolerates a big backward jump as a client restart).
- **`info.host` now reports `hid_stream_port`**; `kvm.stream` added to the
  announced caps as a discovery marker.

### Source client (landed)

`client/rook_hid_kvm.py` — evdev capture of keyboard + Elecom HUGE, `EVIOCGRAB`
on toggle, evdev→HID translation (8-button mouse, boot keyboard), XSalsa20-
Poly1305 via PyNaCl with the matching derived key, UDP to the dongle. Toggle is
**double-tap Right-Ctrl**; sends an all-released report on every toggle and on
exit. See `client/README.md`.

### Gotcha: realtime seq must climb across client restarts

The firmware drops `seq <= g_last_hid_seq` (within a 4096 window) as
stale/replay. A client that starts its sequence at 0/1 every launch gets every
frame rejected until it climbs back past the last value the still-running dongle
remembers — looks exactly like "no movement / dead path." Fix: the client seeds
`seq` from epoch-ms so it's monotonic across restarts. (Hit this live: a second
test run after a host reboot sent seq 1..360 into a dongle that already had
last=360 → all dropped.)

### v0.6.1 → reverted in 0.6.2 — WiFi dropouts vs. a boot crash

Observed during realtime use: cursor/keyboard freezes for a second or two.
A 30-min uptime monitor showed the dongle was NOT rebooting (uptime monotonic)
but had ~11 brief "unreachable" blips — i.e. STA-link dropouts, not crashes.
The 2.4 GHz link is the suspect; modem-sleep (DTIM napping) and USB3 RF noise
are the usual causes.

Attempted fix in 0.6.1: `WiFi.setSleep(false)` in `setupWifi()`. **Reverted in
0.6.2** — after flashing 0.6.1 the device crash-looped, and `setSleep` was the
only code change, so it's the prime suspect (possibly a brownout from higher
always-on radio current on a marginal USB supply, or an interaction with the
mode-switching that `wifiScanVisible()` does). Backing it out restored stability.

Still want the dropout fix, but it needs a safer approach + a way to verify:
- apply PS-off only after the STA is connected, and re-apply after each
  `wifiScanVisible()` mode flip (scans reset PS);
- or `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)` (lighter) instead of NONE;
- and a bench test on USB power before flashing onto the target.

Physical mitigation that helps regardless: dongle on a **USB2 port / short
extension cable**, away from the laptop chassis + USB3, on a quiet 2.4 GHz
channel.

Side lesson: the BOOT button (GPIO0) doubles as the storage-mode toggle while
the app runs, so the flashing dance (holding/pressing BOOT) can silently flip
the dongle into MSC mode. `loadStorageMode()`'s version-change migration resets
it to internal on any firmware-version bump, which is why bumping the version
on reflash auto-recovers it.

### Still open
- Bench test end to end (needs the dongle flashed via OTA + a target).
- Optional: replace the 1 ms poll with a blocking lwip `recvfrom` if jitter shows.
- `device.usb.hid.stream` is currently a raw UDP lane, not a band Channel; fine
  for LAN, revisit only if a cross-network realtime path is ever needed.

## TODO — Future Ideas

- **BT tether to phone for internet** — let the dongle pair with a phone over
  Bluetooth (PAN/Tether profile) and use that link as its uplink. Default
  uplink to BT-tether so the dongle can phone home to the Telesthete hub
  without ever joining a local WiFi. Likely path: NimBLE-Arduino stack +
  BT-Classic PAN client. WiFi STA stays as a fallback / power-saving option.
  Needed when STA isn't an option (e.g., guest network, captive portal, no
  WiFi creds provisioned).

## 2026-06-04 — Twin-Pico wired bridge (alternative to the WiFi dongle)

New plan, new hardware: two RP2040 Picos soldered GP0<->GP1 (UART0 crossover) +
common grounds. Wired HID KVM from CachyOS to the laptop, no radio — which
**sidesteps the unresolved WiFi modem-sleep dropout entirely**. Lives in
`firmware-pico/` (separate PlatformIO project) + `client/rook_hid_pico.py`.

Key decisions / gotchas:

- **One firmware, no master/slave.** Both boards run the same binary; role is
  implicit in the data: CDC-in → relay to UART (bridge); UART-in → emit HID
  (gadget). The board the host script talks to becomes the bridge; the one on
  the laptop becomes the gadget. Crossover is bidirectional so either board can
  be the laptop side.
- **earlephilhower core, NOT the stock platform.** `platform = raspberrypi`
  only ships the Arduino-Mbed core and silently ignores
  `board_build.core = earlephilhower` (it tried to build the broken mbed
  variant — `pico.h not found`). Must use
  `platform = https://github.com/maxgerhardt/platform-raspberrypi.git`.
- **Don't redefine CFG_TUD_HID/CFG_TUD_CDC** in build_flags; the core's
  tusb_config.h sets them (HID=2, CDC=1) and warns on redefinition. Just
  `-DUSE_TINYUSB` + `board_build.usbstack = tinyusb`.
- **Stale root-owned pio packages** (`tool-mkfatfs/mklittlefs/mkspiffs`, left
  by an earlier sudo flash) make `pio run` die with PermissionError before it
  even builds. `chown -R bake:bake` them.
- Same 7-byte mouse / 8-byte keyboard / report-IDs 1/2/4 as the ESP32 firmware,
  so the host frame format is shared. Wire framing adds a sync byte + crc8
  (poly 0x07) for the UART leg.

Verified closed-loop with both boards on the same PC (write a frame to one
board's CDC, the partner injects HID into this host): mouse A→B 30/30, reverse
B→A 20/20, keyboard 'A' lands as KEY_A. Full chain proven before deploying the
gadget board to the laptop.

### 2026-06-04 — onboard client over USB MSC

Added a read-only `ROOK` USB drive to the Pico firmware carrying the host client
(`rook_hid_pico.py` + run script + README), so the dongle ships its own driver.
Now a composite **CDC + HID + MSC** device.

- `SingleFileDrive` (the obvious earlephilhower lib for this) hard-`#error`s
  under `USE_TINYUSB`, which we need for the custom HID descriptor. Dead end.
- Instead: build a 256K FAT12 image on the host (`tools/mkdisk.sh`: mkfs.fat +
  mcopy), `xxd -i` it into `src/disk_image.h` as a const array (lands in flash),
  serve it via `Adafruit_USBD_MSC` read callback. Writes rejected +
  `setWritableCallback(false)` so it mounts write-protected (no host errors).
- Flash cost: 90K -> 353K (16.9% of 2M). Trivial.
- Both boards expose the drive since firmware is identical — incl. the gadget on
  the corp laptop. Acceptable (read-only, tiny); gate setUnitReady() on role if
  it ever needs hiding there.
- Reflash-in-place without touching BOOTSEL: 1200-baud touch on the CDC port
  resets the RP2040 into the UF2 bootloader, then copy the .uf2 onto the mount.
