# Adding machines

A machine joins your band by **signing in as you**. Enrollment makes your
hub the machine's **home hub** and you its owner, and gives it everything it
needs (the band, the hub's key, its id) in one go. Nothing on the machine
needs editing by hand.

## One tap on the Workers page (any machine)

On the new machine, from a checkout of the `castling` branch:

```sh
python -m rook.core2.worker --config worker.yaml enroll https://rook.example --name kaiju
```

It says `kaiju is waiting for someone to let this machine in on the Workers
page of hub 1a2b…` and waits. On the hub's **Workers** page the machine
shows under **Wants to join** with the name it gave, the address its request
came from (and what a proxy in front says) and how long it has waited. Tap
**Join** to let it in (you own it), or **No**. An agent does the same with
`hub.joins` and `hub.join {request}` (`refuse: true` says no).

A request lasts 10 minutes, and one its machine stops asking about goes
after 30 seconds. Then start the worker:

```sh
python -m rook.core2.worker --config worker.yaml
```

## With a one-time code: no tap (an agent, a script, the QR)

Anyone with a hub token, you or an agent working for you (Claude Code on
the new machine, say), gets a one-time code and gives its `qr` text to the
machine, which joins at once:

```sh
curl -s -H "Authorization: Bearer $TOKEN" -d '{}' https://rook.example/enroll/code   # or the cap hub.join_code
python -m rook.core2.worker --config worker.yaml enroll 'rook://enroll?url=…&code=…&hub=…'
```

The QR text also pins the hub's id, so the machine can't be talked into
joining a different hub. `--code <code>` with a plain address does the same
without the pin.

## A phone

In the Rook Android app, Settings > Band > Rook version: pick **0.5 preview**
(0.4 stays the default). Then either type the hub's address and tap
**Sign in**, and tap **Join** for the phone on the Workers page, as above. Or
scan the hub's QR text (`rook://enroll?…`) with the phone's camera: the app
opens, names the hub and asks before it signs in. Tap **Start worker** once
signed in.

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
- `<data>/worker.key`: the machine's own key. The worker signs what it asks
  of a hub with it (sub-calls, elevation), so nobody else on the band can
  ask for those in its name. It never leaves the machine; the
  hub recorded its public half when the machine joined. A
  worker that started before it had one makes it on its next start, and
  its hub takes it from the worker's first announce. Lost the file? Enroll
  again (`--force`): the hub refuses a different key for a machine it knows.
- `name` and `worker_id` in `worker.yaml`. The rest of the file is kept.

A machine that already has a home refuses to enroll again; `--force` makes a
new home hub, a new key, and drops every other band. Off this machine, enrollment needs
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
