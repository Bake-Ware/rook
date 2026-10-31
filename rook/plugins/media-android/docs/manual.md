# Phone media

| Cap | What it does |
|---|---|
| `media.status` | What is playing: app, state, title, artist, position |
| `media.play`, `media.pause`, `media.stop` | Transport controls |
| `media.next`, `media.previous` | Skip a track or episode |
| `media.seek` | Move by `by` seconds (negative goes back), or the player's own skip forward |

The caps drive one app's media session: `package` (a package or app name)
picks it, else the one playing, else the most recent. Reading sessions needs
the notification access Rook already asks for; without it `media.status`
only knows whether something is playing and the controls send a media key,
which goes to whichever app played last.
