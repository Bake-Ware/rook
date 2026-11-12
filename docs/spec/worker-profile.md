# The minimal worker profile

What a worker must do to be a full member of a Rook 0.5 band: **join,
announce its caps, answer calls, heartbeat.** Nothing else is required. It
is written from what the hub accepts today; firmware, the single-file
Python script, the Rust worker and the phone app all speak it. A native
module talking to its local worker speaks these same messages.

The example implementation is `tests/test_core2_minimal_worker.py` (about
70 lines of worker), run against a real hub. `worker/rook_worker.py` is
the profile with the carrier written out too (standard library Python, no
Telesthete library), a worker people run. The ESP32-S3 dongle's firmware
speaks it in C++ (`firmware/src/profile.cpp`; `firmware/DEVLOG.md`, "Joins a
0.5 band").

The key words MUST, MAY and SHOULD are as in RFC 2119.

## 1. Join

Use the Telesthete carrier exactly as [core v2 section 2.1](core-v2.md#21-telesthete-profile)
says: band id and key from the PSK, ChaCha20-Poly1305 CHANNEL frames with
empty associated data, the 21-byte fragmentation envelope, the relay over
UDP (keepalive at least every 20 s) or the hub's `/band` WebSocket bridge.

Every message is one JSON object, UTF-8, sent as one carrier payload to
everyone on the band (route 0). Ignore every payload that is not a JSON
object, and every object you don't understand.

A worker has a `worker_id` (32 lowercase hex, the same after every restart)
and a `name` (unique on the band, not `rook`).

## 2. Announce your caps

Send the announce when you join and again after every reconnect:

```json
{"kind": "announce", "worker_id": "3f0a9c1d2e3f40516273849506a7b8c9", "name": "porch-lamp",
 "core": "2.0", "manifests": [{"plugin": "lamp", "version": "1.0.0", "hash": "9c1e…"}],
 "manifests_hash": "e3b0…"}
```

| Field | What |
|---|---|
| `core` | MUST be `"2.0"` (any `2.x` works) |
| `manifests` | one entry per plugin you run: `plugin`, `version`, and `hash` = hex SHA-256 of the manifest's canonical JSON |
| `manifests_hash` | hex SHA-256 of the canonical JSON of the `manifests` list |

Canonical JSON is RFC 8785. For a manifest made only of objects, lists,
strings, integers and booleans that is: keys sorted, no spaces. A device
with fixed manifests computes both hashes once, at build time.

Your caps are the caps of your manifests; there is no separate cap list. A
manifest is a plugin manifest (`conformance/manifest/manifest.schema.json`)
with `builtin: true`, whose caps carry no `run` or `action`:

```json
{"manifest": 1, "plugin": "lamp", "version": "1.0.0", "title": "Lamp", "doc": "The porch lamp.",
 "builtin": true, "caps": {"set": {"doc": "Turn it on or off.", "risk": "write", "scope": "self",
                                   "args": {"on": {"type": "bool", "required": true}}}}}
```

The hub learns a manifest in one of two ways:

- **It already has it.** The plugin is in the hub's library (its
  `plugin.yaml`), or another worker sent it before. Then you never send it.
  A device too small to send its manifests MUST announce only these.
- **It asks you.** For an entry it doesn't have, the hub calls
  `core.manifests` on you (a call, section 3) with `args` `{"plugins":
  ["lamp"]}` (or `{}` for all). Answer `{"manifests": [<manifest>, …]}`,
  each exactly the object you hashed.

## 3. Answer calls

A call is a JSON object with a `cap`:

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "cap": "lamp.set", "target": "3f0a9c1d2e3f40516273849506a7b8c9",
 "args": {"on": true}, "ticket": {…}, "accept": ["drop", "ack"], "rr": 4711}
```

- Read `target`: if it is not your `worker_id`, stay silent.
- Read `id`, `cap` and `args` (absent means `{}`). The hub does not check
  `args` against your manifest: check what you read.
- You MAY ignore everything else: `ticket`, `accept`, `rr`, `carry`,
  `appliance`.

Reply once, to everyone:

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "from": "3f0a9c1d2e3f40516273849506a7b8c9", "ok": true, "result": {"on": true}}
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "from": "3f0a9c1d2e3f40516273849506a7b8c9", "ok": false, "error": "relay stuck"}
```

`id`, `from` and `ok` MUST be there; `result` (any JSON, `null` if none)
when `ok`, `error` (a string) when not. Add `"retry": true` when trying
again could work. A cap you don't serve, `core.manifests` included when
you never send manifests, gets `"error": "unknown capability: <cap>"`.

The hub waits for the cap's `timeout` (manifest), else 15 s, plus 5 s, then
tells the caller the call may still be running. It doesn't resend calls to
a worker that announced without the `ack` feature.

## 4. Heartbeat

Every 30 s (±20 %):

```json
{"kind": "heartbeat", "worker_id": "3f0a9c1d2e3f40516273849506a7b8c9", "core": "2.0", "manifests_hash": "e3b0…"}
```

The hub shows a worker offline after 90 s without an announce or a
heartbeat. When your manifests change, send a new announce. A heartbeat
whose `manifests_hash` the hub doesn't hold makes it call `core.manifests`
for the whole list; a hub that never saw your announce lists you under
your `worker_id` until the next one.

## 5. Everything else is optional

A worker MAY do any of these; the hub requires none of them.

| What | core v2 |
|---|---|
| More announce fields: `version`, `description`, `facts`, `unavailable`, `status` | 4.2 |
| Checking tickets and the replay cache | 10.1 |
| A worker key (`key` in the announce) and signed sub-calls (`core.call`) | 10.2 |
| Verifying the signed hub announce, hub contact and hub loss | 10.3 |
| Events (`core.event`) | 10.4 |
| Streams (terminals, processes): caps that answer `{"stream": id}` | 10.5 |
| Transport extensions: `route`, `drop`, `z`, `ack` (listed in `features`) | 10.6 |
| Pushed plugins and self-update (`plugin.sig`, `release.sig`) | 10.7 |
| Appliances behind a delegate (`appliance`, `carry`) | 10.8 |
| Parked: elevation and leases; ceilings, memberships and several bands | 10.9, 10.10 |

**Events, the minimum.** Declare the event under `emits` in your manifest.
Take the hub's node id from the `worker_id` of any announce with `"name":
"rook"` (no need to verify it), and send it a call:
`{"id": "<32 hex>", "cap": "core.event", "target": "<hub node id>", "args":
{"worker_id": "<yours>", "events": [{"id": "<32 hex>", "event": "lamp.pressed",
"plugin": "lamp", "payload": {…}}]}}`. The hub answers `{"taken": n}`.
