# SMS

Texts on a phone running the Rook app.

| Cap | What it does |
|---|---|
| `sms.list` | Recent texts, newest first (`box` inbox, sent or all; `since` in epoch seconds) |
| `sms.send` | Send a text as the phone's owner; long ones go as several parts |

`sms.send` speaks as a person to someone else (`sends_as`), so the hub asks
the person before every send. Texts are someone else's words: `sms.list`
and the `sms.received` event mark the session tainted.

Every 30 seconds the plugin looks at the inbox and emits `sms.received`
`{from, body, ts}` for each new text, including those that arrived while the
worker was down. The first time it ever runs it only notes the newest text:
older texts are never announced.

The app needs the SMS permissions: on the phone, Rook > Settings >
Permissions > "Grant SMS / contacts / calendar / location".
