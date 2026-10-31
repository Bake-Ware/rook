# Phone screen

| Cap | What it does |
|---|---|
| `screen.on` | Turn the screen on over the lock screen (`dismiss_keyguard` brings up the PIN pad) |
| `screen.keep_on` | Keep the screen on for some minutes, or let it go |
| `screen.status` | Screen on, locked, keep-awake state, PIN saved, screen timeout |
| `screen.capture` | The screen as a JPEG |

`screen.on` needs "Display over other apps" allowed in the Rook app.
`screen.capture` uses Android's screen capture (MediaProjection): a person
grants it once in the app, and apps that block screenshots come out black
(`ui.screen` still reads them).

## Keep-awake

Caps that drive the screen (`ui.*`, `hid.*` except `hid.status`,
`screen.capture`, `app.launch` and the other caps that open an app) first
tell the app an agent is working: it turns the screen on and keeps it on
until it has been idle for the timeout, and when that starts on a locked
phone it unlocks with the PIN saved in the app (never retried after a failed
automatic try). This never fails the cap.

The app's Settings screen holds the two settings ("Keep phone awake while
agent is working", the idle minutes). Setting `keep_awake_auto` or
`keep_awake_idle_minutes` on this plugin hands the value to the app when the
plugin loads or the setting changes; blank leaves the app's value alone.
