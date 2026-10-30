# Desktop keyboard and mouse (hid-desktop)

Types, presses keys, moves, clicks and drags on the desktop of the machine the
worker runs on. It needs a desktop session (`requires.hardware: display`).

| Cap | What it does |
|---|---|
| `hid-desktop.hid.type` | Type `text`; `enter=true` presses Enter after it. A newline presses Enter. |
| `hid-desktop.hid.key` | Press `key` with `mods` held (`ctrl`, `shift`, `alt`, `meta`). |
| `hid-desktop.hid.move` | Move the pointer to pixel `x`, `y` (top-left origin). |
| `hid-desktop.hid.click` | Click `button` (`left`, `right`, `middle`), at `x`, `y` when given. |
| `hid-desktop.hid.drag` | Press at `x1`, `y1`, move to `x2`, `y2` over `duration`, release. |
| `hid-desktop.hid.status` | The keyboard and pointer tools in use (null when none is installed). |

## Tools

- **Linux:** the first installed of `xdotool` (X11), `wtype` (Wayland, keyboard
  only) and `ydotool` (Wayland, needs its daemon). Typing falls through to the
  next tool when one fails, so `xdotool` failing on a Wayland session hands over
  to `wtype`. Install one with the distribution's package manager.
- **Windows:** typing and pointer moves use PowerShell (`SendKeys`, `Cursor`),
  keys, clicks and drags use `SendInput`. They reach the desktop of the account
  the worker runs as.

Typed text is passed to the tool on stdin, so it never shows in a process list.

## Key names

`enter`, `escape`, `tab`, `backspace`, `space`, `delete`, `insert`, `home`,
`end`, `pageup`, `pagedown`, `up`, `down`, `left`, `right`, `f1`..`f12`, a
letter or a digit. With `xdotool` or `wtype` any other X keysym name works too
(`Print`, `XF86AudioPlay`).

## Not carried over from 0.4

- Termux's `input` command (it needs root); the Android app has its own
  `hid-android` plugin.
- The evdev/uinput backend (it needed the `evdev` Python package).
