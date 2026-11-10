# Rook ESP32-S3 dongle (dongle-s3)

The Rook ESP32-S3 dongle (firmware in `firmware/`, a LilyGo T-Dongle-S3) is
its own worker on the band: plugged into a machine, it is a USB keyboard, media
keys and a serial port for that machine, and it can pair as a Bluetooth
keyboard with another. The machine it is plugged into needs no Rook at all.

This manifest is builtin: the firmware serves the caps and announces only the
manifest's hash, so the hub's library must carry this plugin (every install
level links the `devices` package). The firmware build computes the hash from
this file; change a cap here and the firmware must be rebuilt and flashed.

## Setup

Flash the firmware and give it Wi-Fi, the hub's UDP relay address and the band
PSK (`firmware/DEVLOG.md`, "Joins a 0.5 band"). It shows up on Workers under
its name (default `rookdongle`).

## Caps

| Cap | What it does |
|---|---|
| `dongle-s3.info.host` | Device, firmware version, chip, MAC, Wi-Fi network, IP, free heap. |
| `dongle-s3.info.uptime` | Milliseconds since it started. |
| `dongle-s3.info.ping` | Answers at once, with `echo` back. |
| `dongle-s3.hid.type` | Type `text` (US layout) on the USB host; `enter` presses Enter after; `delay_ms` between keys (default 10). |
| `dongle-s3.hid.key` | Tap `key` with `mods` (ctrl, shift, alt, meta) held. |
| `dongle-s3.hid.consumer` | Tap a media key by name (`volume_up`, `play_pause`, ...) or raw usage `code`. |
| `dongle-s3.hid.status` | Whether the USB keyboard is switched on. |
| `dongle-s3.hid.set` | Switch the USB keyboard on or off (`enabled`). |
| `dongle-s3.bt.type`, `bt.key`, `bt.consumer` | The same on the paired Bluetooth host. |
| `dongle-s3.bt.status` | Whether a Bluetooth host is connected. |
| `dongle-s3.serial.write` | Write `data` to the USB serial port the host sees. |
| `dongle-s3.serial.read` | Take up to `max` (600) bytes the host wrote, base64. |
| `dongle-s3.serial.status` | Bytes waiting and whether a file transfer is running. |

It provides the `hid.type`, `hid.key` and `hid.status` contracts, so
`hid.key` with the dongle as the target works like any other keyboard.
