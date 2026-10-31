# Phone touch and keys

Taps, swipes, typing and the back/home/recents keys on a phone running the
Rook app, without root: the app's accessibility service performs them.

| Cap | What it does |
|---|---|
| `hid.status` | Whether the accessibility service is on |
| `hid.type` | Type into the focused field (`enter` presses the keyboard's enter after) |
| `hid.click` | Tap at a pixel (`ui.screen` gives every item's centre) |
| `hid.drag` | Swipe |
| `hid.key` | back, home or recents |

`ui.click` and `ui.type` find things by their label instead of a pixel and
are usually the better choice. Gestures need Android 7 or newer.

The accessibility service must be on: Rook > Settings > Permissions.
