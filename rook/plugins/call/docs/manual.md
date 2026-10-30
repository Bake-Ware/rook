# Calls

| Cap | What it does |
|---|---|
| `call.list` | Recent calls, newest first: number, name, direction, duration_s, ts |
| `call.dial` | Call a number now, as the phone's owner |

`call.dial` places the call (Android's ACTION_CALL): it speaks as a person
to someone else (`sends_as`), so the hub asks the person before every call.
To only open the dialer with a number, use `app.open_url` with
`tel:<number>`.

Permissions, on the phone: the call log (Rook > Settings > Permissions) for
`call.list`, and Phone (CALL_PHONE) for `call.dial`.
