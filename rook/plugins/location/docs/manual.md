# Location

| Cap | What it does |
|---|---|
| `location.get` | Where the phone is: lat, lon, accuracy, how old the fix is, a Maps link |
| `location.navigate` | Start turn-by-turn directions in Maps (`mode` drive, walk, bike or transit) |
| `location.show` | Show a place on the map |

`location.get` asks Android for a fresh fix (GPS or network) for up to
`timeout` seconds and falls back to the last known one, marked `stale`. It
needs Location on and Rook's location permission set to "Allow all the
time" (the app's worker runs in the background).

`navigate` and `show` open Maps (or whichever app takes the link) and wake
the screen first.
