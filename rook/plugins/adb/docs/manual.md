# Android over adb

Drives Android phones and tablets cabled to a worker over the Android Debug
Bridge: a shell, screenshots, the text on screen, typing, keys, taps and
swipes. Nothing is installed on the device, so a tablet with a broken screen
still makes a headless playground.

Each device is an **appliance**: it runs no Rook worker. Plug it into a worker
that has `adb` (Android platform-tools), run `adb.scan` there to see its
serial, and onboard it with that serial; the worker it is plugged into is its
delegate. Name the device in the call; the caps don't take a serial.

Rook never authorizes itself: a device that hasn't allowed USB debugging from
that computer answers "has not allowed USB debugging"; a person taps **Allow**
on it once. Only the onboarded serial is ever addressed, so a phone plugged in
just to charge stays off the band. Nothing here runs `adb root`, `tcpip`,
`reboot`, `install` or `kill-server`.

| Cap | What it does |
|---|---|
| `adb.scan` | Every device adb sees on this worker: serial, state (`device`, `unauthorized`, `offline`), model. On the worker, not a device. |
| `adb.shell.exec` | Runs a command on the device as Android's `shell` user: `argv` (quoted for you) or `cmd` (a line for the device's `sh`), with `stdin`, `cwd`, `env` and `timeout` (seconds, up to 600). Returns `{code, stdout, stderr}`. Output past 1 MiB stops the command (`truncated`). The shell user can change settings, uninstall apps for the user and read or delete shared storage (photos, downloads). |
| `adb.battery.status` | Percent, charging, what it is plugged into, status, temperature. |
| `adb.screen.capture` | The screen, even with a dead display: a JPEG when ImageMagick is on the worker, else a PNG. Its pixels are `hid.click`'s `x`/`y`. |
| `adb.ui.screen` | The text on screen top to bottom, each with the centre to tap, and the app in front. `limit` keeps the first few. |
| `adb.hid.type` | Types ASCII text into whatever has focus (a newline presses Enter; `enter=true` presses it after). Other text: `adb.shell.exec`. |
| `adb.hid.key` | One key with modifiers held: `key=home`, `key=a, mods=[ctrl]`, or any `KEYCODE_*` name. |
| `adb.hid.click` | Taps at `x`, `y` (both required; left button only). |
| `adb.hid.drag` | Swipes from `x1`,`y1` to `x2`,`y2` over `duration` (default 300ms). |

The device also answers the shared device caps: `screen.capture`, `ui.screen`,
`hid.type`, `hid.key`, `hid.click` and `hid.drag` sent to it reach the caps
above.

Key names for `hid.key`: home, back, recents, menu, power, wake, sleep, enter,
tab, escape, space, backspace, delete, up, down, left, right, volume_up,
volume_down, mute, notifications, search, a letter or digit.

## Instance settings

- `serial`: the device's adb serial (from `adb.scan`). Blank uses the
  instance's name as the serial.

The 0.4 `android.*` names are gone (0.5 is a clean cut). `hid.key_combo`'s `keys="ctrl+a"` is now
`key=a, mods=[ctrl]`. `adb.list` is gone: devices are listed with the other
appliances. `device.info` is not ported (use `adb.shell.exec` with `getprop`).
