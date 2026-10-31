# Workers and appliances

A **worker** is Rook running on one machine. It connects to its bands,
announces what it can do (one manifest per plugin it runs), sends a
heartbeat every 30 seconds, and runs calls that carry a valid ticket from a
hub it trusts. The hub lists every worker it knows, online or not: it
remembers a worker's caps while the machine is off.

## A worker's config

`worker.yaml` (enrollment fills in `anchor`, `name` and `worker_id`):

```yaml
data: ./worker-data            # replay cache, memberships, installed plugins
anchor: ./anchor.json          # the home hub: written at enrollment
name: kaiju
description: GPU box in the rack (what this machine is for, for people and agents)
plugins: [../rook/plugins/shell, ../rook/plugins/file]   # plugin folders loaded at start
settings: { torrent: { console: /usr/bin/deluge-console } }
hardware: [gpu, display]       # what this machine has, for plugins that need it
```

Plugins listed under `plugins:` are trusted because the machine's owner put
them there. Plugins the hub pushes are signed for this worker and installed
under `<data>/plugins` ([Plugins](plugins.md)). The full key list is in the
[Reference](reference.md#workeryaml).

## What a worker can't serve

A cap whose plugin needs something the machine lacks (a program, hardware,
a setting) is announced as **unavailable**, with the reason, for example
`missing bin: deluge-console`. Calling it says why instead of failing
strangely. `GET /workers` and the dashboard's Workers page show these.

## Normal user, elevation on request

Every call runs as the worker's normal account: no `sudo`, no "run as
administrator". Shell stays fully open within that account. A cap that
needs administrator rights declares `elevation: upfront` or `may` in its
manifest; the worker then asks the hub, inside the same call, for a
15-minute **lease** for that person, agent and session. The hub journals
it, and the call runs elevated (on Linux through `sudo -n`, so the worker's
account needs a sudoers rule you add yourself). No answer from the hub in
10 seconds and the call fails: elevation fails closed. Leases end when
rights change, when the worker loses the hub, or when it restarts. A worker
running as root refuses to run normal calls as root. Elevation on Windows
and macOS is not built yet.

## Ceilings

A **ceiling** is the most a hub may ever do on a worker, kept on the
worker and signed by its home hub. Your home hub's ceiling is "everything"
until you narrow it; other bands get only the ceiling you set when you let
them in ([Adding machines](enrollment.md#joining-another-band)). A ceiling
is a rights object:

```json
{"grants": ["tv.*", "light.*@downstairs-*"], "max_risk": "write", "effects": ["physical"]}
```

The hub refuses early when a call is outside a worker's announced ceiling,
and the worker refuses it again on its own, whatever a ticket says.

## Sacred machines

List machines that must never change without you in the hub's config:

```yaml
sacred: [nas, router]
```

On a sacred machine, every call that isn't read-only is a **hard stop**:
it waits for you, and only you can release it ([Holds](holds.md)).

## Appliances

An **appliance** runs no worker: a TV, a PiKVM, a phone over adb, a
Pico-CEC bridge. Its plugin has an `appliance:` section (how to find it,
its per-device settings), and a worker that can reach the device carries
its calls; the call names the device (`appliance`), and the ticket binds
both. The plugins for these ship (`kvm`, `tv`, `adb`, `cec`), but the hub
can't onboard an appliance yet: it has no registry of devices and their
routes, so appliance caps can't be called end to end on 0.5 today.

## The hub's own node

The hub appears as a worker named `rook`. Its caps (`hub.workers`,
`journal.search`, `approval.list`, the workbench, agents, ...) are called
like any other: `worker=rook`.
