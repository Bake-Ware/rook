# Rook RP2040-Zero dongle

Firmware version **0.1.0**, serial protocol **1**. A USB-powered keyboard,
mouse and consumer-control device with an optional SSD1306 OLED. Separate
build from the ESP32 firmware; no Wi-Fi, Bluetooth, HTTP server, SD storage,
or independent band membership. A computer's Rook worker bridges band calls
to the dongle over USB. HID input goes to that same computer.

## OLED wiring

| OLED | RP2040-Zero |
| --- | --- |
| SDA | GPIO28 |
| SCL | GPIO29 |
| GND | GND |
| VCC | 3V3 |

Default: **SSD1306 128×64**, I²C0 at 100 kHz. Detects address `0x3c` or `0x3d`.
Pins are GPIO numbers, not physical header positions. For 128×32, change
`ROOK_OLED_HEIGHT` in `platformio.ini`. SH1106 needs a different display driver.
The firmware works without an OLED. After attaching one, send `display_probe`.
The display reports USB connection, not band connectivity.

## Build and flash

```sh
pio run -d firmware-rp2040
```

Hold BOOT while connecting USB (or hold BOOT and tap RESET), then copy
`.pio/build/rp2040-zero/firmware.uf2` to the **RP2040-Zero's** `RPI-RP2` drive.
Make sure you are flashing the RP2040-Zero and not another attached RP2040 board.
The application product is **Rook RP2040 Zero**, manufacturer **Rook**;
its serial number comes from the board. The application USB ID is `2e8a:000a`,
distinct from the ROM bootloader `2e8a:0003`. Identify by product and serial,
not VID/PID alone. Disconnect VM USB redirection when controlling from the host.

## Host worker integration

Install `pyserial>=3.5` in the worker's Python environment, set
`ROOK_DONGLE_PORT` to the dongle's stable `/dev/serial/by-id/...` path, and
restart a worker running this source. The user running it needs serial-port
access. This initial bridge is tested on Linux; Windows serial opening needs
platform-specific handling of the `exclusive` option.

It adds `dongle.status`, `dongle.display`, `dongle.display_probe`,
`dongle.keyboard`, `dongle.mouse`, `dongle.consumer`, and `dongle.release`
to the **host worker**, not a separate dongle entry. No PSK is stored on the
board. The opt-in plugin does not alter the existing `hid.*` backend.

Keyboard takes up to six USB HID usage codes and a modifier mask. Mouse
moves are relative signed -127..127, with a five-button mask. Consumer takes
a 16-bit HID consumer usage. Each host-plugin call releases the held input
before closing. Firmware also releases on serial disconnect and after one
second without another HID report. Commands are not automatically retried.
Use `dongle.display(text="Hello Rook")` to test without injecting input.

## Serial protocol

115200 baud, newline-delimited JSON, maximum 1023 bytes before newline.
No boot chatter on this port. Every reply echoes the caller's `id` and
contains `ok`. The local serial interface is trusted; band authorization is
provided by the host worker. Examples:

```json
{"id":"1","cmd":"status"}
{"id":"2","cmd":"display","text":"Hello Rook"}
{"id":"3","cmd":"display_probe"}
{"id":"4","cmd":"keyboard","mods":0,"keys":[4]}
{"id":"5","cmd":"release"}
{"id":"6","cmd":"mouse","buttons":0,"x":10,"y":0,"wheel":0,"pan":0}
{"id":"7","cmd":"consumer","usage":233}
```

`keyboard` example presses A: use only with a controlled target and follow
with release. Text/Unicode typing and absolute mouse positioning aren't
implemented. There is no automatic input on boot.

Hardware reference: [Waveshare RP2040-Zero](https://www.waveshare.com/wiki/RP2040-Zero).
USB core: [Arduino-Pico USB](https://arduino-pico.readthedocs.io/en/latest/usb.html).
