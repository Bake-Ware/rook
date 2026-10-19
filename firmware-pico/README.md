# Rook Pico HID bridge

A wired, zero-radio alternative to the ESP32 dongle: **two RP2040 Picos**
soldered together act as a USB-HID KVM from this PC to the work laptop. No WiFi,
no encryption, no corp-VPN concerns — it's a wire. Sidesteps the WiFi
modem-sleep dropouts that plagued the ESP32 path.

## How it works

Both Picos run the **same** firmware (`src/main.cpp`). Role is decided at
runtime by which input is active — there is no master/slave flag:

```
       USB                UART0 (GP0<->GP1)              USB
CachyOS ===> [Pico A] ------ cross-soldered ------ [Pico B] ===> Laptop
 (script)    bridge                                 gadget      (HID kbd+mouse)
```

* A board fed bytes on its **USB-CDC** serial port copies them verbatim out the
  UART → it's the *bridge*.
* A board fed bytes on its **UART** decodes them into USB-HID reports → it's the
  *gadget*.

Each board does both; it just exercises whichever applies to where it's plugged
in. The crossover is bidirectional, so it works regardless of which board lands
on the laptop. Both enumerate as the same composite **HID + CDC** device
(`2e8a:000a "Rook HID Bridge"`); the idle interface on each side is harmless.

## Wiring

Two Picos, common ground, UART0 crossover:

| Pico A | Pico B | why |
|--------|--------|-----|
| GP0 (UART0 TX) | GP1 (UART0 RX) | A talks, B listens |
| GP1 (UART0 RX) | GP0 (UART0 TX) | B talks, A listens |
| GND ×4 | GND ×4 | common reference |

Link runs at 921600 baud — overkill on purpose so the wire is never the latency
floor (USB HID polls at 1ms; a frame is ~100µs on the wire).

## Wire frame

Identical on the CDC link and the UART link, so the bridge is a dumb byte pipe:

```
0xAB | type | len | payload[len] | crc8(type,len,payload)     crc8 poly 0x07, init 0
  type 1 = keyboard (8 bytes: mods, reserved, key[6])
  type 2 = mouse    (7 bytes: buttons, x:i16le, y:i16le, wheel:i8, pan:i8)
  type 4 = consumer (2 bytes: usage:u16le)
```

Report IDs (1/2/4) and the 8-button mouse layout match the ESP32 rook firmware,
so the host frame format is shared.

## Build & flash

```sh
export PLATFORMIO_CORE_DIR=/home/bake/.platformio
pio run                         # builds .pio/build/pico/firmware.uf2
```

Flash by holding BOOTSEL while plugging in (board mounts as `RPI-RP2`), then
copy the UF2 onto it:

```sh
cp .pio/build/pico/firmware.uf2 /run/media/$USER/RPI-RP2/
```

Do both boards. After flashing each re-enumerates as `Rook HID Bridge`.

> Uses the **earlephilhower** Arduino-Pico core via the
> `maxgerhardt/platform-raspberrypi` fork (the stock `raspberrypi` platform only
> ships the Arduino-Mbed core and ignores `board_build.core`). USB stack is
> Adafruit TinyUSB (`board_build.usbstack = tinyusb`).

## Onboard client (USB drive)

The firmware also serves a tiny **read-only `ROOK` USB stick** (a 256K FAT12
image embedded in flash) carrying the host client itself — `rook_hid_pico.py`,
`run_pico.sh`, and a `README.txt`. Plug the dongle into any machine and the
script you need to drive it is right there. It rides a third USB interface
alongside HID + CDC (composite HID + CDC + MSC).

Regenerate the image whenever the client files change, then rebuild + reflash:

```sh
tools/mkdisk.sh        # rebuilds src/disk_image.h from ../client/*
pio run                # re-embed + build
```

Notes:
- Both boards expose the drive (identical firmware), so the gadget side shows a
  small read-only `ROOK` stick on the **laptop** too. It's write-protected and
  harmless; if you'd rather hide it there, gate `usb_msc.setUnitReady()` on a
  bridge-vs-gadget signal (it currently always reports ready).
- Couldn't use the core's `SingleFileDrive` — it `#error`s under `USE_TINYUSB`,
  which we require for the custom composite HID. Hence the embedded-FAT +
  `Adafruit_USBD_MSC` approach.

## Deploy

1. Leave **one** Pico plugged into CachyOS (becomes the bridge).
2. Plug the **other** Pico into the laptop (becomes the HID gadget).
3. On CachyOS: `sudo ../client/run_pico.sh` (auto-detects the bridge port).
4. Double-tap **Right-Ctrl** to start driving the laptop; double-tap to release.

## Verified

Closed-loop test with both boards on the same PC (write a frame to one board's
CDC, watch the partner inject HID into the host):

* mouse A→B: 30 frames in → 30 REL events out on the partner, 0 on the bridge.
* mouse B→A (reverse): 20 in → 20 out — crossover is bidirectional.
* keyboard A→B: 'A' frame → partner's keyboard reports KEY_A.
