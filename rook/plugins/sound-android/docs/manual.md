# Phone sound

| Cap | What it does |
|---|---|
| `sound.status` | Every stream's volume, the ringer, Do Not Disturb |
| `sound.set` | A stream's volume (`level` percent, `step` +n/-n, `mute`) and/or the `ringer` |
| `sound.dnd` | Do Not Disturb off, priority, alarms or total |

`stream` is media (the default), ring, notification, alarm, call or system.
Changes show no volume panel on the phone. Silencing the ringer and Do Not
Disturb need Rook's Do Not Disturb access (allow it in the app).
