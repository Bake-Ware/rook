# Phone

| Cap | What it does |
|---|---|
| `phone.find` | Ring at full alarm volume for up to 2 minutes, then restore the volume |
| `phone.find_stop` | Stop ringing early |
| `phone.unlock` | Type the PIN on the lock screen |
| `phone.vibrate` | Buzz for a moment |
| `phone.torch` | Flashlight on or off |

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
