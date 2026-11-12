# Rook core specification, version 2.0

Status: **2.0 draft** for Castling (Rook 0.5). A clean cut from
[core v1](core-v1.md): no v1 peer interoperates with a v2 peer.

Sections 2 to 9 are what every hub and worker must do. For a worker
that is exactly the [minimal worker profile](worker-profile.md): join,
announce its caps, answer calls, heartbeat. Section 10 is the
optional extensions: each says what it is, and none is required of a worker.
The Python reference (`rook/core2`) implements all of it; the hub is
`rook/core2/hub`, the workers are `worker/` (Rust, the profile) and
`rook/core2/worker` (Python, the profile plus every extension).

Companion material:

- `conformance/core-v2/vectors/*.json`: test vectors for the pure functions.
  Where prose and vectors disagree, the vectors win.
- `conformance/manifest/manifest.schema.json`: the plugin manifest. This
  document only says how manifests travel and which enums they use.
- `rook/streams/README.md`: streams.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are as in RFC 2119
and RFC 8174 when they appear in capitals.

---

## 1. Overview

*This section is informative.*

A **band** is a set of peers sharing one pre-shared key (PSK) over a
Telesthete relay. A **hub** is the band's control plane: people and agents
come in through its doors (MCP, the site, the phone, jobs, chat bridges),
and every door ends at one **dispatch** step that decides and journals. A
**worker** runs on a machine, announces the caps its manifests declare, and
answers the calls the hub sends it.

```
 agents, people ──MCP/HTTPS──▶ hub (dispatch, journal) ──▶ relay ──▶ workers ──▶ appliances
```

## 2. Transport and versions

### 2.1 Telesthete profile

The transport is **Telesthete v0.3.0** (<https://github.com/Bake-Ware/telesthete>,
wire 1.2, `PROTOCOL_VERSION = 3`), pinned. The Rook profile of it is core v1's,
incorporated by reference, with v1's vectors (`band_crypto.json`,
`fragments.json`, `frames.json`) still normative:

| Core v1 section | What |
|---|---|
| 2.1 Keys | `band_id = SHA-256(psk)[0:16]`, HKDF key |
| 2.2 Frames | CHANNEL frames, ChaCha20-Poly1305, **empty AAD** |
| 2.3 Sequence numbers | CSPRNG start, +1 per frame, one counter per process |
| 2.4 Fragmentation | 21-byte envelope, ≤ 1003 data bytes per chunk |
| 2.5 The relay | keepalive `0x00` every ≤ 20 s, idle eviction |
| 2.6 The WebSocket bridge | one frame per WebSocket message |

Every message is sent to everyone on the band (route 0) unless a transport
extension (10.6) says otherwise.

### 2.2 Versions

Core versions are `MAJOR.MINOR` (`"2.0"`). Every announce (worker's and
hub's) and every heartbeat carries `core`. Each side computes
`negotiate(mine, theirs)`: equal majors give the same major with the lower
minor; anything else (another major, a malformed `core`) gives none. The
hub sends no calls to a worker with no version in common, and lists it with
the reason (`incompatible: core …`). Vectors: `version.json`.

### 2.3 Compatibility rules

1. **Unknown fields are ignored**, in every message, object and result.
2. **Anything added in a minor version is optional**, and its absence means
   "behave as before".
3. Existing fields never change meaning or type; normative error strings
   never change; enum values are never removed. New enum values come with a
   fallback for peers that don't know them (3.4 to 3.6).
4. Anything that breaks these rules is v3.

## 3. Messages and caps

### 3.1 Message kinds

Every message is one UTF-8 JSON object, one carrier payload. A payload that
is not a JSON object (a binary sub-protocol such as Drop) is ignored by peers
that don't speak it; the keepalive `0x00` is discarded.

| Kind | Recognised by | Section |
|---|---|---|
| request | `cap` present (a non-empty string) | 5.1 |
| reply | no `cap`; `id`, `from` and `ok` present | 5.2 |
| announce | no `cap`; `kind == "announce"` | 4.2, 4.4 |
| heartbeat | no `cap`; `kind == "heartbeat"` | 4.3 |

Anything else MUST be ignored. New kinds carry `kind` and never `cap`.

### 3.2 Canonical JSON

`canonical(v)` is the **JSON Canonicalization Scheme, RFC 8785**, as UTF-8:
members sorted by name as UTF-16 code units, no whitespace, only the escapes
RFC 8785 requires, numbers as ECMAScript prints them (`-0` as `0`). NaN,
infinities, unpaired surrogates, non-string keys and integers outside
±(2^53 − 1) have no canonical form, and an operation that needs one fails.
For a value made only of objects, lists, strings, integers and booleans
this is: keys sorted, no spaces.

`args_hash(args)` = base64url(SHA-256(canonical(args))), unpadded, where a
missing or `null` `args` hashes as `{}`. Manifest hashes (4.2) and journal
hashes (7.3) use `canonical` too. Vectors: `canonical.json`.

### 3.3 Cap names and contracts

A cap name is `<plugin>.<suffix>`: the owning plugin's name
(`[a-z][a-z0-9-]*`, unique per hub), then a suffix that may hold more dots
(`deluge.torrents.list`). Every cap is declared in its plugin's manifest with
`doc`, `risk`, `scope`, `effects` and argument and result schemas, so the hub
answers "what does this cap do" with the worker offline. **The enums below
belong to this document**; the manifest schema uses exactly these values.

A cap MAY provide a **contract**, a shared interface named once (such as
`screen.capture` or `notify.send`). A call by a contract's name goes to the
one cap on the target that provides it (more than one: the call is refused
and names them); the call keeps the contract's name beside the cap, and
patterns (6.2) match either.

### 3.4 Risk

Required on every cap, lowest first: `read` (changes nothing), `write`
(changes state through the cap's narrow purpose), `exec` (runs arbitrary
code or commands), `admin` (changes Rook administration, security or system
configuration). An unknown risk counts as `admin`.

### 3.5 Effects

Flags a cap lists when they apply; they are what the hard stops (6.4) read.

| Flag | Means |
|---|---|
| `destructive` | erases or overwrites data for good |
| `sends_as` | sends as a person to someone else |
| `sensitive` | reveals secrets or private content |
| `physical` | acts on the physical world (power, locks, motors, speakers) |

An unknown flag counts as `destructive` for holds. The hub never retries a
`destructive` or `physical` call on its own (5.3).

### 3.6 Scope

Required on every cap; how far the effect reaches, shown to people: `self`
(Rook's own state, or the caller's own data), `local` (this machine or
appliance), `multi-machine`, `multi-user` (other people's data or
accounts), `external` (outside the band). An unknown scope counts as
`external`.

## 4. Workers and presence

### 4.1 Node identities

| Id | What | Form |
|---|---|---|
| `worker_id` | a worker; kept across restarts | 32 lowercase hex |
| `hub_id` | the hub: the `key_id` (10.1) of its root key | 16 hex |
| `band` | the band's `band_id` | 32 hex |

The hub appears on its band as a node named `rook`, with a `worker_id` of
its own (the **hub node**). No worker may use the name `rook`.

### 4.2 Worker announce

A worker announces when it joins, after every reconnect, and whenever its
manifests change. What it MUST send is in the profile:
`kind`, `worker_id`, `name`, `core`, `manifests` and `manifests_hash`.

| Field | Meaning |
|---|---|
| `manifests` | one entry per plugin it runs: `{plugin, version, hash, builtin?}`, `hash` = hex SHA-256 of `canonical(manifest)` |
| `manifests_hash` | hex SHA-256 of `canonical(manifests)` |
| `version` | MAY: worker software build |
| `description` | MAY: what the machine is for, ≤ 280 characters; data, not instructions |
| `facts` | MAY: self-reported platform facts (`os`, `arch`, …) as core v1 section 5.1; they grant nothing |
| `unavailable` | MAY: `{cap: reason}` for caps it can't serve now |
| `status` | MAY: small live status per plugin namespace |
| `features` | MAY: transport extensions it speaks (10.6) |
| `key` | MAY: the worker's public key (10.2) |
| `role`, `ceiling` | MAY: parked (10.10) |

A worker's caps are the caps of its manifests; there is no separate list.
The hub resolves each entry from its plugin library, or from a manifest it
learned before, by `plugin`, `version` and `hash`; for an entry it lacks it
calls `core.manifests` (9) on the worker and validates what comes back. A
device too small to send its manifests announces only entries the hub
already has. A hub keeps a worker's caps known while it is offline and
across restarts.

The hub ignores an announce that is malformed, names itself `rook`, or
whose `manifests_hash` doesn't match its `manifests`.

### 4.3 Heartbeat and liveness

A worker sends `{"kind": "heartbeat", "worker_id", "core", "manifests_hash",
"status"?}` every 30 s (±20 %). A heartbeat whose `manifests_hash` the hub
doesn't hold makes the hub call `core.manifests` for the whole list (once
per new hash); a heartbeat from a worker it has no announce for lists the
worker under its `worker_id` until the next announce.

The hub shows a worker offline after 90 s without an announce or a
heartbeat. Stored status is a routing hint only: the hub never refuses a
call because it believes the target is offline; it tries the real call and
reports the real failure.

### 4.4 Hub announce

The hub node announces on its band every 30 s (±20 %), and within a second
of a worker's announce:

```json
{"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": "<hub node id>",
 "name": "rook", "hub": "<hub_id>", "band": "<band>", "core": "2.0",
 "ts": 1793300000, "seq": 41, "features": ["drop", "route", "ack"],
 "kid": "<op key id>", "sig": "<b64>", "hubkey": {…}}
```

It is signed (10.3). A worker learns the hub node's id from any announce
named `rook`; checking the signature is optional (10.3). The hub takes no
announce with `typ: rook-announce` as a worker.

## 5. The call

### 5.1 Request

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "cap": "shell.exec",
 "target": "3f0a9c1d2e3f40516273849506a7b8c9", "args": {"argv": ["uptime"]},
 "ticket": {"typ": "rook-ticket", "v": 2, "…": "…"}}
```

| Field | Meaning |
|---|---|
| `id` | 32 hex, chosen by the hub, unique per hub; echoed in the reply |
| `cap` | the cap name, contract already resolved |
| `target` | the `worker_id` that answers. There are no open calls: every other peer stays silent. |
| `args` | a JSON object, secrets already substituted; absent means `{}` |
| `ticket` | the hub signs one for every call (10.1); a worker MAY ignore it |
| `appliance`, `carry` | on a call through a delegate (10.8) |
| `accept`, `rr` | transport extensions (10.6) |

The hub does not check `args` against the cap's schema; the worker checks
what it reads. A cap a hub plugin declares to run `on: worker` runs on the
hub for that worker (through its primitives), and never goes over the band.

### 5.2 Reply

```json
{"id": "5d1c…", "from": "3f0a…", "ok": true, "result": {"stdout": "up 3 days"}}
{"id": "5d1c…", "from": "3f0a…", "ok": false, "error": "relay stuck", "retry": true}
```

`id`, `from` (the replier's `worker_id`) and `ok` MUST be there; `result`
(any JSON, `null` if none) when `ok`, `error` (a string) when not. `retry:
true` says trying again could work. Normative errors: `unknown capability:
<cap>`, `bad args: <detail>`. The hub takes a reply only from the call's
target, and redacts every secret value it substituted from the result and
the error before the caller sees them. Large replies MAY go over Drop
(10.6).

### 5.3 Timeouts and retries

The hub waits for the cap's declared `timeout`, else 15 s, plus 5 s. A cap
with a `timeout` argument (seconds) is waited on at least as long as the
call asks (else that argument's default), up to its `max` and at most an
hour. Then the caller hears `timeout: the call may still be running`; a
late reply is journaled. There is no cancellation.

The hub retries a call once only when the cap is declared `idempotent: true`
and has no `destructive` or `physical` effect. A worker that refuses a
ticket as `expired` or `not_yet_valid` (10.1) gets the same call once more
with a fresh ticket.

## 6. Dispatch on the hub

### 6.1 Order

Every door ends at dispatch. In order:

1. **Door.** A door an owner closed (the kill switch) refuses:
   `denied: door <name> closed`.
2. **Caller.** The door's caller as `person` and/or `agent` (6.2).
3. **Session** (6.3): known, not ended, and the caller's.
4. **Target and cap.** The worker by id or name, or an appliance by name
   (10.8); the cap from the target's manifests (`unknown worker: …`,
   `unknown capability: …`).
5. **Why.** Every call carries a reason in one plain sentence, ≤ 500
   characters: `denied: why required`.
6. **Rights** (6.2): `denied: <layer>: <reason>`.
7. **Holds** (6.4), only when the hub's `hub.security` setting is `on`.
8. **Send** with a fresh ticket, and wait (5).
9. **Journal** (7): one entry per call, whatever the outcome.

With `hub.security` `off` (the default until the hardening pass) dispatch
goes from rights straight to the send; the entry says `security: off` and
lists any hard stop the call would have waited on (`would_hold`).

### 6.2 People, agents and rights

Identities are `person:<name>` (a person with an account), `agent:<name>`
(a token, a persona, a harness) and `system:<component>` (the hub itself,
for core caps). An agent working for a person carries both; one working
alone carries only its own.

A **rights object** is `{"grants": [pattern, …], "max_risk"?, "effects"?}`:
a call must match a grant, be at most `max_risk` (absent: no limit), and
have only effects in `effects` (absent: all; `[]`: none). A pattern is
`cap-glob` or `cap-glob@target-glob`; `*` matches any run of characters,
dots included; the target is the appliance, else the worker's name. A call
by a contract's name matches by the contract's name or the cap's.

A call is allowed only when **every layer that applies permits it**: the
person's, the agent's, the session's declared caps, and any extra layer the
door adds (a page's caps, a calling plugin's `uses`). A person's rights are
their preset plus grants: `owner` (`["*"]`), `adult` (`["*"]`, up to
`admin`), `child` (`["page.view"]`, up to `write`, no effects) and `guest`
(`[]`, up to `write`, no effects, expires 7 days after the account is
made); plugins may add grants inside their own namespace to a preset, and
owners may give a person more, but never past the preset's `max_risk` and
`effects`. Vectors: `rights.json`, `presets.json`.

### 6.3 Sessions

Every call belongs to a session, hub state with `id`, `person`, `agent`,
`caps` (patterns declared at the start; the session layer), `project` (the
journal chain its entries go to, 7.3), `task`, and an expiry (12 h).
`["*"]` is a valid declaration. A session that needs more starts a new one.

### 6.4 Holds and hard stops

With security on, a call that meets a **hard stop** is held: the caller
gets `{"ok": false, "held": true, "hold": "<name>", "error": "held: …"}`, and
only the person the call is for (an owner when it is for no one), from a
door where they are present, releases it, which runs that exact call with a
fresh ticket; a refusal is final. The hard stops: a change (not `read`) on a
machine the hub config lists as `sacred`; `destructive` effects (unknown
effects count); a `sensitive` cap whose result declares a secret; and
`sends_as`.

## 7. Journal

### 7.1 Who writes

Only the hub writes the journal: one entry per dispatched call, plus
enrollments, worker key pins, holds and their outcomes, late replies and
session starts and ends. Agents never write it.

### 7.2 Call entry

| Field | Meaning |
|---|---|
| `id`, `kind`, `chain`, `prev`, `hash` | entry id, `call`, and its chain links (7.3) |
| `ts`, `call` | dispatch time, call id |
| `hub`, `band`, `worker`, `appliance`, `cap` | where it went |
| `person`, `agent`, `session`, `project` | who, and the session's project |
| `why` | the caller's reason |
| `args`, `args_hash` | the arguments with secret values redacted, and their hash |
| `ticket` | the nonces of the tickets minted for it |
| `risk`, `scope`, `effects` | from the manifest |
| `decision` | `allow`, `deny`, `held`, `released`, `refused`; `layer` says who denied |
| `security`, `would_hold` | with security off (6.1) |
| `ok`, `error`, `ms` | the outcome and how long it took |

### 7.3 Hash chains

One table, a chain per project and a `general` chain for sessions without
one. `prev` is the previous entry's `hash` in its chain (64 zeros for the
first); `hash` = hex SHA-256 of `canonical(entry without hash)`. Rows are
write-once in the database; the hub keeps every chain's head and re-checks
hourly. Vectors: `journal.json`.

### 7.4 Retention and reading

Project chains are kept. The `general` chain is pruned by age, a day at a
time, and the prune is itself journaled. Owners read everything; an agent
reads its own calls and its sessions' projects; a person reads their own.

## 8. Enrollment

A machine joins a hub's band for a person, who then owns it. Enrollment runs
over the hub's HTTPS door, never over the band, in one of two ways that end
in the same config bundle:

- **One tap.** The machine asks to join (`POST /enroll/ask {name, key?}`, no
  token) and polls (`POST /enroll/poll {ticket}`) while it shows on the
  Workers page as wanting to join; a person taps Join or No there
  (`hub.joins`, `hub.join`).
- **A join code.** Someone with a hub token, a person or an agent for one,
  makes a one-time code (`POST /enroll/code`, or the `hub.join_code` cap;
  ten minutes, one use) whose QR text is `rook://enroll?url=…&code=…&hub=…`.
  The machine claims it with no token (`POST /enroll/claim {code, name,
  key?}`). An agent already on the machine can join it with no tap this way.

The machine finds the hub from the QR, by local network discovery (mDNS
`_rook-hub._tcp`), or from a typed address. The bundle, given once:

| Field | Meaning |
|---|---|
| `hub` | `{hub_id, root, url}` |
| `band` | `{id, psk, relay?, ws_url?}` |
| `worker_id`, `name` | the id the hub recorded for the machine, and its name |
| `owner` | the person it belongs to |
| `services` | service endpoints the machine needs (a voice server), with their keys |
| `core` | the hub's core version |

When the machine sends its public `key`, the hub records it with the
`worker_id` it issues (10.2). Secrets in the bundle are the machine's own;
every other secret stays in the hub's vault and travels with a call.

## 9. Core caps

Every worker that sends manifests answers `core.manifests` (`{"plugins":
[name, …]}`, or `{}` for all) with `{"manifests": [<manifest>, …]}`, each
exactly the object it hashed (4.2). A worker that never sends manifests answers it `unknown
capability`. The hub calls it as `agent: system:hub`.

The hub node serves three caps to its band's workers, the only requests
from the band it answers: `core.event` (10.4), `core.call` (10.2) and
`core.elevate` (10.9).

## 10. Optional extensions

None of these is required of a worker. Where the hub does its part on every
call (it signs every ticket and every announce), that is said.

### 10.1 Tickets and signatures

**Keys.** Keys are Ed25519, written `ed25519:<standard base64 of the 32
bytes>`; `key_id(pub)` is the first 16 lowercase hex of SHA-256 over the 32
bytes. A **signed object** carries `typ`, `v` (`2`), `kid` (the signing
key's `key_id`) and `sig` (standard base64 of the Ed25519 signature over
`PREFIX(typ) ‖ canonical(object without sig and hubkey)`). The prefix is
`<typ>-v2\n`, for the types `rook-hubkey`, `rook-announce`, `rook-ticket`,
`rook-plugin`, `rook-release`, `rook-request`, and the parked `rook-lease`
and `rook-membership`. A verifier accepts an object only under its own
`typ`'s prefix. Vectors: `signatures.json`.

**Ticket.** The hub signs one ticket per call with its operational key
(10.3) and puts it on the request: `hub`, `band`, `call` (the request `id`),
`nonce` (32 hex, fresh per ticket), `worker`, `cap`, `args_hash` (of the
request's `args`), `carry_hash` (when the request has `carry`),
`appliance` (when it has one), `person` and/or `agent`, `owner: true` when
the person is an owner of the hub, `session`, `why`, `contract` (when called
by one), `elevate` (asked up front, 10.9), `iat` and `exp` (60 s later),
plus `hubkey` (unsigned, 10.3).

**Worker checks.** A worker that checks tickets refuses on the first
failure, with `denied by worker: <reason>`: `no_ticket`, `malformed`,
`unknown_hub` (not a hub it trusts for that band), `unknown_key`,
`bad_signature`, `wrong_band`, `wrong_worker`, `wrong_appliance`,
`wrong_call`, `wrong_cap`, `wrong_args`, `bad_lifetime` (`exp − iat` outside
60 to 120), `not_yet_valid` (`now < iat − 30`), `expired` (`now > exp +
30`), `replay`, `unknown_cap` (replied as `unknown capability: <cap>`),
`outside_ceiling` (10.10). It keeps every accepted nonce until at least
`exp + 30` (the Python worker: 15 minutes, across restarts), and takes the
call's `person`, `agent`, `owner`, `session` and `why` only from a verified
ticket. Vectors: `tickets.json`.

### 10.2 Worker keys and signed requests

A worker MAY have its own Ed25519 key, made on the machine and never
leaving it, and send its public half as `key` at enrollment (8) and in its
announce. The hub pins one key per `worker_id`: from enrollment, else from
the first announce that carries one (journaled as `worker_key`), and drops
any later announce with another key. A machine that lost its key enrolls
again.

A worker's `core.call` (a sub-call its plugin makes, `{ticket, cap, target,
args, why}`, the ticket being that of the call it is running) and
`core.elevate` MUST carry `proof`, a `rook-request` signed with that key:
`worker`, `hub`, `band`, `id` and `cap` (the request's), `args_hash` (of its
`args`), `parent` (the ticket's nonce), `nonce` (32 hex, fresh), `ts`. The hub
refuses, with `denied: worker request: <reason>`, on `no_proof`,
`malformed`, `unknown_worker` (no pinned key), `unknown_key`,
`bad_signature`, `wrong_hub`, `wrong_band`, `wrong_call`, `wrong_cap`,
`wrong_args`, `wrong_parent`, `wrong_worker` (the ticket is not for the
signer), `stale` (more than 60 s off) and `replay`. Then the ticket must be
one it minted for that worker and still in flight. The hub dispatches the
sub-call (6.1) as the parent call's person, agent and session, with the
calling plugin's `uses` as an extra layer, at most 32 per parent call, each
journaled with `parent`. Vectors: `requests.json`.

### 10.3 Hub keys and the signed hub announce

The hub has a **root key** (its identity, `hub_id = key_id(root)`) and an
**operational key** that signs tickets and announces, replaced every 30
days. The root certifies it with a `rook-hubkey` grant, `{hub, key, bands,
iat, exp}`, valid 7 days and renewed daily, which rides as `hubkey` on
tickets and announces. A worker that verifies accepts the grant when it is
signed by the root it pinned at enrollment, the band is in `bands`, and
`iat − 3600 ≤ now ≤ exp + 3600`; and the hub announce (4.4) when it verifies
under such a key, `|now − ts| ≤ 90` and `seq` is higher than the last one.
A verified announce or ticket is **hub contact**; 90 s without it, the
worker has **lost the hub**. Vectors: `signatures.json`.

### 10.4 Events

A worker tells the hub something happened with a `core.event` call to the
hub node: `{worker_id, events: [{id, event, plugin, payload}, …],
dropped?}`, at most 100 events, answered `{"taken": n}`. The hub takes an
event only from a worker it knows on that band, declared under `emits` in a
manifest the worker announced (by that `plugin`), whose name the worker's
ceiling grants (everything, unless it announced one), with a payload that
fits the declaration (≤ 16 KiB). It sets `person` (the worker's owner) and
`worker` on the payload, drops ids it has seen, and passes the event to hub
plugins' `on:` hooks, jobs and live pages. A worker that is not known yet
gets `unknown worker` with `retry`. No ticket: an event is only as good as
the band key. The profile shows the smallest sender.

### 10.5 Streams

A cap that answers `{"stream": id}` opened a stream (a terminal, a process,
a socket, a chat room). Streams are specified in `rook/streams/README.md`
and the core `stream` manifest (`rook/core2/builtin/stream.yaml`).

### 10.6 Transport extensions

A peer lists the ones it speaks in its announce's `features`; a peer
without one never receives it. From core v1 section 14, unchanged:
addressed routing (`route`, `rr`), large replies over Drop (`drop`, and
native DROP frames), and compression (`z`). And **acks** (`ack`): a request
whose `accept` lists `ack` asks its target to ack it, `{"kind": "ack",
"from", "ids": […]}`, and promises to ack the reply; either side may resend
until acked; a request seen again with the same `id` and ticket nonce is a
resend and MUST NOT run twice. The hub never resends to a worker that
announced without `ack`.

### 10.7 Signed updates

**Plugins.** A pushed plugin carries `plugin.sig`, a `rook-plugin` object
signed by the hub's operational key: `hub`, `worker` (the one it was
resolved for), `plugin`, `version`, `files` (relative path → hex SHA-256,
every file but `plugin.sig`), `python` (pinned requirements) and `iat`. A
worker installs it only when the signature verifies under a hub it trusts,
`worker` is its own id and the files match, and re-checks the hashes at
every start.

**Releases.** A core release carries `release.sig`, a `rook-release`
object: `hub`, `version`, `core`, `files`, `requires` and `iat`, for every
worker at once. A worker runs it only when the signature verifies under a
hub it trusts, the files match and `core` negotiates with its own. A worker
that runs one built file names it in `facts.artifact` and takes only
releases that hold it. The update procedure (trial, confirmation,
rollback) is in `docs/castling-updates.md`.

### 10.8 Appliances and delegates

An **appliance** (a TV, a lamp, a printer) runs no worker; a **delegate**
worker that can reach it carries its calls. An appliance is hub state: an
instance of a plugin with an `appliance` section, with a name, settings and
its delegates in order (`appliance.add`, which runs the plugin's `pair` cap
first when it has one, `appliance.list`, `appliance.remove`). A call names
the appliance as its target and reaches only its plugin's caps; the hub
sends it to a delegate with `target` = the delegate's `worker_id`,
`appliance` = the instance name, and `carry` = `{settings, instance}` (the
plugin's hub-kept settings and the instance's `name`, `host`, `port` and
`settings`, secrets resolved for this call only). Online delegates go first;
a send that didn't arrive, or a call safe to repeat (5.3) that timed out or
failed with `retry`, goes to the next one, and the delegate that answers
goes first next time.

### 10.9 Elevation (parked)

The Python worker runs every call as a normal user and can ask the hub,
inside a running call, for a 15-minute **lease** to run it as
administrator: `core.elevate {ticket, reason}`, signed (10.2), answered
with a `rook-lease` after the hub checks the call is in flight and its
rights permit `core.elevate` (risk `admin`) on that worker; the hub drops
leases with `core.lease.drop`. A cap declares `elevation: never | may |
upfront` (the shell plugin uses `may`). The code exists, is dormant
(`docs/castling-design.md`: "Park … elevation and leases") and is not
required: a worker that doesn't elevate simply runs as its account.
Vectors: `leases.json`.

### 10.10 Ceilings and several bands (parked)

The Python worker keeps a trust store: an anchor (the home hub, pinned at
enrollment) and `rook-membership` documents, one per other hub and band,
each with a **ceiling** (a rights object, 6.2) bounding what that hub may
do there; `core.trust.set` and `core.trust.get` manage it, and it announces
`role` and `ceiling`. The hub applies an announced ceiling as one more
rights layer (`core.manifests`, `core.lease.drop` and `core.trust.set`
excepted) but no longer makes memberships: it serves one band. The code is
dormant and not required. Vectors: `memberships.json`.

## 11. Conformance

| Profile | Requires | Vectors |
|---|---|---|
| **wire** | 2.1 | v1 `band_crypto`, `fragments`, `frames` |
| **worker** | wire and [the minimal worker profile](worker-profile.md) | |
| **hub** | sections 2 to 9, and the hub's side of 10.1 to 10.4 and 10.8 | `canonical`, `signatures`, `version`, `rights`, `presets`, `journal`, `requests` |
| extensions | each part of section 10 a worker takes on | `tickets`, `requests`, `leases`, `memberships` |

`conformance/core-v2/README.md` describes the vector files. The Python
reference passes them all; `generate.py` checks the hand-written
expectations against it and rewrites the computed parts.

## 12. Security considerations

- **The PSK is band membership.** Anyone holding it reads the band's
  traffic, secret values sent with calls included, and can announce
  anything and answer calls sent to a `worker_id` it impersonates (replies
  are unsigned). A worker that checks tickets (10.1) runs only what the hub
  signed; one that doesn't runs whatever a band member sends it.
- **Tickets** are single use, bound to the call and its arguments, and
  live a minute; a ticket read off the band is no use for `core.call` or
  `core.elevate`, which the ticket's own worker must sign (10.2).
- **`why` is data**: attacker-influenced text for whoever reads it; it never
  changes rights.
- **Journal.** Hash chains and write-once rows catch a confused or
  compromised agent covering its tracks, not root on the hub.
- Transport considerations of core v1 section 12 (empty AAD, nonce reuse,
  route ids, relay exposure) apply unchanged.
- The hub runs with `hub.security` off until the hardening pass (6.1).

## 13. Changes from core v1

The transport is v1's (2.1). Manifest hashes and heartbeats replace cap
lists and hash-only announces (v1 14.5), appliances replace device workers
(v1 14.6), chat rooms (v1 7) are streams, and every call carries a ticket.
