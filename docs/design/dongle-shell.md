# Dongle shell over USB: an Android device as a headless playground

Status: phase 1 implemented (`adb` worker plugin, Android device workers).
Phases 2 and 3 are designs, not built. User manual: `docs/manual/hardware.md`.
Device framework: `docs/design/devices.md`.

Roadmap item (Bake, 2026-10-26): "Dongle shell over USB. Needs to work; first
use: a broken Android tablet as a headless playground for the house agent."

## 1. What the house agent needs

A shell on the tablet with real output and exit codes, a way to see the screen
when the panel is dead or cracked, and a way to tap and type when the touch
layer is dead. It must not need the tablet's own screen, an app on the tablet,
or root, and it must never reach any other phone that happens to be cabled to
the same computer.

## 2. Options weighed

| Option | Output back | Works on a broken tablet | Cost | Verdict |
|---|---|---|---|---|
| (a) Dongle types a command over HID, the target writes output to the dongle's serial port | Only where the target can open a USB serial port. Unrooted Android cannot without an app. | No output path on Android | Small firmware change | Fits PCs, not this tablet |
| (b) Dongle as USB host speaking ADB (RP2040 PIO-USB) | Yes | Still needs someone to tap Allow for the dongle's key, the same first-contact problem as (c) | Large: a USB host port, the ADB protocol and RSA auth on a microcontroller, untestable without hardware | Not now |
| (c) The worker the tablet is cabled to runs `adb`; the tablet becomes a device worker | Yes: stdout, stderr, exit code, screenshots, screen text | Yes, once USB debugging is allowed | Pure worker code, testable on an emulator | **Chosen** |

Decision (rw2 agent, 2026-10-27, no open design question on the roadmap):
build (c). Android already ships the USB shell (`adbd`); every byte the agent
needs comes back over the same cable. The dongle's own job is **first
contact**: turning USB debugging on and tapping Allow on a tablet whose touch
layer is dead (section 5).

## 3. Phase 1 (built): Android devices as device workers

`rook/worker/plugins/adb.py`. A worker with `adb` installed and
`adb.devices = playground-tablet=<serial>` becomes the delegate for that
tablet. The tablet shows on the band as its own worker (`playground-tablet`,
type `android`, driver `adb`, **via** the delegate) with the cap names the
Rook Android app uses, so an agent drives it the way it drives any phone:

| Cap | Risk | What it runs |
|---|---|---|
| `shell.exec` | exec | `adb -s S shell -T -- <cmd>`; argv/cmd/stdin/cwd/env/timeout like `shell.exec` everywhere |
| `screenshot.capture` | read, sensitive | `adb exec-out screencap -p`, JPEG when ImageMagick is on the worker |
| `ui.text` | exec, sensitive (as on the app) | `uiautomator dump`, returned as text plus tap points |
| `hid.type`, `hid.key_combo`, `hid.mouse.click`, `hid.mouse.drag` | exec, physical | `input text / keyevent / keycombination / tap / swipe` |
| `device.info`, `battery.status` | read | `getprop`, `wm size`, `dumpsys power / battery` |

On the delegate: `adb.list` (configured devices and their adb state) and
`adb.scan` (every serial adb sees, to fill in the setting).

Rules:

- **Listed serials only.** Every command is `adb -s <listed serial>`; there is
  no "first device" fallback. A phone plugged into the same computer to charge
  stays off the band (`adb.scan` lists it, nothing addresses it).
- **Never authorize.** Rook does not touch adb keys or accept prompts. An
  unauthorized tablet answers every call with "someone has to tap Allow on it
  once"; the heartbeat says `not paired`.
- **No privileged adb verbs.** The plugin never runs `root`, `tcpip`,
  `connect`, `reboot`, `install` or `kill-server`. `shell.exec` runs as
  Android's `shell` user: it cannot read other apps' data, but it can change
  settings and remove apps for the current user, which is why it is `exec`.
  The policy that already holds `shell.exec` on any worker holds it here (same
  cap name, same tier floor).
- **Timeouts end the remote command.** Killing the local `adb` client closes
  the shell session and Android kills the command (checked on an Android 15 emulator).
- Output is capped at 1 MiB per stream (`truncated: true`).
- The heartbeat (`hb.android = {on, paired?, adb?, battery?}`) comes from one
  `adb devices -l` every 30 s and a battery read every 5 minutes. The first
  call starts the adb server on the delegate if none runs (as any `adb` use
  does); it keeps running after the worker stops.

## 4. Phase 2: the playground

Nothing new to build for the agent's side: the house agent calls
`playground-tablet` by name. Suggested setup, once the tablet is on the band:

1. Allow USB debugging on the tablet (section 5) and cable it to a worker that
   stays on (janitor or a Pi; not a laptop that sleeps).
2. `adb.devices = playground-tablet=<serial>` on that worker (Settings page),
   then `adb.list` to see it as `device`.
3. Tell the house agent it may experiment there (its role guidance), and keep
   Bake's own phone off any worker's `adb.devices`.

Later, if wanted: a "Playground" role with a trust entry that lets it run
`shell.exec` on `playground-tablet` without asking (only Bake adds trust
entries), and `proc.*`-style long jobs on the tablet.

## 5. Phase 3: first contact with a broken touch screen

Allowing USB debugging needs Settings > About > Build number seven times,
Developer options > USB debugging, then the "Allow USB debugging?" prompt when
the cable goes to the worker. With a dead touch layer:

1. **Now, by hand (recommended):** any USB mouse through a USB-C/OTG adapter
   gives the tablet a pointer. Five minutes, once. Tick "Always allow from this
   computer" so the worker's key sticks.
2. **Remote, twin dongle (design):** the RP2040 dongle has one USB port, so
   plugged into the tablet its serial port belongs to the tablet and the band
   cannot reach it. Two RP2040-Zeros joined by UART fix that, the topology
   Bake's Geppetto bridge already proves: board A on the worker (USB serial),
   board B in the tablet (USB keyboard and mouse), the same firmware on both.
   Protocol 2 would add a `relay` field: a command sent to A with
   `"via": "twin"` goes out A's UART, B runs it and the reply comes back the
   same way. `dongle.status` would report the twin. Not built: no Rook RP2040
   board is attached anywhere to test it on, and blind HID is of little use
   before adb works (no screenshots until then), so the hand step is cheaper.
3. **Remote, ESP32 dongle:** the LilyGo T-Dongle-S3 joins the band over Wi-Fi
   by itself and already types over USB (`kvm.*`). Plugged into the tablet
   through OTG it can do the same steps blind. It has been off the band since
   2026-08-29.

After first contact the tablet needs only a data cable to the worker.

## 6. Not done, on purpose

- **Wireless adb.** `adb connect` targets would let the tablet leave the
  cable; the plugin does not run `connect`, so a Wi-Fi serial (`ip:port`) works
  only if something else keeps the connection up. Add an explicit
  `adb.connect` setting if wanted.
- **Firmware.** No firmware changed. The RP2040 that enumerates as
  `2e8a:000a Bake-Ware Geppetto` on cachyrig is Bake's Geppetto HID bridge
  (`~/gepetto/geppetto`), not a Rook dongle: anything written to its serial
  port is typed on the machine at the other end. Rook must never set
  `dongle.port` to it.
- **The host-side shell of option (a)** for PCs without a worker. It needs the
  ESP32 dongle (the only one that reaches the band by itself) and is the old
  `run_command` of `firmware/DEVLOG.md`; revisit with the ESP32 dongle.
