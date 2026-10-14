---
id: hardware
title: USB dongle, PiKVM and HDMI-CEC
order: 18
features:
  - features/esp32-s3-usb-dongle
  - features/preconfigure-a-dongle-build
  - features/pikvm
  - features/hdmi-cec-pico-w
  - plugin/worker/dongle
  - plugin/worker/pikvm
  - plugin/worker/cec
  - settings/dongle
  - settings/pikvm
  - settings/cec
since: 0.1.0
deprecated: false
---
# USB dongle, PiKVM and HDMI-CEC

Rook can reach machines where no software can run: a computer stuck in its
BIOS, one that has frozen, or the TV in the living room.

## The USB dongle

A small USB stick (the LilyGo T-Dongle-S3) running Rook's own firmware. Plug it
into any computer and the computer sees an ordinary **USB keyboard** and a
**serial port**. The dongle joins your band over Wi-Fi by itself, so nothing is
installed on the computer. It works in BIOS and UEFI setup, boot menus,
installers and login screens.

- Type text, press key combinations (Ctrl+Alt+Del, F-keys) and media keys.
- Act as a **Bluetooth keyboard** for phones, tablets and TVs.
- Talk to the computer's serial console (bootloaders, small boards, recovery).
- Switch the keyboard off without a reboot.
- Its microSD card can stay with the dongle for files or be handed to the
  computer as a USB flash drive.

The little screen shows its status. Wi-Fi networks, band settings and modes are
managed from the dongle's own web page or a serial menu. The dongle has its own
update path; it does not take part in worker updates.

**Building a dongle for your band.** With a current pairing code, run
`python3 firmware/scripts/dongle-band-config.py --server https://<your-hub>
--udp-hub <relay-address>:7474`. It writes a private settings file that the
firmware build includes. The finished firmware contains your band key: keep it
private.

A worker connected to a dongle by USB uses the **Dongle** setting (its serial
device) to reach it.

## PiKVM

A PiKVM is a small box that captures another computer's screen and acts as its
keyboard, mouse and power button. Run a worker on (or near) the PiKVM and set
its **PiKVM** address, user and password on that worker's Settings page. You
get a screenshot of the attached computer even from its firmware setup,
keyboard and mouse, and the power and reset buttons. Together that is a full
remote console.

## HDMI-CEC

A Raspberry Pi Pico W with Pico-CEC firmware, plugged into an HDMI port, lets
Rook send HDMI-CEC commands: switch a TV on or off, change the volume, change
input. Set its address in the worker's **HDMI-CEC** settings.
