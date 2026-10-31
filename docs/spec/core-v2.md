# Rook core specification, version 2.0

Status: **2.0 draft** for Castling (Rook 0.5). A clean cut from
[core v1](core-v1.md): no v1 peer interoperates with a v2 peer, and every
machine of a band moves together. Companion material:

- `conformance/core-v2/vectors/*.json`: test vectors for the pure functions
  (canonical JSON, signed objects, ticket checks, version negotiation,
  rights, presets, gate verdicts, journal chains). Where prose and vectors
  disagree, the vectors win and the prose is a bug.
- `rook/core2/`: the Python reference for those functions.
- `conformance/manifest/manifest.schema.json`: the plugin manifest schema.
  The manifest format is specified there and in the Castling manifest pages;
  this document only says how manifests travel and which enums they use.
- Design background (Rook knowledge): `castling-security`,
  `castling-protocol`, `castling-multiband`, `castling-journal`,
  `castling-changes`, `castling-enrollment`, `castling-manifest`.

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT,
RECOMMENDED, MAY and OPTIONAL are to be read as in RFC 2119 and RFC 8174 when
they appear in capitals.

## Contents

1. [Overview](#1-overview)
2. [Transport and versions](#2-transport-and-versions)
3. [Messages, canonical JSON and signatures](#3-messages-canonical-json-and-signatures)
4. [Caps: risk, scope and effects](#4-caps-risk-scope-and-effects)
5. [Hubs, workers and presence](#5-hubs-workers-and-presence)
6. [The call](#6-the-call)
7. [Elevation](#7-elevation)
8. [People, agents and rights](#8-people-agents-and-rights)
9. [Workers on several bands](#9-workers-on-several-bands)
10. [Dispatch on the hub and the gate hook](#10-dispatch-on-the-hub-and-the-gate-hook)
11. [Journal](#11-journal)
12. [Enrollment](#12-enrollment)
13. [Core caps](#13-core-caps)
14. [Conformance](#14-conformance)
15. [Security considerations](#15-security-considerations)
16. [Changes from core v1](#16-changes-from-core-v1)
17. [Appendix: wire examples](#17-appendix-wire-examples)

---

## 1. Overview

*This section is informative.*

Rook gives an agent more access where it matters. A **band** is a set of
peers sharing one pre-shared key (PSK) over a Telesthete relay. A **hub**
is the band's control plane: people and agents enter through its doors (MCP,
dashboard, phone, jobs, chat bridges, the house agent), every door ends at
one **dispatch** step, and dispatch decides. A **worker** runs Rook on a
machine, announces what it has, and **enforces**: it runs a call only when
it carries a ticket the hub signed for exactly that call.

```
 agents, people ──MCP/HTTPS──▶ hub ── dispatch: rights ∩, session, gate, holds
                                │      mints one signed ticket per call
                                ▼
          Telesthete relay (keyless, routes by band id)
                ▲              ▲                 ▲
           worker-a        worker-b ──▶ appliance (TV, lamp, NAS admin…)
           checks ticket, per-hub ceiling, replay; runs as a normal user;
           asks the hub for an elevation lease inside the call if needed
```

Layers: Telesthete (stable transport) → **Rook core v2** (this document) →
the Python reference → plugins and apps. Everything in a worker beyond
transport and enforcement (connection, heartbeat and announce, ticket
checks, per-hub ceilings, process launch with the elevation check, plugin
loader, self-update) is a plugin, including shell.

Four kinds of time-bounded authority nest, each checked against the one
above it:

| Layer | Lives | Set by |
|---|---|---|
| Grants (person and agent rights) | until changed | owners |
| Session (declared caps) | hours | the session at start |
| Elevation lease | 15 minutes | the hub, on a worker's request |
| Ticket | about a minute, one call | the hub at dispatch |

## 2. Transport and versions

### 2.1 Telesthete profile

The transport is **Telesthete v0.3.0** (<https://github.com/Bake-Ware/telesthete>,
wire 1.2, `PROTOCOL_VERSION = 3`), pinned. Rook core v2 changes nothing in
it and depends on nothing beyond it. A Telesthete change reaches Rook only
as a versioned, negotiated extension, never as a break.

The Rook profile of Telesthete is unchanged from core v1 and is
incorporated here by reference, with v1's vectors (`band_crypto.json`,
`fragments.json`, `frames.json`) still normative:

| Core v1 section | What | Status in v2 |
|---|---|---|
| 2.1 Keys | `band_id = SHA-256(psk)[0:16]`, HKDF key | kept |
| 2.2 Frames | CHANNEL frames, ChaCha20-Poly1305, **empty AAD** | kept |
| 2.3 Sequence numbers | CSPRNG start, +1 per frame, one counter per process | kept |
| 2.4 Fragmentation | 21-byte envelope, ≤ 1003 data bytes per chunk | kept |
| 2.5 The relay | keepalive `0x00` every ≤ 20 s, idle eviction | kept |
| 2.6 The WebSocket bridge | one frame per WebSocket message | kept |
| 3.4 Rosters | evict after 90 s of silence | kept (heartbeats count, 5.5) |
| 14.1 Addressed routing | ROUTE/ADDRESSED frames, `rr` | kept |
| 14.2 Large replies over Drop | `drop` reply, receiver-driven pull | kept |
| 14.3 Compression | `\0rz` + zlib, `accept: ["z"]` | kept |
| 14.4 Native DROP frames | `drop-native` | kept |
| 14.5 Hash-only announces | `caps_hash` | **replaced** by heartbeats (5.5) |
| 14.6 Device workers | delegate announces devices as nodes | **replaced** by appliances (5.7) |

Peers advertise the transport extensions they speak in the announce's
`features` list exactly as in core v1 section 14.

**Acks (`ack`)**, an optional transport extension in the same style: a
request whose `accept` lists `ack` asks its target to ack it and promises
to ack the reply. An ack is `{"kind": "ack", "from": "<node_id>", "ids":
["<id>", …]}` (sent to the peer's route; one message may ack several ids).
The target acks the request unless its reply goes out first, and may
resend the reply until it is acked; the caller may resend a request until
it is acked or answered, unless its target announced without `ack` (a hub
that just restarted resends to workers it has not heard yet). A request
received again with the same `id` and the same ticket `nonce` is a resend:
it MUST NOT run again (a held reply may be sent again); a re-minted ticket
(6.5) is a new request. A reply received again is only acked. Resends stop
when the caller gives up on the call. A peer without `ack` ignores acks as
unknown messages (3.1), and refuses a resend that reaches it before its
announce did as a replay (6.3).

### 2.2 Versions

Core versions are `MAJOR.MINOR`, two decimal integers without leading
zeros (`"2.0"`, `"2.1"`). This document is **2.0**.

- **Both sides state their version.** Every announce (worker and hub)
  carries `core`. Each side computes the version for a peer pair as
  `negotiate(mine, theirs)`: if the majors are equal, the same major with
  the **lower** minor; otherwise there is none. A peer with no version in
  common (another major, or a malformed `core`) MUST NOT be sent calls, and
  its calls MUST NOT be served. A hub shows such a worker as incompatible
  with the reason; a worker keeps its connection and keeps announcing (so
  the hub can show it) but serves nothing.
- **Use the lower version.** Toward a peer, an implementation sends only
  what the negotiated minor defines. Each band connection negotiates on its
  own (section 9).
- Vectors: `version.json`.

### 2.3 Compatibility rules

Every v2.x is compatible with this document. The rules that make that hold:

1. **Unknown fields are ignored**, in every message, object and result.
   Inside a signed object an unknown field is still covered by the
   signature (3.3): verify over every field received, then ignore the ones
   you don't know.
2. **Anything added in a minor version is optional** and has a stated
   default that means "behave as before" when it is absent.
3. Existing fields never change meaning or type; normative error strings
   and reason codes never change; enum values are never removed or
   reordered. New enum values arrive in minors with a stated fallback for
   peers that don't know them (4.2, 4.3).
4. Anything that breaks these rules is v3.

## 3. Messages, canonical JSON and signatures

### 3.1 Message kinds

Every message is one UTF-8 JSON object inside the Telesthete profile.
Messages that are not JSON objects (binary sub-protocols such as Drop)
MUST be ignored by peers that don't implement them; the one-byte keepalive
`0x00` MUST be discarded.

| Kind | Recognised by | Section |
|---|---|---|
| request | `cap` present (a non-empty string) | 6.1 |
| reply | no `cap`; `id`, `from` and `ok` present | 6.4 |
| announce | no `cap`; `kind == "announce"` | 5.3, 5.4 |
| heartbeat | no `cap`; `kind == "heartbeat"` | 5.5 |

Anything else is foreign chatter and MUST be ignored. New kinds MUST carry
`kind` and MUST NOT carry `cap`.

### 3.2 Canonical JSON

`canonical(v)` is the **JSON Canonicalization Scheme, RFC 8785**, encoded
as UTF-8:

- object members sorted by their names compared as **UTF-16 code units**;
- no whitespace; strings with only the escapes RFC 8785 requires (`\"`,
  `\\`, `\b`, `\f`, `\n`, `\r`, `\t`, and `\u00XX` with lowercase hex for
  the other control characters below U+0020), everything else literal
  UTF-8, `/` and U+007F included;
- numbers serialised as ECMAScript `Number.prototype.toString` does
  (`1`, `1.5`, `1e+21`, `1e-7`, `0.000001`); `-0` as `0`.

Values that have no canonical form MUST make the operation that needed one
fail: NaN and infinities, strings with unpaired surrogates, non-string
object keys, and integers outside ±(2^53 − 1) (they can't round-trip
through an IEEE double). An integer-valued number and the same value
written with a fraction (`1` and `1.0`) canonicalise identically.

`args_hash(args)` = base64url(SHA-256(canonical(args))) without padding,
where a missing or `null` `args` hashes as `{}` (and nothing else does).

Vectors: `canonical.json`.

### 3.3 Keys and signed objects

Keys are Ed25519. A public key is written `ed25519:<standard base64 of the
32 raw bytes>`. `key_id(pub)` = the first 16 lowercase hex characters of
SHA-256 over the 32 raw bytes.

A **signed object** is a JSON object with:

| Field | Meaning |
|---|---|
| `typ` | the object type; selects the signing prefix |
| `v` | the object's format version: `2` for everything in this document |
| `kid` | `key_id` of the signing key |
| `sig` | standard base64 of the Ed25519 signature |

The signature covers `PREFIX(typ) ‖ canonical(object without sig and
hubkey)`. `hubkey` (5.2) is the only field that may ride on a signed object
unsigned. A verifier MUST accept an object only under the prefix of its
own `typ`:

| `typ` | Prefix | Signed by | Section |
|---|---|---|---|
| `rook-hubkey` | `rook-hubkey-v2\n` | hub root key | 5.2 |
| `rook-announce` | `rook-announce-v2\n` | hub operational key | 5.4 |
| `rook-ticket` | `rook-ticket-v2\n` | hub operational key | 6.2 |
| `rook-lease` | `rook-lease-v2\n` | hub operational key | 7.3 |
| `rook-membership` | `rook-membership-v2\n` | the worker's home hub (root or operational key) | 9.3 |
| `rook-plugin` | `rook-plugin-v2\n` | hub operational key | 13 |
| `rook-release` | `rook-release-v2\n` | hub operational key | 13 |

Times are integer Unix seconds. Identifiers called "32 hex" are 32
lowercase hex characters drawn from a CSPRNG.

Vectors: `signatures.json` (test keys from published seeds).

## 4. Caps: risk, scope and effects

### 4.1 Names

A cap name is `<plugin>.<suffix>`: the namespace is the owning plugin's
name (`[a-z][a-z0-9-]*`, unique per hub) and the suffix MAY contain further
dots (`deluge.torrents.list`). Names are case-sensitive. Cap names say what
they do, not who ships them. A renamed cap keeps its old name as a declared
alias for a release or two; an alias resolves to the new name at dispatch,
and tickets carry the resolved name.

A cap MAY provide a **contract**: a shared interface defined once (by
core or a package) under its own name, such as `screen.capture` or
`notify.send` (manifest `contracts`/`provides`). Its manifest data lists
the contracts it provides in `contracts`. A call made by a contract's
name resolves to the one cap on the target that provides it (more than
one: the call is refused and names them); the call keeps the contract's
name beside the resolved cap, so patterns may match either (8.2), and
its ticket carries it (6.2). The provider's own risk, scope and effects
apply unchanged.

Every cap is declared ahead of time in its plugin's manifest with `doc`,
`risk`, `scope`, `effects`, argument and result schemas. The hub answers
"what does this cap do" from manifests, with the worker offline. **The
enums below are owned by this document**; the manifest schema MUST use
exactly these values.

### 4.2 Risk

Required on every cap. Ordered, lowest first:

| Value | Means |
|---|---|
| `read` | observes; changes nothing |
| `write` | changes state through the cap's own narrow purpose |
| `exec` | runs arbitrary code or commands (shell, scripts, an `exec` action whose binary comes from an argument) |
| `admin` | changes Rook administration, security or system configuration; also what an elevated call counts as (7.1) |

An unknown risk value MUST be treated as `admin`.

### 4.3 Effects

Security flags, a controlled vocabulary. A cap lists zero or more.

| Flag | Means | Hub treatment |
|---|---|---|
| `destructive` | erases or overwrites data for good | hard stop (10.3); never retried automatically |
| `sensitive` | reveals secrets or private content (screen, camera, secret values) | hard stop when it reveals a secret's value; gate weight otherwise |
| `personal` | reads or writes a person's personal data (messages, contacts, location) | presets; multi-user filtering |
| `physical` | acts on the physical world (power, locks, motors, speakers) | not in child or guest presets; never retried automatically |
| `sends_as` | sends as a person to someone else | hard stop |
| `spends` | spends money | hard stop |
| `persists` | creates persistence (services, keys, users, scheduled tasks) | journaled loudly; stricter gate |
| `taints` | brings outside content into the session (web, email, inbound messages) | marks the session tainted (8.4) |

An unknown effect flag MUST be treated as `destructive` for holds, and is
outside any rights object that lists its allowed effects (8.2). Egress has
no flag: it is `scope: external`.

### 4.4 Scope

Required on every cap. How far the effect reaches; used by gates and
shown to people.

| Value | Means |
|---|---|
| `self` | Rook's own state on this node, or the caller's own data |
| `local` | this machine (or this appliance) |
| `multi-machine` | several machines of the band |
| `multi-user` | other people's data or accounts |
| `external` | outside the band: the internet, third-party services |

An unknown scope MUST be treated as `external`.

## 5. Hubs, workers and presence

### 5.1 Node identities

| Id | What | Form |
|---|---|---|
| `worker_id` | a worker; persisted, survives restarts; the same on every band the worker joins | 32 hex |
| `hub_id` | a hub: `key_id` of its root key | 16 hex |
| `band` | a band: its `band_id` | 32 hex (16 bytes) |

The hub appears on its band as a node named `rook`. A client MUST resolve
the name `rook` only to a node whose announce verifies as a hub announce
(5.4) under a hub key it trusts; anything else naming itself `rook` is an
ordinary, suspect peer.

### 5.2 Hub keys

A hub has a **root key** (its identity; `hub_id = key_id(root)`), kept
off the hot path, and an **operational key** that signs tickets, leases and
announces, rotated about every 30 days. The root certifies the operational
key with a `rook-hubkey` grant:

```json
{"typ": "rook-hubkey", "v": 2, "hub": "<hub_id>", "key": "ed25519:<op key>",
 "bands": ["<band>"], "iat": 1793300000, "exp": 1793904800,
 "kid": "<hub_id>", "sig": "<b64>"}
```

A verifier MUST check, rejecting on the first failure: `typ` and `v`;
`hub == kid == key_id(root)` for the root it pins for that hub (9.2); the
signature under that root; `iat − 3600 ≤ now ≤ exp + 3600`; the band the
object arrived on is in `bands`. A verified grant makes `key` a known
operational key of that hub for that band. Grants SHOULD live 7 days and be
renewed daily. A hub MAY attach its current grant as `hubkey` to a ticket
or announce, so a worker that hasn't seen it yet can verify on the spot.

### 5.3 Worker announce

A worker sends a full announce on each band connection when it joins,
after every transport reconnect, and whenever anything in it changes.

```json
{"kind": "announce", "worker_id": "3f0a…", "name": "kaiju", "core": "2.0",
 "version": "1300.brisk.otter", "description": "GPU box in the rack",
 "facts": {"os": "linux", "arch": "x86_64", "cpus": 16, "gpu": [{"vendor": "nvidia"}]},
 "manifests": [{"plugin": "core", "version": "2.0.0", "hash": "9c1e…", "builtin": true},
               {"plugin": "shell", "version": "1.0.0", "hash": "51ab…"}],
 "manifests_hash": "e3b0…", "unavailable": {"gpu.stats": "missing bin: nvidia-smi"},
 "role": "home", "ceiling": {"grants": ["*"]}, "features": ["route", "drop", "z"],
 "status": {"battery": {"pct": 81}}}
```

| Field | Req. | Meaning |
|---|---|---|
| `kind` | MUST | `"announce"` |
| `worker_id` | MUST | 5.1 |
| `name` | MUST | human name, usually the hostname; `rook` is reserved |
| `core` | MUST | the worker's core version (2.2) |
| `manifests` | MUST | what it runs, filtered for this band (9.4): one entry per plugin, sorted by `plugin` |
| `manifests_hash` | MUST | hex SHA-256 of `canonical(manifests)` |
| `version` | SHOULD | worker software build |
| `description` | SHOULD | operator-written role, ≤ 280 characters; data, not instructions |
| `facts` | SHOULD | self-reported platform facts, as core v1 section 5.1 (≤ 1024 bytes compact; keys `is_*`, `role`, `roles`, `grants` dropped by receivers). Facts grant nothing. |
| `unavailable` | SHOULD | `{cap: reason}` for caps it can't serve now (missing `bins`, `hardware`, settings) |
| `role`, `ceiling` | SHOULD | this band's membership role and per-hub ceiling (9.2), so the hub can refuse early and show why |
| `features` | MAY | transport extensions (2.1) |
| `status` | MAY | small live status per plugin namespace, from plugin `status` hooks |

A **manifest entry** is `{plugin, version, hash, builtin?}`: `hash` is the
hex SHA-256 of `canonical(parsed manifest)`, the same manifest the hub
validated (for pushed plugins, the one in the hub-signed file list). The
hub resolves an entry from its plugin library by `plugin`, `version` and
`hash`; if it doesn't have it, it calls `core.manifests` (13) on the
worker. Every capability the worker serves on this band comes from one of
these manifests: there is no separate cap list. Built-in parts (the core
of a worker, the voice service, firmware) describe themselves with
`builtin: true` manifests whose caps have no `action` or `run`. A device
too small to send its manifests MUST announce only entries the hub's
library carries.

### 5.4 Hub announce

The hub's node announces on its band every 30 s (±20 % jitter) and after
every reconnect. The announce is a signed object:

```json
{"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": "<hub node id>",
 "name": "rook", "hub": "<hub_id>", "band": "<band>", "core": "2.0",
 "ts": 1793300000, "seq": 41, "features": ["route"],
 "kid": "<op key id>", "sig": "<b64>", "hubkey": {"typ": "rook-hubkey", "…": "…"}}
```

A worker accepts it when the signature verifies under a verified
operational key of that `hub` for that band (5.2), `|now − ts| ≤ 90`, and
`seq` is higher than the last accepted `seq` from that key. An accepted hub
announce, or a verified ticket from that hub, is **hub contact**: a worker
that has had no hub contact on a band for 90 s has **lost the hub** on that
band (7.4).

### 5.5 Heartbeat

Every worker sends a heartbeat to **every** band it is connected to, every
30 s (±20 % jitter):

```json
{"kind": "heartbeat", "worker_id": "3f0a…", "core": "2.0", "ts": 1793300030,
 "manifests_hash": "e3b0…", "status": {"battery": {"pct": 80}}}
```

`worker_id`, `core` and `manifests_hash` are REQUIRED. A hub that holds no
announce for that worker, or whose `manifests_hash` differs from the last
announce's, calls `core.manifests` (13) on the worker; announces and
heartbeats both count as signs of life for roster eviction (90 s).

### 5.6 Liveness

Stored status is a routing hint only. A hub MUST NOT refuse a call because
it believes a worker or appliance is offline; it attempts the real call
(and for appliances, the next known-good route) and reports the real
failure.

### 5.7 Appliances and delegates

An appliance (a TV, a lamp, a printer, a SaaS account) runs no worker.
A **delegate** worker that can reach it carries its calls. Appliances are
instances of a plugin with an `appliance` section; each instance has a
name, an owning band, a reach scope and a known-good delegate route, all
hub state recorded when the appliance is onboarded. An agent targets the
appliance by name; the hub resolves the route and sends the request to the
delegate with `target` = the delegate's `worker_id` and `appliance` = the
instance name. The ticket binds both. Instance settings and credentials
travel with the call (secrets resolved by the hub per call). A delegate
answers with `from` = its own `worker_id`.

## 6. The call

### 6.1 Request

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "cap": "shell.exec",
 "target": "3f0a…", "args": {"argv": ["uptime"]},
 "ticket": {"typ": "rook-ticket", "v": 2, "…": "…"}}
```

| Field | Req. | Meaning |
|---|---|---|
| `id` | MUST | the call id, 32 hex, chosen by the hub; unique per hub; echoed in the reply |
| `cap` | MUST | cap name (aliases already resolved) |
| `target` | MUST | the `worker_id` that should answer. There are no open calls. |
| `args` | MAY | keyword arguments as a JSON object, secrets already substituted; absent means `{}` |
| `appliance` | MAY | appliance instance name, on a call through a delegate (5.7) |
| `ticket` | MUST | 6.2 |
| `rr`, `accept` | MAY | transport extensions (core v1 section 14) |

A worker MUST stay silent on a request whose `target` is not its own
`worker_id`.

### 6.2 Ticket

One ticket per call, minted by the hub at dispatch (after the agent has
decided, so agent and model latency never count against it), signed with
the hub's operational key:

| Field | Req. | Meaning |
|---|---|---|
| `typ`, `v` | MUST | `"rook-ticket"`, `2` |
| `kid` | MUST | the operational key |
| `hub` | MUST | `hub_id` of the minting hub |
| `band` | MUST | the band the call travels on |
| `call` | MUST | the request `id` |
| `nonce` | MUST | 32 hex, fresh per ticket (a re-minted ticket for the same call gets a new one) |
| `worker` | MUST | target `worker_id` |
| `appliance` | MAY | appliance instance name; present exactly when the request has `appliance` |
| `cap` | MUST | cap name |
| `args_hash` | MUST | `args_hash` of the request's `args` (3.2) |
| `person` | one of | the person the call is for: `person:<name>`, or absent |
| `agent` | one of | the agent making it: `agent:<name>` or `system:<component>`, or absent. At least one of `person` and `agent` MUST be present. |
| `session` | MUST | the session id (8.4) |
| `why` | MUST | the caller's reason in one plain sentence; non-empty after trimming, ≤ 500 characters |
| `elevate` | MAY | `true`: the caller asked for elevation up front (7.2). Default `false`. |
| `contract` | MAY | the contract the call was made by (4.1); `cap` provides it |
| `owner` | MAY | `true`: `person` is an owner of the minting hub (8.5), so the worker may let them at what another person keeps on it for that band (their streams, 13). Only with `person`; a worker honours it only from a hub whose role is `home` or `co-owner` (9.3). Default `false`. |
| `iat`, `exp` | MUST | issue and expiry time; `60 ≤ exp − iat ≤ 120`; hubs SHOULD use 60 |
| `sig` | MUST | 3.3 |
| `hubkey` | MAY | the hub's current `rook-hubkey` grant, unsigned by the ticket (5.2) |

A minute covers cellular links and clock skew; single use and argument
binding mean a longer expiry opens nothing. There is no multi-call ticket:
Ed25519 signing costs tens of microseconds, negligible next to the network
trip.

### 6.3 Worker checks

A worker receiving a request targeted at it MUST check, in this order, and
refuse on the first failure with the reason code shown (vectors:
`tickets.json`):

| # | Check | Reason code |
|---|---|---|
| 1 | a `ticket` is present | `no_ticket` |
| 2 | it is a ticket object with every required field of the right type and form (6.2) | `malformed` |
| 3 | `hub` is a trusted hub for the band the request arrived on (9.2) | `unknown_hub` |
| 4 | `kid` is a verified operational key of that hub (5.2) | `unknown_key` |
| 5 | the signature verifies (3.3) | `bad_signature` |
| 6 | `band` is the band the request arrived on | `wrong_band` |
| 7 | `worker` is its own `worker_id` | `wrong_worker` |
| 8 | `appliance` equals the request's `appliance` (both absent counts as equal) | `wrong_appliance` |
| 9 | `call` is the request `id` | `wrong_call` |
| 10 | `cap` is the request `cap` | `wrong_cap` |
| 11 | `args_hash` equals `args_hash` of the arguments as received | `wrong_args` |
| 12 | `60 ≤ exp − iat ≤ 120` | `bad_lifetime` |
| 13 | `now ≥ iat − 30` | `not_yet_valid` |
| 14 | `now ≤ exp + 30` | `expired` |
| 15 | `nonce` is not in the replay cache | `replay` |
| 16 | the worker serves `cap` on this band (with `contract`: and `cap` provides it) | `unknown_cap` |
| 17 | the call is within this hub's ceiling (9.2) | `outside_ceiling` |

On success the worker records `nonce` in its replay cache and runs the
cap. A refusal is a reply with `ok: false` and `error` =
`denied by worker: <reason code>`, except `unknown_cap`, which replies
`unknown capability: <cap>`. A worker MUST verify every request; there is
no unauthenticated cap and no audit-only mode.

**Replay cache.** A worker keeps every accepted nonce until at least its
ticket's `exp + 30`, and SHOULD keep entries 10 to 20 minutes (the
reference keeps 15). Entries are a few dozen bytes. If it cannot record a
nonce it MUST refuse the call. The cache MUST survive a worker restart (a
file is enough); a worker that can't keep it MUST refuse every ticket for
150 s after it starts.

The ceiling check (17) uses the worker's own manifest data for the cap
(`risk`, `effects`), the ticket's `contract` if any (8.2) and as target the
request's `appliance`, else the worker's `name`. Two exceptions: `core.manifests` and `core.lease.drop`
are within every ceiling, and `core.trust.set` is accepted only from the
home hub, whatever the ceilings say.

### 6.4 Reply

```json
{"id": "5d1c…", "from": "3f0a…", "ok": true, "result": {"stdout": "up 3 days"}}
{"id": "5d1c…", "from": "3f0a…", "ok": false, "error": "denied by worker: expired"}
```

| Field | Req. | Meaning |
|---|---|---|
| `id` | MUST | the request `id` |
| `from` | MUST | the replier's `worker_id` |
| `ok` | MUST | `true` when the cap ran and succeeded |
| `result` | when `ok` | the cap's result (`null` if none) |
| `error` | when not `ok` | a string; normative forms below |
| `retry` | MAY | `true` when the cap says retrying could succeed (plugin `ctx.Error(..., retry=True)`) |
| `elevated` | MAY | the lease id, when the call ran elevated (7) |
| `drop` | MAY | large reply over Drop (core v1 14.2) |

Normative error forms: `unknown capability: <cap>`,
`denied by worker: <reason code>` (6.3, 7), `bad args: <detail>` (the
arguments don't match the cap's declared schema), `elevation: <reason>`
(7.2). Other error text is free-form; secret values MUST be redacted from
it.

### 6.5 Expiry in flight, timeouts and retries

- When a worker refuses with `expired` or `not_yet_valid`, the hub mints a
  fresh ticket (new nonce, new times) for the same call id and resends it,
  once. The agent never sees this.
- There is no protocol-level cancellation. **A call that timed out may
  still be running**: the hub retries automatically only caps declared
  `idempotent: true` and never one with `destructive` or `physical`
  effects. Late replies are journaled.
- The hub waits for a cap's declared `timeout` (manifest), else 15 s, plus
  5 s.

### 6.6 Call context

A worker hands the cap the verified call context from the ticket: `person`,
`agent`, `owner`, `session`, `band`, `why`, plus the hub (`hub`), the call id and the
cap's `risk` from its manifest (`admin` when running elevated).
Plugin code reads it as `ctx.person`, `ctx.agent`, … (plugin kit). Nothing
else on the wire says who a call is for; a worker MUST NOT take identity
from anywhere but a verified ticket.

## 7. Elevation

### 7.1 Running as a normal user

Every call starts **without** administrator rights: no `sudo`, not "run as
administrator". Shell stays fully open: whatever that account can do, a
shell call can do. Every plugin starts processes through core's launch
path, so none can skip this. An elevated call counts as risk `admin` for
the journal and the gate.

Each cap declares `elevation` in its manifest: `never` (default),
`may` (it can ask mid-call), or `upfront` (it needs a lease before it
runs).

### 7.2 Flow

Elevation is the trigger for an online check, made **inside the same
call**: the agent sees one call and one result.

1. The worker needs elevation when the cap is `upfront`, when the ticket
   has `elevate: true` and the cap is not `never`, or when a `may` cap hits
   access denied (an OS permission error, or the plugin raising the kit's
   elevation error; detection is best effort).
2. If it holds a valid lease (7.3) for this hub, `person`, `agent`,
   `session` and itself, it uses it. Otherwise it first checks its own
   ceiling for this hub permits `core.elevate` (risk `admin`), then calls
   `core.elevate` on the hub (13) with the call's ticket.
3. The hub checks that the ticket is its own and the call is still in
   flight, and that the call's rights (8.3) permit `core.elevate` on that
   worker. Unless the call already met the gates as `admin` at dispatch
   (asked up front, or an `upfront` cap), it runs the gates (10.2) on the
   call as `admin`, with `elevation` set to the reason: elevation must
   pass, so a `flag` refuses as well as a `block` (nothing can wait for a
   person inside a running call; asking up front lets a flag become a
   hold). It journals the elevation, with the gates' readings, and returns
   a lease, or refuses.
4. **Fail closed:** no answer from the hub within 10 s, or a refusal, and
   the call fails with `elevation: <reason>` (`hub unreachable`,
   `denied`, `outside ceiling`).
5. With the lease, the worker runs (or for `may`, re-runs once) the cap's
   action elevated, and replies with `elevated` = the lease id.

Re-running is why up-front elevation is preferred: a script that failed
halfway runs again from the start.

### 7.3 Lease

```json
{"typ": "rook-lease", "v": 2, "id": "<32 hex>", "hub": "<hub_id>", "band": "<band>",
 "worker": "<worker_id>", "person": "person:bake", "agent": "agent:claude-code",
 "session": "s_7c1e…", "iat": 1793300000, "exp": 1793300900,
 "kid": "<op key id>", "sig": "<b64>"}
```

`exp − iat` MUST NOT exceed 900 (15 minutes, like a sudo timestamp). A
worker accepts a lease when it is well formed, signed by a verified
operational key of the issuing hub, its `hub`, `band`, `worker`, `person`,
`agent` and `session` equal the ticket's, and `iat − 30 ≤ now ≤ exp`
(vectors: `leases.json`). A lease is reused for later calls with the same
binding until it expires or is dropped; each of those calls still needs
its own ticket and is journaled with the lease id. Leases live in memory
only.

### 7.4 Dropping leases

- **Access change:** the moment rights change for that person, agent,
  session or worker, the hub calls `core.lease.drop` (13) on the worker.
- **Hub loss:** a worker drops every lease from a hub as soon as it has
  lost that hub (5.4), so blocking the hub after elevating ends the lease.
- **Restart:** leases don't survive a worker restart.

Elevation is journaled (11), never prompted: no card, no ping. Alerts come
only from security plugins (gate, triage agent) when something looks
wrong. Because one elevated call can create persistence (an SSH key, a
service), elevation should be rare.

## 8. People, agents and rights

### 8.1 Identities

Identities are strings `<kind>:<name>`:

| Kind | Who | Rights from |
|---|---|---|
| `person:` | a person with a hub account | their preset plus their own grants (8.5) |
| `agent:` | an agent: an MCP token, a persona, a harness session | its own grants (a persona's `grants`, a token's grants) |
| `system:` | hub internals (dispatch housekeeping, `system:hub`) | fixed by the hub; used for core caps (13) |
| `band:` | an unauthenticated band peer, as recorded by the hub | none |

Agents and people each have their own identity. An agent acting **on
behalf of** a person carries both; an unattended agent (the nightly
security agent, a persona job) acts as itself, with its own limited rights,
so the journal names the agent, not the person.

### 8.2 Rights and patterns

A **rights object** describes what one party may do:

```json
{"grants": ["deluge.*", "light.set@downstairs-*"], "max_risk": "write", "effects": ["physical"]}
```

| Field | Req. | Meaning |
|---|---|---|
| `grants` | MUST | patterns; a call must match at least one |
| `max_risk` | MAY | highest risk allowed (4.2); absent = no limit |
| `effects` | MAY | the effect flags allowed; a call is allowed only if all of its effects are in the list. Absent = all; `[]` = none. |

A **pattern** is `cap-glob` or `cap-glob@target-glob`. `*` is the only
wildcard and matches any run of characters, dots included (`phone.*`
covers `phone.motion.started`); everything else matches literally; the
whole name must match. Without `@target` a pattern matches every target;
with it, the call's target name (the appliance instance, else the worker
name) must also match. Event names are grantable with the same patterns.
A call made by a contract's name (4.1) matches a pattern by the contract's
name or the resolved cap's: `screen.*` covers `kvm.screen.capture` called
as `screen.capture`, not called by its own name.

`denies(rights, call)` returns the first failing check of `grants`,
`risk`, `effects`, or nothing. A malformed rights object refuses
everything.

### 8.3 The intersection rule

A call is allowed only when **every layer that applies permits it**. A
layer can only take away; nothing a layer says can grant past another.
The layers:

| Layer | Applies when | Checked by |
|---|---|---|
| person | the call is for a person | hub |
| agent | an agent makes the call | hub |
| session | always (8.4) | hub |
| plugin `uses` | plugin code calls a cap (`ctx.call`, `core.call`) | hub |
| page | a sandboxed page calls a cap (the page's declared `caps`) | hub |
| per-hub ceiling | always, at the target worker (9.2) | worker (hub checks early from the announce) |

So an agent acting for a person gets only what both may do; acting alone
it uses its own rights. Vectors: `rights.json` gives layers and calls with
the expected decision and the first refusing layer.

### 8.4 Sessions

Every call belongs to a session. A session is hub state:

| Field | Meaning |
|---|---|
| `id` | session id |
| `person`, `agent` | who it acts as (8.1) |
| `caps` | patterns declared at start; the hub signs tickets only for caps matching one of them |
| `project` | the project its work belongs to (stamped on every journal entry, 11) |
| `task` | the pinned task recorded at start, for gates |
| `tainted` | set once the session calls a cap or receives an event with `taints`; never cleared |
| `started`, `exp` | sessions last hours; hubs SHOULD end them after 12 h |

`caps` is declared at the start and does not grow; a session that needs
more starts a new one. A hub MAY fill the declaration from the agent's
configuration (a persona's grants, a job's steps); `["*"]` is a valid
declaration that leaves only the identities' layers.

### 8.5 Users and presets

Every person has a **preset**. Core ships four; owners edit them like any
other hub state, and plugins add grants to them in their manifest
(`presets: {child: {grants: [homework.item.list]}}`).

| Preset | `grants` | `max_risk` | `effects` | Other |
|---|---|---|---|---|
| `owner` | `["*"]` | none | all | owns every band of the hub; manages people, bands and ceilings |
| `adult` | `["*"]` | `admin` | all | |
| `child` | `["agents.chat.*", "pages.view"]` | `write` | `[]` | `approve_new: owner`: the first call of a cap the person hasn't used is held for an owner |
| `guest` | `["agents.chat.*"]` | `write` | `[]` | `expires: 7d` after the account is created |

A person's rights = the preset's `grants`, plus the grants plugins add to
that preset (a plugin may add only patterns inside its own namespace,
`<plugin>.…`; others are ignored), plus grants an owner gives that person; **`max_risk` and
`effects` stay the preset's**. A plugin (or a per-person grant) can widen
what is reachable but never push past the preset's ceilings: a child
granted `lights.set` still can't use it while it has `physical`. Vectors:
`presets.json`.

## 9. Workers on several bands

### 9.1 Ownership

- A worker's **home hub** is the hub it was set up on (12). The person who
  sets it up owns it; the home band's default ceiling is everything
  (`{"grants": ["*"]}`), so day one needs no configuration.
- Adding the worker to another band happens on the home hub: pick the
  worker, "add to another band", choose a role. Nobody touches the
  machine. Other bands **invite, never pull**: an invite is a small JSON
  blob (the inviting hub's `hub_id`, root key, band, connection details
  and a suggested role and ceiling) that a person brings to the home hub,
  where an owner accepts it.
- Roles per band: `home`, `co-owner` (may manage the worker too: plugins,
  updates, settings, within a full default ceiling) or `guest` (with a
  ceiling). Changing memberships and ceilings stays with the home hub.

### 9.2 Trust store

A worker keeps one **anchor** and a set of **memberships**:

- The anchor: the home hub's `hub_id`, root key and band, pinned at
  enrollment. It is stored where the worker's own process can't write it
  whenever the platform allows (a service install keeps it owned by
  root/Administrator); replacing it is a local administrator action on the
  machine.
- Memberships: one per (hub, band), each a `rook-membership` document
  signed by the home hub (9.3), naming the hub, its root key, the band, the
  role and the per-hub **ceiling** (a rights object, 8.2).

The worker holds no signing key, so it cannot create or widen an entry.
Without a membership for the home band, the home ceiling is everything. A
hub is trusted for a band exactly when the trust store has a membership
(or the anchor) for that pair. Ceilings bind the hub: nothing it signs gets
past them, whatever its grants say.

### 9.3 Membership document

```json
{"typ": "rook-membership", "v": 2, "serial": "<32 hex>", "worker": "<worker_id>",
 "hub": "<member hub_id>", "root": "ed25519:<member root key>", "band": "<band>",
 "role": "guest", "ceiling": {"grants": ["light.*@downstairs-*"], "max_risk": "write"},
 "by": "person:bake", "iat": 1793300000, "kid": "<home key id>", "sig": "<b64>"}
```

| Field | Meaning |
|---|---|
| `worker` | the worker it applies to |
| `hub`, `root`, `band` | the member hub, its root key (`hub == key_id(root)`), and its band |
| `role` | `home` (narrows the home band's own ceiling), `co-owner`, `guest`, or `removed` |
| `ceiling` | rights object; required unless `removed` |
| `by` | the owner who made the change |
| `iat`, `serial` | issue time; per (hub, band) the worker keeps only the newest and refuses a document whose `iat` is not newer (no rollback) |

The worker accepts a document signed by the home hub's root key or a
verified home operational key, for its own `worker_id`, whose `hub` is
`key_id(root)`, with a known role and a valid ceiling (vectors:
`memberships.json`). Role `home` is valid exactly when `hub` is the home
hub: there is one home, and the home hub is never a guest or co-owner of
itself. Documents arrive through
`core.trust.set` (13), whose arguments also carry the member band's
connection details (relay or WebSocket address and the band PSK as a
`{{secret:…}}` reference, resolved by the home hub for that call).

### 9.4 Independent connections and filtered views

- The worker keeps its **own connection** to each band (its PSK, its relay
  or WebSocket) and never relays one band's traffic through another hub.
  It announces, heartbeats (5.5), negotiates its version (2.2) and loses
  the hub (5.4) per band.
- The band a request arrived on is the band its ticket must name, and the
  ceiling that applies is that hub's. Nothing about other bands crosses a
  band's connection.
- **Filtered view:** on each band the worker announces only manifests with
  at least one cap or event (`emits`) whose name the band's ceiling grants
  match, and its `ceiling` for that band. Events go to a hub only when its
  ceiling grants their name (13, `core.event`). A delegate that is a member of an appliance's
  band only as a proxy has a ceiling naming those appliances (`light.*@…`),
  so that band sees nothing else of the machine.
- The home hub knows all of a worker's bands (it added them). A guest band
  sees only its own connection.

### 9.5 Losing the home hub

If the home hub is gone, the worker keeps serving its other bands within
their ceilings and is managed locally until a person with administrator
rights on the machine points it at a new home (a new enrollment, 12).
Re-anchoring removes every membership; the new home adds them again.

## 10. Dispatch on the hub and the gate hook

### 10.1 Dispatch order

Every door ends at dispatch, before a ticket exists. Platform agents that
run inside the hub (the security triage agent, recovery) call dispatch
in-process rather than through MCP, so they keep working when MCP fails or
is compromised; an owner or the security agent can switch the MCP door off
(the kill switch) without touching the other doors. A hub MUST apply, in
order:

1. **Caller.** Authenticate the door's caller into `person` and/or `agent`
   (a token is an agent acting for its owner or itself; a dashboard sign-in
   is a person; a job runs as its creator or its persona).
2. **Session.** Resolve the session (8.4).
3. **Target and cap.** Resolve the worker or appliance route (5.7) and the
   cap's manifest data; an unknown cap is an error.
4. **Why.** Refuse a call without a `why` (`denied: why required`).
5. **Rights.** The intersection rule (8.3), including the target's
   announced ceiling; refuse with the refusing layer
   (`denied: <layer>: <reason>`).
6. **Gates** (10.2). `block` refuses (`denied: blocked by gate <name>:
   <reason category>`); `flag` MAY hold.
7. **Holds** (10.3).
8. **Ticket.** Mint (6.2), send, wait (6.5).
9. **Journal** (11): one entry per dispatched call, whatever the outcome.

### 10.2 Gate contract

A gate is a plugin's `gate` hook. Gates run at hub dispatch, in parallel,
for calls their `match` selects (by risk, scope or effects). A shared
worker MAY run its own gates before executing, with the same contract,
replying `denied by worker: gate` on a block.

Input:

| Field | Meaning |
|---|---|
| `cap`, `args` | the call (secret references unresolved) |
| `caller` | `{person, agent}` |
| `worker`, `appliance` | the target |
| `why` | the caller's reason |
| `risk`, `scope`, `effects` | from the manifest (an elevated call is `admin`) |
| `session` | `{id, caps, project, task, tainted}` |
| `band` | the band |
| `elevation` | only on `core.elevate` (7.2): `upfront` or `access_denied` |

Output:

| Field | Meaning |
|---|---|
| `verdict` | `allow`, `flag` or `block` |
| `confidence` | number 0..1 |
| `reason` | category: `intent_mismatch`, `anomaly`, `persistence`, `exfiltration`, `injection`, `canary`, `taint`, `policy`, `gate_error`, `other` |

Rules (vectors: `gate.json`):

- **Block-only by construction.** Gates can only restrict, never grant; the
  hub ignores anything in a result that isn't a restriction. `block` wins
  over `flag`, `flag` over `allow`.
- A gate in `shadow` mode is journaled but never counts. Gates ship in
  shadow mode first.
- A gate that errors, times out or returns a malformed result counts as
  `flag` with reason `gate_error`.
- Each gate declares a latency `budget` (default 20 ms); the hub MUST NOT
  wait longer than 200 ms for any gate.
- Disabling or replacing a gate plugin is a deliberate owner action and is
  journaled.
- A gate MAY also declare a `result` check, run on a call's result after it
  ran, with the same input plus `result`. It can only withhold: `block`
  turns the reply into `denied: blocked by gate <name>: <reason>`; any other
  verdict is journaled only (a call that already ran can't be held). Its
  entries are journaled with the call's other gate results, marked
  `stage: result`. Canary detection uses it.

### 10.3 Holds and hard stops

When the trust map, a hard stop or a gate's flag requires approval, the hub
**holds the exact call**: the right person gets one card, and approving
runs that exact call (same cap, target, `args_hash`, session) with a ticket
minted at that moment. The caller gets a held reply carrying the hold's
name; a refusal is final. One approval path serves deploys, sacred caps
and security holds.

**Hard stops** always hold, and nothing (no relayed approval, no triage
agent) releases them except the person: a change on a sacred machine,
`destructive` effects (unknown effects count as destructive), revealing a
secret's value (`sensitive` on a secret-revealing cap), `sends_as`, and
`spends`.

Lasting changes (deploys, configuration, plugin installs, trust and grant
edits, deletes; guidance, not a hard rule) get a **change record** in the
journal (what, why, risk, testing, rollback, steps), with ceremony scaled
by the project's trust level: `sacred` needs the record and owner
approval, `normal` the record, `ship-it` a one-line note.

## 11. Journal

### 11.1 Who writes

**Only the hub writes the journal**: one entry per dispatched call, plus
change records, elevations and lease drops, holds and their outcomes,
security events (gate blocks, triage actions) and session starts and ends.
Agents never write entries directly. Workers MAY keep a local audit of
what they ran (time, call id, cap, ticket verdict, a redacted argument
summary); it is not the journal.

### 11.2 Call entry

| Field | Meaning |
|---|---|
| `id`, `kind` | entry id; `call` |
| `chain`, `prev`, `hash` | chain name and links (11.3) |
| `ts` | dispatch time |
| `call` | the call id |
| `hub`, `band` | where it was dispatched |
| `worker`, `appliance`, `cap` | the target and cap |
| `person`, `agent` | who (8.1) |
| `session`, `project` | the session and the project stamped from it |
| `why` | the caller's reason |
| `args_hash` | 3.2; the arguments themselves are kept redacted (secret values replaced) |
| `ticket` | the ticket `nonce`s minted for the call (more than one after a re-mint) |
| `risk`, `scope`, `effects` | from the manifest |
| `decision` | `allow`, `deny`, `held`, `released`, `refused` (by the person) |
| `layer` | on `deny`: the refusing layer and reason (8.3) |
| `gates` | per gate: `{gate, mode, verdict, confidence, reason}` |
| `lease` | lease id when the call ran elevated |
| `ok`, `error`, `ms` | the outcome and duration; `error` when it failed or was refused by the worker |

There is no separate ticket log: the call entry carries the ticket nonce,
the identities and the argument hash.

### 11.3 Hash chains

One table, several chains: each project has its own, and sessions with no
project go to the `general` chain. Every entry has `prev` (the previous
entry's `hash` in its chain; 64 zeros for a chain's first entry) and
`hash` = hex SHA-256 of `canonical(entry without hash)`, which covers
`prev`. The hub keeps every chain's latest hash in memory and re-checks
periodically; a mismatch means history was rewritten. Rows are write-once
at the database layer (a trigger in SQLite, revoked update and delete
rights in Postgres). Vectors: `journal.json`.

### 11.4 Retention and reading

- A project chain lives and dies with its project: pruned some time after
  the project closes, the whole chain at once, never with gaps.
- The `general` chain is pruned by age (30 to 90 days) in daily segments;
  each day starts from the previous day's final hash.
- Owners read everything; others read their own actions; agents read their
  project's history unless granted more.

## 12. Enrollment

A device signs in as a person and the hub tells it how to configure
itself. Enrollment makes the hub the device's home hub (9.1) and the
person its owner.

**Finding the hub**, in order: a QR code from the dashboard carrying the
hub address and a one-time code; local network discovery (mDNS service
`_rook-hub._tcp`); a typed address. Awkward-input devices (headless boxes,
a tablet app) use a **device code**: the device shows a short code, the
person approves it on their phone or the dashboard.

Enrollment runs over the hub's HTTPS endpoint, never over a band. The
result is a **config bundle**, scoped to what that person may do:

| Field | Meaning |
|---|---|
| `hub` | `{hub_id, root, url}`: becomes the anchor (9.2) |
| `band` | `{id, psk, relay, ws_url}` |
| `worker_id` | the id the hub recorded for the device (32 hex) |
| `owner` | the enrolling person |
| `services` | service endpoints the device needs (for example the voice server) with their keys |
| `core` | the hub's core version |

The bundle is shown once and travels only over TLS to the device that
claimed the code. Secrets in it are the device's own (band PSK, service
keys); other secrets stay in the vault and travel per call. Plugin
settings are marked worker-local or hub-provided by the plugin author;
hub-provided values reach the worker with each call, and secret settings
are vault names resolved per call, never written to worker disk.

## 13. Core caps

Every worker implements these caps from its built-in `core` manifest. They
travel like any call, with a ticket, and the hub uses `agent: system:hub`
for the ones it makes on its own.

| Cap | Risk | Arguments | Result |
|---|---|---|---|
| `core.manifests` | read | `plugins` (list, optional) | `{"manifests": [<parsed manifest>, ...]}` for the band's filtered view; within every ceiling |
| `core.lease.drop` | write | `lease` (id) or `person`/`agent`/`session` (drop all matching) | `{"dropped": n}`; within every ceiling |
| `core.trust.set` | admin | `membership` (9.3), `connect` (`{relay, ws_url, psk}`, not for `removed`) | `{"ok": true}` or why not; home hub only |
| `core.trust.get` | read | | the anchor's `hub_id` and band, and the memberships (no PSKs) |

The hub serves three caps to its band's workers. They are the only
band-originated requests a hub serves, and they carry no ticket of their
own. `core.elevate` and `core.call` are each authenticated by the **ticket
of a call still in flight**, which the hub minted itself, for that worker.
`core.event` is only as good as the band key, like an announce (15).

| Cap (on the hub node) | Arguments | Result |
|---|---|---|
| `core.elevate` | `ticket` (the call's), `reason` (`upfront` or `access_denied`) | `{"lease": <rook-lease>}`, or an error: `elevation: denied`, `elevation: outside ceiling` |
| `core.call` | `ticket` (the parent call's), `cap`, `target`, `args`, `why` | the sub-call's reply. The hub dispatches it (10.1) as the parent's `person`, `agent` and `session`, with the calling plugin's `uses` as an extra layer. |
| `core.event` | `worker_id`, `events` (at most 100 of `{id, event, plugin, payload}`), `dropped` (optional count) | `{"taken": n}`; `unknown worker` with `retry` when the hub holds no announce for that worker on this band |

**Events.** A worker sends the events its plugins emit (`emits`) to every
hub whose ceiling grants the event's name, in `core.event` batches, one
batch in flight per hub. While a hub is lost (5.4) the worker keeps them in
a bounded queue per hub; when it is full the oldest go, and `dropped` says
how many with the next batch. A batch the hub didn't answer goes again with
the same event `id`s (random, per event), and the hub drops ids it has
seen. The hub takes an event only when the worker is known on the band the
request came on, a manifest it announced declares the event (by that
`plugin`), its ceiling grants the name (with the worker's name as target)
and the payload fits the declared fields (no others; at most 16 KiB as ASCII JSON). A
worker's events are its owner's data: the hub sets `person` (the person who
owns the worker, null when none is known) and `worker` (its name) on the
payload, replacing whatever the worker sent there. Refused events are
dropped and logged; the batch is still answered. A taken event goes where
the hub's own events go: hub plugins' `on:` hooks (library code an owner
installed), jobs (event jobs are an owner's) and live pages, which show it
only to viewers whose rights grant its name and whose data it is. An event declared `taints: true` taints the sessions that act
on it (8.4): the session a hook or job runs its calls in, and a page's
session once the page receives it.

Worker administration (plugins over Drop, updates, configuration,
enrollment changes) is defined by the core manifest's caps, not by this
document. Whatever pushes a plugin, a worker loads it only when its
`plugin.sig` (the hub's signature over the plugin's file-hash list)
verifies under a hub it trusts with role `home` or `co-owner`; anyone may
write a plugin, but only those hubs approve it for this machine.

`plugin.sig` is a `rook-plugin` signed object: `hub`, `worker` (the
`worker_id` it was resolved for), `plugin`, `version`, `files` (posix path
relative to the plugin folder → hex SHA-256, every file but `plugin.sig`
itself, assets included at `assets/<name>`), `python` (the pinned
requirements the hub resolved for that worker, installed before load) and
`iat`. A worker installs a bundle only when the signature verifies under a
current operational key of such a hub, `worker` is its own id, and the
bundle's files are exactly `files` with matching hashes. Once installed, an
operational key rotating doesn't unload it: at start-up the worker re-checks
the file hashes and that the signing hub still has role `home` or
`co-owner`.

A core release (the worker's own code, `worker.update`) carries
`release.sig`, a `rook-release` signed object: `hub`, `version` (semver),
`core` (the core version the code speaks), `files` (posix path relative to
the release root → hex SHA-256, every file but `release.sig`; the root holds
the `rook/` package), `requires` (PEP 508 requirements installed before the
switch) and `iat`. It names no worker: one release is signed once for every
worker. A worker runs a release only when the signature verifies under a
current operational key of a hub with role `home` or `co-owner`, the files
are exactly `files` with matching hashes, and `core` negotiates (2.2) with
its own. At every start it re-checks the release's file hashes, and that
the signing hub still has one of those roles. The update procedure (trial,
confirmation, rollback) is the core manifest's, not this document's
(docs/castling-updates.md).

## 14. Conformance

| Profile | Requires | Vectors |
|---|---|---|
| **wire** | 2.1 (core v1 transport sections) | v1 `band_crypto`, `fragments`, `frames` |
| **encoding** | 3.2, 3.3 | `canonical`, `signatures` |
| **worker** | wire, encoding, 2.2, 5.3, 5.5, 6, 7.3, 9.3, 13 | `version`, `tickets`, `leases`, `memberships` |
| **hub** | wire, encoding, all of the above plus 8, 10, 11 | `rights`, `presets`, `gate`, `journal` |

`conformance/core-v2/README.md` describes the vector files. The Python
reference (`rook/core2`) passes them all; `generate.py` checks hand-written
expectations against it and rewrites the computed parts (hashes and
signatures).

## 15. Security considerations

- **Hub decides, worker enforces.** With several hubs the worker is the
  only common point, so the worker checks every call locally with no
  callback. A properly signed ticket can't practically be forged; the real
  risks are replay (closed by the single-use nonce, argument binding and
  the one-minute expiry), a stolen hub operational key (a callback wouldn't
  help; the per-hub ceiling bounds the damage and the root rotates it out
  within a grant lifetime) and revocation lag (closed for the dangerous case
  by elevation, which is checked online and dropped on access change).
- **The PSK is band membership.** Anyone holding it reads all of the band's
  traffic, including secret values sent with calls, and can announce
  anything. It cannot mint tickets, so it cannot make a v2 worker run
  anything. It can send events as any worker the hub knows on the band,
  but only events that worker announced, within its ceiling; they count
  as that worker's owner's data and, when declared `taints`, taint
  whatever acts on them. Workers have no keys of their own in v2.0, so an impostor
  worker on the band can receive calls meant for the name it claims;
  calls are bound to `worker_id`, and hubs SHOULD alert when a known
  `worker_id` appears from a second address.
- **Ceilings live on the worker** and are signed by the home hub; the
  worker can't edit its trust store or widen a ceiling. Only the anchor
  depends on local file protection.
- **Elevation fails closed** and ends when the hub can't be reached, so
  cutting the worker off from the hub after elevating ends the lease. One
  elevated call can still create persistence; elevation is rare and
  journaled loudly.
- **Gates restrict only.** A compromised or buggy gate plugin can deny
  service but can never grant. The triage agent reads attacker-controlled
  material all day and is the likeliest injection target: it can release
  one held call with the same arguments, never a hard stop and never a rule
  change.
- **`why` is data.** It is attacker-influenced text for anything reading it
  (gates, people); it never changes rights.
- **Header and transport** considerations of core v1 section 12 (empty
  AAD, nonce reuse, route ids, relay exposure) apply unchanged.
- **Journal.** The threat model is a confused or compromised agent quietly
  covering its tracks, not root on the hub. Hash chains plus write-once
  rows are enough for that; root on the hub wins regardless.

## 16. Changes from core v1

| Area | Kept | Changed | Cut |
|---|---|---|---|
| Transport | Telesthete profile (2.1) | pinned to v0.3.0 | 14.5 hash-only announces, 14.6 device workers |
| Versioning | additive minors, ignore unknown | both sides state `core`, use the lower; clean cut from v1 | build-167 baseline |
| Encoding | Ed25519, `key_id`, per-type prefixes | canonical JSON is RFC 8785 (floats now hashable); all prefixes `-v2` | |
| Announce | facts, features, description | manifests (`plugin@version` + hash) instead of cap lists and tiers; heartbeats to every hub | `caps`, `tiers`, `plugins`, `authz`, `hb`, signed role grants on worker announces |
| Calls | id/cap/target/args, reply shape | ticket mandatory on every call, bound to call, nonce, worker, appliance, cap, args hash, person, agent, session, why; ~1 min expiry; replay cache 10–20 min | open calls, envelope `identity`, authz modes (`off`/`audit`/`enforce-*`), band risk ceiling on the hub |
| Permissions | risk ladder read < write < exec < admin | effects vocabulary replaces tags; scope enum; intersection of layers; per-hub ceilings; presets | tier override tables, `cmd.*` |
| Elevation | | new: in-call, lease, drop on access change or hub loss | |
| Hub | node named `rook`, root + operational keys | `rook-hubkey` grant replaces `is_hub` role grant; signed hub announce is hub contact | revocation lists, deauth orders (a worker leaves by trust-store change) |
| Chat rooms (v1 7) | | moved to the streams spec | |
| Placement (v1 5.2) | | hub installs plugins per worker; manifests carry `platforms` and `requires` | placement expressions, elections |
| Plugin contract (v1 9) | | the manifest schema | `core_api` ranges, resource strings, SQL migration files |
| Journal | hub journal | hash chains per project, write-once, only the hub writes | |

## 17. Appendix: wire examples

A call to `shell.exec` from an agent working for a person
(`conformance/core-v2/vectors/tickets.json`, case `ok`, has the exact
bytes):

```json
→ {"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "cap": "shell.exec",
   "target": "3f0a9c1d2e3f40516273849506a7b8c9", "args": {"argv": ["uptime"]},
   "ticket": {"typ": "rook-ticket", "v": 2, "kid": "…", "hub": "…", "band": "…",
              "call": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "nonce": "…",
              "worker": "3f0a9c1d2e3f40516273849506a7b8c9", "cap": "shell.exec",
              "args_hash": "…", "person": "person:bake", "agent": "agent:claude-code",
              "session": "s_7c1e", "why": "Check how long kaiju has been up.",
              "iat": 1793300000, "exp": 1793300060, "sig": "…"}}
← {"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "from": "3f0a9c1d2e3f40516273849506a7b8c9",
   "ok": true, "result": {"code": 0, "stdout": "up 3 days"}}
```

The same request replayed a minute later:

```json
← {"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "from": "3f0a…", "ok": false,
   "error": "denied by worker: replay"}
```

An elevation inside a call (`elevation: may`, the first run hit access
denied):

```json
worker → hub: {"id": "<32 hex>", "cap": "core.elevate", "target": "<hub node id>",
               "args": {"ticket": {…the call's ticket…}, "reason": "access_denied"}}
hub → worker: {"id": "<same>", "from": "<hub node id>", "ok": true,
               "result": {"lease": {"typ": "rook-lease", "v": 2, "id": "…", "…": "…"}}}
worker → hub (reply to the original call, after the elevated re-run):
              {"id": "5d1c…", "from": "3f0a…", "ok": true, "result": {…}, "elevated": "<lease id>"}
```
