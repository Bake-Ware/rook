---
id: hardware
title: USB dongle, Android over USB, PiKVM and HDMI-CEC
order: 18
features:
  - features/esp32-s3-usb-dongle
  - features/preconfigure-a-dongle-build
  - features/pikvm
  - features/hdmi-cec-pico-w
  - plugin/worker/dongle
  - plugin/worker/adb
  - plugin/worker/pikvm
  - plugin/worker/cec
  - settings/dongle
  - settings/adb
  - settings/pikvm
  - settings/cec
since: 0.1.0
deprecated: false
---
# USB dongle, Android over USB, PiKVM and HDMI-CEC

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

## Android phones and tablets over USB

An Android phone or tablet cabled to a worker can join your band without
the Rook app, even with a broken screen. The worker drives it with the
Android Debug Bridge (`adb`) over the cable and shows it on the band as a
worker of its own, with a **via** tag naming that worker (see
[TVs and other devices](devices.md)). It answers to the same commands as a
phone running the Rook app: run a shell command, take a screenshot (works
when the display is dead), read the text on screen with where to tap it,
type, press keys, tap and swipe, device info and battery. That makes an old
tablet a safe place for agents to experiment.

**Setting it up**

1. On the tablet, once: Settings > About > tap **Build number** seven times,
   then Developer options > **USB debugging** on. If the touch screen is
   broken, plug any USB mouse into the tablet through a USB-C (OTG) adapter
   and use the pointer.
2. Install Android platform-tools (`adb`) on a worker that stays on and
   connect the tablet to it with a data cable. The tablet asks **Allow USB
   debugging?**: tick **Always allow from this computer** and tap Allow.
3. On that worker's Settings page, set **Android devices** to
   `name=serial`, for example `playground-tablet=R52N30ABCDE`. The worker's
   `adb.scan` command lists the serials it sees. Several devices are
   separated by commas.

The device then appears on the Workers page as **playground-tablet**, with
its battery level. `not paired` means it has not allowed USB debugging from
this worker yet.

**What it can and can't do.** Rook only ever talks to the serials you list,
so a phone plugged into the same computer to charge stays off the band. It
never allows USB debugging on its own, and never roots, reboots or installs
apps. Commands run as Android's shell user: they can change settings and
remove apps, so a shell command on the tablet is treated like one on any
worker. Typing is ASCII only.

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
