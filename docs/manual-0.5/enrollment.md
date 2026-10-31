# Adding machines

A machine joins your band by **signing in as you**. Enrollment makes your
hub the machine's **home hub** and you its owner, and gives it everything it
needs (the band, the hub's key, its id) in one go. Nothing on the machine
needs editing by hand.

Owners and adults may enroll machines; children and guests may not.

## With a code you approve (any machine)

On the new machine, from a checkout of the `castling` branch:

```sh
python -m rook.core2.worker --config worker.yaml enroll https://rook.example --name kaiju
```

It prints something like `To add kaiju, approve code BCDF-GHJK on hub 1a2b…`
and waits. Approve the code from a signed-in session (the dashboard has no
button for it yet, so use the HTTP door with your token):

```sh
H="Authorization: Bearer $TOKEN"
curl -s -H "$H" https://rook.example/enroll/pending                       # who is waiting
curl -s -H "$H" -d '{"code":"BCDF-GHJK"}' https://rook.example/enroll/approve
curl -s -H "$H" -d '{"code":"BCDF-GHJK","decision":"refuse"}' https://rook.example/enroll/approve
```

Codes last 10 minutes. Then start the worker:

```sh
python -m rook.core2.worker --config worker.yaml
```

## With a one-time code (the QR)

Make a one-time code while signed in, and give its `qr` text to the machine:

```sh
curl -s -H "$H" -d '{}' https://rook.example/enroll/code
python -m rook.core2.worker --config worker.yaml enroll 'rook://enroll?url=…&code=…&hub=…'
```

The QR text also pins the hub's id, so the machine can't be talked into
joining a different hub. `--code <code>` with a plain address does the same
without the pin.

## A phone

In the Rook Android app, Settings > Band > Rook version: pick **0.5 preview**
(0.4 stays the default). Then either type the hub's address and tap
**Sign in**: the phone shows a code to approve, as above. Or scan the hub's
QR text (`rook://enroll?…`) with the phone's camera: the app opens, names the
hub and asks before it signs in. Tap **Start worker** once signed in.

## Finding the hub on the local network

A hub whose config has `enroll: {discover: true}` answers local network
discovery (mDNS `_rook-hub._tcp`):

```sh
python -m rook.core2.worker --config worker.yaml enroll --discover
```

## What enrollment writes

- `anchor.json`: the home hub's id, key and band. It is what the worker
  trusts. On a service install keep it owned by root (or Administrator) so
  the worker can't change it; the worker warns when it can.
- `<data>/bands.json`: the home band.
- `name` and `worker_id` in `worker.yaml`. The rest of the file is kept.

A machine that already has a home refuses to enroll again; `--force` makes a
new home hub and drops every other band. Off this machine, enrollment needs
`https://` (`--insecure` overrides it, for a trial on a trusted network).

## Joining another band

A worker can serve more than one band, for example a friend's hub that may
use your living-room TV. Other hubs **invite**; they never pull. All changes
happen on the worker's home hub, never on the machine.

1. The other hub's owner makes an invite (it carries that band's key, so
   treat it like a password):
   `POST /bands/invite {"role": "guest", "ceiling": {"grants": ["tv.*"]}}`.
   Roles are `guest` (limited by a ceiling) or `co-owner` (may also manage
   the machine: plugins, settings).
2. You accept it on your hub for one worker, with a ceiling of your own:
   `POST /memberships {"worker": "kaiju", "invite": {…}, "ceiling": {"grants": ["tv.*"]}, "why": "Lend the TV to Sam's band."}`.
3. To take the worker off that band again:
   `POST /memberships {"worker": "kaiju", "band": "<band id>", "role": "removed", "why": "…"}`.

The **ceiling** is the most that hub can ever do on this worker, whatever
its own grants say ([Workers and appliances](workers.md#ceilings)). The
other hub sees only the caps its ceiling covers. `{"worker", "role": "home",
"ceiling"}` narrows what your own hub may do there.

If the home hub is lost, the worker keeps serving its other bands within
their ceilings; enrolling it again (`--force`) makes a new home.
