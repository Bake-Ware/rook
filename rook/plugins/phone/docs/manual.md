# Phone

| Cap | What it does |
|---|---|
| `phone.find` | Ring at full alarm volume for up to 2 minutes, then restore the volume |
| `phone.find_stop` | Stop ringing early |
| `phone.unlock` | Type the PIN on the lock screen |
| `phone.vibrate` | Buzz for a moment |
| `phone.torch` | Flashlight on or off |
| `phone.switch` | Switch the app back to its Rook 0.4 worker |

`phone.unlock` enters the real PIN the way a finger would (through the app's
accessibility service): it wakes the screen, brings up the PIN pad and taps
the digits where the pad is on this screen size. It needs the accessibility
service and "Display over other apps" allowed in the Rook app. Without `pin`
it uses the PIN saved in the app (Settings > App > Device unlock PIN), which
stays on the phone; a `pin` passed in should be a vault reference
(`{{secret:phone-pin}}`). Replies never contain digits or tap positions.
Standard 4 and 6 digit PINs submit themselves; `unlocked` says whether the
lock screen went away.

Screen-driving caps (`ui.*`, `hid.*`, `screen.capture`, `app.launch`, ...) keep
the phone awake while an agent works and, when the app's keep-awake setting
allows it, unlock it with the saved PIN first (see `screen-android`).

`phone.switch {to: "0.4"}` hands the phone back to Rook 0.4: within about
30 seconds the app stops its 0.5 worker and starts the 0.4 one with the
settings it had (Settings > Band). The phone then leaves the 0.5 band, so
the reply is all this band hears; the app writes how it went to
`files/rook05/switch-result.json`. The way there is the same request file
(`files/rook05/switch.json`, `{"to": "0.5", "hub": "<hub address>",
"name": "<name>"}`) written by 0.4's `shell.exec`; see docs/castling-phone.md.
