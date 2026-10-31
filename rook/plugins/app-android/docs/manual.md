# Phone apps

| Cap | What it does |
|---|---|
| `app.list` | Apps on the launcher, by name |
| `app.launch` | Open an app by package or launcher name |
| `app.open_url` | Open a link or URI in the app that takes it |
| `app.share` | The share sheet with some text; a person picks the target |
| `app.settings` | A Settings page (wifi, bluetooth, dnd, an app's details, ...) |

`app.open_url` covers what used to be separate intents: `tel:` opens the
dialer with a number (no call; `call.dial` places one), `mailto:` and
`smsto:` open drafts (nothing is sent), `geo:` a map, and an app's own deep
links. `screen: true` on `app.launch` and `app.open_url` waits for the app to
come up and returns the screen (see `ui.screen`).

Android 10 and later may silently refuse to open an app from the background.
Rook is exempt while "Display over other apps" or its accessibility service
is allowed; without either, the reply carries `may_be_blocked`.
