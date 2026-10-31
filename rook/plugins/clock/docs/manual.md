# Clock

| Cap | What it does |
|---|---|
| `clock.timer` | Start a countdown timer (`seconds`, optional `label`) |
| `clock.alarm` | Set an alarm at `hour`:`minute`, phone-local, 24-hour |
| `clock.alarms` | Open the list of alarms |

They go through the phone's clock app, which keeps and rings them; Rook
doesn't. With `start: false` the clock app opens with the timer or alarm
filled in and a person confirms it. The screen is woken first.
