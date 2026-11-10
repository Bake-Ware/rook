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

In the Rook Android app, Settings > Band: either type the hub's address and tap
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
new home hub and a new key. Off this machine, enrollment needs
`https://` (`--insecure` overrides it, for a trial on a trusted network).
