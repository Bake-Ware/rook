# Rook USB dongle (dongle)

The Rook RP2040 dongle (firmware in `firmware-rp2040/`) is a USB keyboard and
mouse for the machine it is plugged into. A worker talks to it over its serial
port, so that machine needs no Rook at all: plug the dongle's HID side into the
target and its serial side into a worker (or both into the same machine).

## Setup

Set the worker setting `port` to the dongle's serial device, ideally its stable
name: `/dev/serial/by-id/usb-Rook_Rook_RP2040_Zero_...`. Blank leaves the
plugin's caps unavailable. The worker's account needs read/write access to the
device (on most Linux systems: the `dialout` or `uucp` group).

Before anything is written, a port whose USB product name is known and isn't
"Rook RP2040 Zero" is refused, and every call first asks the device who it is
and stops unless it answers as a protocol-1 Rook dongle.

## Caps

| Cap | What it does |
|---|---|
| `dongle.status` | Identity and state; `probe_display=true` re-probes the display first. |
| `dongle.display` | Show up to 80 bytes of text on the dongle's display. |
| `dongle.hid.key` | Tap `key` with `mods`; media keys too (`volume_up`, `play_pause`, ...). |
| `dongle.keycodes` | Raw codes: up to six keyboard usage codes and a modifier bitmask, or one consumer `usage`. |
| `dongle.hid.move` | Move the pointer by `x`, `y` counts (relative; large moves are split into steps). |
| `dongle.hid.click` | Click `button` where the pointer is (no `x`/`y`: it is a relative mouse). |
| `dongle.hid.scroll` | Scroll by `wheel` notches and/or `pan`. |
| `dongle.hid.release` | Release every key and button. |

Every key, click and scroll is a tap: the plugin always sends `release`
afterwards, and the firmware also releases everything a second after the last
report and when the serial port closes. A failed command is never retried.

One call uses the dongle at a time (`exclusive: dongle`); while another program
has the port open the call fails with "in use".

POSIX only (Linux, macOS): the serial port is driven with the standard library,
so pyserial is no longer needed.
