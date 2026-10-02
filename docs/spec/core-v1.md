# Rook core specification, version 1.0

Status: **1.0**, describing the Python reference on `beta` (core API 1.1,
interoperable with build-167 workers). Companion material:

- `conformance/vectors/*.json`: test vectors generated from the reference
  (`conformance/generate.py`); where prose and vectors disagree, the vectors
  win and the prose is a bug.
- `conformance/harness.py`: the live check of a candidate against a
  reference hub.
- `examples/ports/typescript`, `examples/ports/rust`: two small
  implementations written from this document.
- Design background: `docs/design/plugins.md`, `docs/design/permissions.md`,
  `docs/design/settings.md`.

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT,
RECOMMENDED, MAY and OPTIONAL are to be read as in RFC 2119 and RFC 8174 when
they appear in capitals.

## Contents

1. [Overview](#1-overview)
2. [Transport](#2-transport)
3. [Messages](#3-messages)
4. [Capabilities](#4-capabilities)
5. [Node facts and placement](#5-node-facts-and-placement)
6. [The hub worker `rook` and permissions](#6-the-hub-worker-rook-and-permissions)
7. [Chat rooms](#7-chat-rooms)
8. [Identity, journal and audit](#8-identity-journal-and-audit)
9. [The plugin contract](#9-the-plugin-contract)
10. [Versioning and compatibility](#10-versioning-and-compatibility)
11. [Conformance](#11-conformance)
12. [Security considerations](#12-security-considerations)
13. [Appendix: wire examples](#13-appendix-wire-examples)

---

## 1. Overview

*This section is informative.*

A **band** is a set of peers that share one pre-shared key (PSK). Peers
exchange JSON messages through a **relay** that forwards encrypted datagrams
by a 16-byte band id and never holds a key. Every peer on a band sees every
message; addressing is done inside the encrypted payload.

Peers play three roles, and one process can play several:

| Role | Does | Examples |
|---|---|---|
| **Worker** | announces its capabilities (caps) and answers calls for them | `rook-worker`, the Android app, the example ports |
| **Client** | tracks announces into a roster and calls caps, matching replies by id | the hub's MCP bridge and dashboard, the `rook band` TUI |
| **Hub node** | a worker with the reserved name `rook`, proven by a signed role grant; serves hub-placed plugins (knowledge, tasks, chat rooms, policy) | the MCP bridge process |

A **cap** is a dot-namespaced function such as `shell.exec`, called with a
JSON object of keyword arguments and returning one JSON value. Caps are
grouped into **plugins**; where a plugin runs is decided by **placement**
over each node's **facts**.

The **hub** (control plane: MCP bridge, dashboard, journal, vault, signing
keys) is where people and agents enter the band with authenticated tokens.
Band peers themselves are authenticated only by knowing the PSK; section 6
describes the signed grants and call tickets layered on top.

```
 agents/people ──MCP/HTTPS──▶ hub (MCP bridge = worker "rook", dashboard)
                                 │  band client
                                 ▼
          UDP ──▶ relay (telesthete-hub, keyless, routes by band_id) ◀── UDP
                   ▲            ▲             ▲
               worker-a     gpu-box      your port (TS / Rust / …)
```

## 2. Transport

The transport is a profile of the Telesthete protocol
(<https://github.com/Bake-Ware/telesthete>, "SPEC.md"). Only the parts below
are needed to join a band; implementations MUST NOT depend on other
telesthete features being present.

### 2.1 Keys

For a PSK string `psk` (UTF-8 encoded):

```
band_id = SHA-256(psk)[0:16]
key     = HKDF-SHA256(salt = "telesthete-v1", ikm = psk,
                      info = "encryption-chacha20-poly1305", L = 32)
```

`band_id` travels in clear so the relay can route; it reveals nothing useful
about the PSK. Vectors: `band_crypto.json`.

### 2.2 Frames

Each UDP datagram carries exactly one frame:

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 16 | `band_id` | from 2.1 |
| 16 | 1 | `channel_type` | `0x02` (CHANNEL) |
| 17 | 2 | `channel_id` | `0`, big-endian |
| 19 | 8 | `sequence` | big-endian u64, see 2.3 |
| 27 | n+16 | sealed chunk | ChaCha20-Poly1305 ciphertext of one fragment chunk (2.4), then the 16-byte tag |

- The AEAD is ChaCha20-Poly1305 (IETF, RFC 8439) under `key`, nonce =
  4 zero bytes followed by the 8-byte big-endian `sequence`.
- **Associated data is empty.** The telesthete specification binds the
  channel header as AAD; the Rook worker transport does not, and every Rook
  peer (build 167 onwards) seals and opens with empty AAD. Implementations
  MUST use empty AAD to interoperate.
- A sender MUST send `channel_type = 0x02` and `channel_id = 0`. A receiver
  SHOULD drop frames with another channel type and MUST drop frames shorter
  than 43 bytes, with a foreign `band_id`, or that fail authentication,
  silently.

### 2.3 Sequence numbers

The key is shared by every peer of the band, so the nonce must never repeat
under it. A sender MUST draw its initial sequence from a CSPRNG (the
reference uses a random 63-bit value), MUST increment it by one for every
frame it sends (across all messages and fragments), and MUST NOT restart
from a fixed value. Concurrent senders in one process MUST share one
counter. Receivers take the sequence from the frame; they do not track it.

### 2.4 Fragmentation

Every message, including an empty one, is split into chunks, each prefixed
with a 21-byte envelope and sent in its own frame:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `version` = `0x01` |
| 1 | 16 | `fragment_id`, random per message |
| 17 | 2 | `seq`, big-endian, 0-based chunk index |
| 19 | 2 | `total`, big-endian, chunk count (≥ 1) |
| 21 | ≤ 1003 | chunk data |

- Senders MUST NOT put more than 1003 data bytes in a chunk (1024 channel
  bytes minus the envelope) and MUST send the chunks of one message
  contiguously, in order, with consecutive sequences. Messages are limited
  to 65535 chunks.
- Receivers MUST drop chunks with `version ≠ 1`, `total = 0` or
  `seq ≥ total`; MUST reassemble by `fragment_id` in any arrival order;
  MUST ignore duplicate chunks; and on a `total` that differs from the one
  first seen for that id MUST restart that message. Receivers SHOULD drop
  incomplete messages after 30 s and SHOULD bound the number of partial
  messages (the reference keeps 256, evicting the oldest).

Vectors: `fragments.json` (split, parse, reassembly scenarios) and
`frames.json` (complete datagrams).

### 2.5 The relay

The relay (`telesthete-hub`) listens on UDP (default port 7474). For every
datagram it reads `band_id`, remembers the sender's address as a member of
that band, and forwards the datagram unchanged to every other member of the
band. It never forwards a datagram back to its sender, never decrypts, and
carries many bands at once.

- A peer registers by sending any frame. On joining, a peer MUST send a
  **keepalive** message (the one-byte payload `0x00`, fragmented and sealed
  like any message) and MUST repeat it at least every 20 s while it wants to
  stay registered; the relay evicts idle peers (`HUB_PEER_TTL_SECS`, 60 s in
  the shipped deployment).
- Receivers MUST discard a reassembled message equal to the single byte
  `0x00`.
- Messages that are not JSON objects (binary sub-protocols such as the
  in-band OTA transfer ride the same channel) MUST be ignored by peers that
  don't implement them.

### 2.6 The WebSocket bridge

Peers that can't send UDP (browsers, restrictive networks, the Android app
behind a tunnel) MAY reach the relay through the hub MCP server's `/band`
WebSocket endpoint (`ws://host:port/band`, `wss://` when the port is 443 or
8443). Each binary WebSocket message carries exactly one frame (2.2) in each
direction; the bridge gives each WebSocket its own UDP socket towards the
relay, so bridged peers see each other and never need the PSK in the bridge.
Frames larger than 65507 bytes close the socket (code 1009).

Trade-off: the bridge adds a TCP hop and depends on the hub process being up,
but lets an implementation skip UDP entirely; everything above the frame is
identical. Implementations SHOULD reconnect with backoff (reference: 1 s
doubling to 30 s) and MUST send a keepalive and an announce after every
(re)connect. The example ports use UDP.

## 3. Messages

Every message is one UTF-8 JSON object. Senders SHOULD NOT emit duplicate
keys. **Receivers MUST ignore keys they don't know**: every wire addition in
this specification's history is an optional key (section 10).

There are three kinds, told apart by their keys:

| Kind | Recognised by |
|---|---|
| request | `cap` present and truthy (truthiness as defined below) |
| announce | no truthy `cap`, and `kind == "announce"` |
| reply | no truthy `cap`, and `id`, `ok` and `from` all present |

"Truthy" follows the reference (Python): `null`, `false`, `0`, `""`, `[]`
and `{}` are falsy; everything else is truthy. Anything else is foreign
chatter and MUST be ignored.

### 3.1 Requests

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "cap": "conformance.echo",
 "args": {"value": "hi"}, "target": "3f0a…worker_id", "identity": "agent:ci-runner",
 "ticket": {"v": 1, "…": "…"}}
```

| Key | Req. | Meaning |
|---|---|---|
| `id` | SHOULD | Caller-chosen string, unique per caller for the reply window (reference: 32 lowercase hex, uuid4). Echoed in the reply. Without an id the worker still answers, but nobody can match the reply. |
| `cap` | MUST | Capability name (4.1). |
| `args` | MAY | Keyword arguments as a JSON object. A falsy value (absent, `null`, `[]`, `false`…) means `{}`. |
| `target` | SHOULD | `worker_id` of the one node that should answer. Absent or falsy = open call: every node owning `cap` answers. |
| `identity` | MAY | Display identity of whoever the call is for (section 8). Self-asserted; never proof of anything. |
| `ticket` | MAY | Hub-signed call ticket (6.6). |

A worker handling a request MUST apply these rules in order (vectors:
`messages.json`):

1. If `target` is truthy and not its own `worker_id`: stay silent.
2. If it does not own `cap`: reply `unknown capability: <cap>` only if
   `target` equals its `worker_id`; otherwise stay silent (so an open call
   does not draw one error per worker).
3. If `args` (after the falsy-means-`{}` rule) is not an object: reply
   `args must be an object`.
4. If the arguments don't fit the cap's parameters (a missing required or an
   unknown argument): reply with an error starting `bad args: `.
5. Run the cap. Success: `ok: true` with the result (`null` if none).
   Failure: `ok: false` with an error string; the reference uses
   `"<ExceptionType>: <message>"`.

Workers SHOULD run requests concurrently, so a slow cap never stalls the
receive loop (a cap that itself calls another node would otherwise deadlock
waiting for a reply the loop can't deliver).

### 3.2 Replies

```json
{"id": "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", "from": "3f0a…worker_id", "ok": true, "result": {"value": "hi"}}
{"id": "5d1c…", "from": "3f0a…", "ok": false, "error": "unknown capability: nope.nothing"}
```

- `from` MUST be the replier's `worker_id`; `id` MUST echo the request's id
  when it had one and MUST be omitted otherwise.
- Exactly one of `result` (when `ok` is `true`) or `error` (a string, when
  `ok` is `false`).
- A denial by policy (6.7) is an ordinary failure with an extra `denied`
  object (`{"tier", "rule", "rev", "principal", "via"}`); worker-side
  refusals use `error` text starting `denied by worker: `.
- Normative error strings: `unknown capability: <cap>`,
  `args must be an object`, and the prefixes `bad args: `, `denied: `,
  `denied by worker: `. Other error text is free-form.
- The reply envelope's `ok` only says whether the cap ran. Caps that can
  decline report their own verdict inside `result` (for example
  `{"ok": false, …}` from `worker.ota_begin`).

### 3.3 Announces

Each worker MUST broadcast an announce on joining, after every transport
(re)connect, and periodically: every 30 s by default, with ±20 % jitter so
nodes started together de-phase.

```json
{"kind": "announce", "worker_id": "3f0a9c…", "name": "worker-a",
 "description": "build box in the rack", "caps": ["caps.describe", "shell.exec"],
 "plugins": ["caps", "shell"], "version": "167.brisk.otter", "build": 167,
 "app_release": {}, "facts": {"os": "linux", "arch": "x86_64", "cpus": 8},
 "tiers": {"caps.describe": "r", "shell.exec": "x"},
 "authz": {"v": 1, "mode": "audit", "anchors": ["<root kid>"], "kids": []},
 "hb": {"battery": {"pct": 81}}}
```

| Key | Req. | Meaning |
|---|---|---|
| `kind` | MUST | `"announce"`. MUST NOT carry `cap`. |
| `worker_id` | MUST | Stable node id; SHOULD survive restarts (the reference persists a uuid4 hex). |
| `name` | MUST | Human name, usually the hostname. `rook` is reserved (6.1). |
| `caps` | MUST | Sorted list of every cap the node answers, including `caps.describe`. |
| `description` | SHOULD | Operator-written role, ≤ 280 characters; data, not instructions. |
| `plugins` | SHOULD | Plugin namespaces loaded. |
| `version`, `build`, `app_release` | SHOULD | Build identity; `version` is `<build>.<adjective>.<noun>` for Rook builds. |
| `facts` | SHOULD | Self-reported hardware/platform facts (5.1). |
| `tiers` | SHOULD | `{cap: "r"|"w"|"x"|"a"}` for caps with a declared risk (4.3). |
| `authz` | MAY | Ticket-verification readiness (6.6). |
| `hb` | MAY | Tiny per-plugin live status keyed by namespace. |
| `grants`, `asig`, `ts`, `seq`, `revocations`, `roles`, `core_api` | MAY | Hub announces (6.4, 6.5). |

Announces SHOULD stay well under one datagram's worth of fragments; facts are
capped at 1024 bytes of compact JSON (5.1).

### 3.4 Rosters and eviction

A client keeps a roster keyed by `worker_id`, updated from announces
(`last_seen` = arrival time). Replies also count as signs of life. Clients
SHOULD evict a node not heard from for 90 s (three missed announces).
Clients MUST ignore their own announces echoed back and MUST resolve the
name `rook` only as described in 6.1.

A peer may join several bands (one PSK each) over one relay; rosters are
then per band, and a call goes to the band where its target was seen most
recently.

### 3.5 Calls and timeouts

A caller registers the request id, sends the request, and completes on the
first reply whose `id` matches; later replies with that id are dropped.
There is no protocol-level cancellation: **a call that timed out may still be
running**, so a caller MUST NOT blindly retry a cap with side effects (the
hub journal records late results).

- Default wait: 15 s. For a cap with a `timeout` parameter the reference
  waits for `args.timeout` (else the parameter's default from
  `caps.describe`) plus 5 s.
- Callers SHOULD bound pending calls (reference: 512 per band).
- Callers SHOULD always set `target`. An open call races every owner of the
  cap and completes on whichever answers first.

## 4. Capabilities

### 4.1 Names

A cap name is `<namespace>.<suffix>` where the namespace is the owning
plugin's (`shell`, `hub`, `chat`) and the suffix MAY contain further dots
(`shell.env.get`). Names are case-sensitive, non-empty, and unique per
node. Every node MUST implement `caps.describe`.

`cmd.*` is reserved for operator-defined command caps (always tier `exec`).
Namespaces used by Rook built-ins (Appendix A of `docs/design/permissions.md`)
SHOULD NOT be reused for different behaviour.

### 4.2 Arguments and `caps.describe`

Arguments are keyword arguments: a JSON object whose keys are parameter
names. A cap declares its parameters (name, required or default, type hint);
unknown keys and missing required keys are `bad args` errors (3.1).

`caps.describe(prefix = "")` returns, for every cap whose name starts with
`prefix`:

```json
{"conformance.add": {"doc": "Return a + b.",
                     "params": [{"name": "a", "required": true, "default": null, "type": "int"},
                                {"name": "b", "required": true, "default": null, "type": "int"}],
                     "risk": "read"}}
```

- `doc` is the first paragraph of the cap's documentation, on one line.
- `params` in declaration order; `default` is `null` when `required`;
  `type` is a free-form hint (Python annotation names in the reference) or
  `null`.
- `risk`, `tags`, `limit`, `fields`, `tool` appear only for caps that
  declare them (4.3, 4.4).
- Build-167 workers reject the `prefix` argument; clients talking to them
  filter on their side.

### 4.3 Risk tiers

Every cap has a tier, ordered `read < write < exec < admin` (letters
`r w x a`), and optional tags `sensitive`, `destructive`, `physical`.

The **effective tier** (vectors: `tiers.json`) is:

1. `cmd.*`: `exec`.
2. otherwise `max(builtin[cap], declared)`, where `builtin` is the table in
   Appendix A of `docs/design/permissions.md` (machine-readable in
   `tiers.json`) and `declared` is the cap's own risk (or its announce
   `tiers` entry). A node can raise its tier but never lower it.
3. A cap in neither: `exec`. Never assume a cap is harmless.
4. An operator override raises freely and lowers only when explicitly
   allowed.

Workers SHOULD declare a risk for every cap and MUST put declared tiers in
the announce `tiers` map.

### 4.4 Output contract: `limit` and `fields`

A cap MAY declare a default page size `limit` and a default projection
`fields` (`"*"` = projection supported, everything by default; or a list of
key names). The host (not the cap) then enforces (vectors:
`projection.json`):

- **limit.** If the handler takes a `limit` parameter, the host injects the
  default when the caller passes none. Otherwise the host removes `limit`
  from the arguments and trims the result to the caller's positive integer
  `limit`, else the default: a list is cut to that length; an object with
  an `items` list longer than that gets `items` cut, `truncated: true` and
  `total` (kept if present, else the original length).
- **fields.** Unless the handler takes `fields` itself, the host removes it
  from the arguments. The caller may pass a list, a comma-separated string,
  or `"*"`/an empty list (everything). Absent: the declared default applies.
  Projection keeps only the named keys of: an object; each object in a list;
  or, for an object carrying an `items` list, each item (the envelope keys
  stay). Scalars pass through.
- `limit` trimming happens before `fields` projection.

## 5. Node facts and placement

### 5.1 Facts

Facts are a flat JSON object of self-reported hardware and platform
properties, sent in announces:

| Fact | Type | Meaning |
|---|---|---|
| `os` | string | `linux`, `windows`, `darwin`, `android`, … |
| `arch` | string | `x86_64`, `aarch64`, … (lowercase) |
| `py` | string | Python version (reference workers only) |
| `cpus` | int | logical CPUs |
| `mem_gb` | number | total memory, GiB, one decimal |
| `pty`, `display`, `camera`, `embedded` | bool | capabilities of the machine |
| `gpu` | list | `[{"vendor", "name", "vram_gb"}, …]` |

- Implementations MAY add facts. Senders MUST drop `false` booleans (absent
  means false) and keep the compact JSON ≤ 1024 bytes (the reference drops
  `gpu` and long strings to fit).
- Receivers MUST drop fact keys starting with `is_` and the keys `role`,
  `roles`, `grants`: roles come from signed grants only (6.4). Facts gate
  placement; they grant nothing.
- Operators MAY override facts on a node (`ROOK_NODE_FACTS`, a JSON object
  merged over the detected facts, same key filter).

### 5.2 Placement expressions

A plugin's placement is a predicate over a node's facts (vectors:
`placement.json`). The language is a subset of Python expression syntax,
parsed, never `eval`ed:

```
expr     := or
or       := and ("or" and)*
and      := not ("and" not)*
not      := "not" not | cmp
cmp      := atom (cmpop atom)*            # chains: 2 < cpus <= 8
cmpop    := "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "not" "in"
atom     := NAME | STRING | NUMBER | "True" | "False" | "None"
          | "(" expr ")" | "(" [expr ("," expr)* [","]] ")" | "[" [expr ("," expr)*] "]"
          | "has" "(" STRING ("," expr)* ("," NAME "=" expr)* ")"
```

Anything else (attribute access, subscripts, arithmetic including unary
minus, other calls, `is`, conditionals, comprehensions, lambdas, `;`, dict
literals) MUST be rejected when the plugin is declared, and an invalid
expression evaluates to false.

Semantics:

- `any`, `anywhere`: true. `is_embedded`: the `embedded` fact's
  truthiness. Any other `is_<x>`: whether the node holds role `is_<x>`
  (6.4). Any other name: that fact, or `None` when absent.
- `has('f')`: fact `f` is truthy (for a list, any item is truthy).
  `has('f', cond…, key=value…)`: some truthy item of `f` (a list's items, or
  the value itself) satisfies every `cond` evaluated with the item's keys
  as names, and has every `key == value` (values evaluated at the top
  level).
- Comparisons follow Python: numbers compare numerically (`8 == 8.0`);
  ordering operators are false when either side is `None`; ordering mixed
  types (a string against a number) is an **evaluation error**; `in` means
  substring for strings, membership for lists, key membership for objects,
  and is false when the right side is `None`.
- `and`/`or` short-circuit left to right. Any evaluation error makes the
  **whole** expression false.

Placement also has a run mode: `all` (every matching node) or `one` (a
single node per band, chosen by the hub; honoured for hub placement only).
The default placement is `not is_hub` (workers only).

## 6. The hub worker `rook` and permissions

### 6.1 The reserved name

The hub appears on each of its bands as a worker named **`rook`**, serving
the hub-placed plugins (Appendix A.2 of `docs/design/permissions.md`: `hub.*`,
`chat.*`, `knowledge.*`, `task.*`, `policy.*`, `settings.*`, …). Its
`worker_id` is a persisted random id.

- A client MUST treat an announcer as `rook` only when the announce proves a
  valid `is_hub` grant for this band (6.5), checked against the client's
  trust anchors (the band's root public key).
- An announce naming itself `rook` without that proof MUST NOT be routable
  by the name `rook`. The reference lists it as `rook~<first 8 of
  worker_id>`, flags it `quarantined` and journals `audit.impostor`.
- A client with no trust anchor configured MAY fall back to the name, but
  MUST then treat anything it gets from `rook` as coming from an arbitrary
  band peer.

### 6.2 Keys

| Key | Signs | Trusted because |
|---|---|---|
| root (ed25519) | grants, OTA manifests, deauth orders, revocation lists | its public half is distributed to workers (baked into builds, `ROOK_UPDATE_PUBKEY`, `anchors.json`) |
| hub operational key (ed25519, rotated ~30 days) | call tickets, hub announces | a root-signed `is_hub` grant naming it |

`key_id(pub)` = the first 16 hex characters of SHA-256 over the raw 32-byte
public key. Public keys are written in standard base64, optionally prefixed
`ed25519:`.

### 6.3 Canonical JSON and signatures

`canonical(v)` is JSON with object keys sorted by Unicode code point, `,` and
`:` separators and no whitespace, and every character outside printable
ASCII escaped (Python `json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=True)`):

- `\"` `\\` `\b` `\f` `\n` `\r` `\t` as short escapes; other code points
  below U+0020, DEL (U+007F) and everything above U+007E as `\uXXXX`
  (lowercase hex; UTF-16 surrogate pairs above U+FFFF); `/` unescaped.
- Integers in decimal, within ±(2^53 − 1). Signed bodies contain no floats.
- Sort by code point, **not** by UTF-16 unit (they differ for keys above
  U+FFFF; `canonical.json` has a case).

A signed object is the body plus `sig` = base64(ed25519(key,
`PREFIX` ‖ canonical(body without `sig`))). Each object type has its own
prefix, and verifiers MUST accept an object only under its own prefix:

| Object | Prefix |
|---|---|
| grant | `rook-grant-v1\n` |
| call ticket | `rook-ticket-v1\n` |
| signed announce | `rook-announce-v1\n` |
| revocation list | `rook-revocations-v1\n` |
| deauth order v2 | `rook-deauth-v2\n` |
| OTA manifest v2 | `rook-manifest-v2\n` |
| root rotation | `rook-root-rotate-v1\n` |

`args_hash(args)` = base64url(SHA-256(canonical(args or {}))) without padding.

Vectors: `canonical.json`, `signatures.json` (test keys with published seeds).

### 6.4 Role grants

```json
{"typ": "rook-grant", "v": 1, "serial": "<32 hex>", "iss": "<root key_id>",
 "sub": {"key": "ed25519:<b64>", "kid": "<key_id of sub.key>", "worker_id": "<optional>"},
 "role": "is_hub", "name": "rook", "scope": {"bands": ["<band_id hex>"]},
 "constraints": {"max_tier": "admin"},
 "iat": 1790800000, "nbf": 1790800000, "exp": 1791404800, "sig": "<b64>"}
```

A verifier MUST check, in this order, and reject on the first failure:
`typ == "rook-grant"` and `v == 1`; `iss` is the key_id of a trusted root;
the signature under that root with the grant prefix; `nbf − 3600 ≤ now ≤
exp + 3600` (one hour of clock grace); `role` is known (v1: only `is_hub`);
the current band is in `scope.bands`; `serial` is not revoked;
`sub.kid == key_id(sub.key)`; `sub.worker_id`, when present, equals the
announcer's `worker_id`; an `is_hub` grant's `name`, when present, is
`rook`. Grants live 7 days and are renewed daily.

### 6.5 Signed announces (proof of possession)

A grant copied into someone else's announce proves nothing. A role holder
adds to its announce `grants` (up to 4 are considered), `ts` (unix seconds),
`seq` (increasing) and `asig = {"kid": <key_id of the grant key>, "sig":
<b64>}`, the signature being over `rook-announce-v1\n` ‖ canonical of
exactly:

```json
{"worker_id": …, "name": …, "caps": [...], "ts": …, "seq": …}
```

The announce **holds** a role when some grant verifies (6.4) and
`asig.kid == grant.sub.kid`, `|now − ts| ≤ 90`, and the signature verifies
under `grant.sub.key`. Receivers that learn keys from announces (workers
checking tickets) MUST also require `seq` to increase per key.

### 6.6 Call tickets

The hub attaches a ticket to targeted calls it sends (build-167 workers
ignore the key):

```json
{"v": 1, "kid": "<op key_id>", "p": "token:agent_1", "via": [], "cap": "shell.exec",
 "t": "<target worker_id>", "id": "<request id>", "ah": "<args_hash(args)>",
 "tier": "x", "rev": 42, "iat": 1790800000, "exp": 1790800030, "sig": "<b64>",
 "grant": {"…": "optional inline is_hub grant, not covered by sig"}}
```

A worker verifying a ticket MUST check: `v == 1`; `kid` names an op key it
holds a verified `is_hub` grant for (learned from a signed hub announce, or
from `grant` inlined in the ticket after verifying it); the signature over
the ticket minus `grant`; `t` equals its `worker_id`, `id` the request id,
`cap` the request cap, `ah` the args hash of the arguments as received;
`iat − 300 ≤ now ≤ exp + 300`; the ticket tier is not above the grant's
`constraints.max_tier`, nor below the cap's effective tier; and `id` is not
in its replay cache (message ids seen within 330 s, bounded at 10 000).

Worker enforcement modes (`ROOK_AUTHZ_MODE`): `off`; `audit` (default:
verify and log, never refuse); `enforce-admin`; `enforce-exec` (exec and
admin need a valid ticket); `enforce-all` (everything but `caps.describe`
and `info.ping`). A refusal replies `denied by worker: <tier> requires a hub
ticket (mode <mode>; <reason>)`. Workers SHOULD advertise
`"authz": {"v": 1, "mode", "anchors": [<root kids>], "kids": [<op kids>]}`
in announces; an implementation that does not verify tickets SHOULD
advertise `"mode": "off"`.

Floats in `args` are not portable through `ah` (canonical JSON has no float
rule); implementations MAY fail such tickets, and in `audit` mode that only
logs.

### 6.7 Calls from the band to `rook`

Band peers are authenticated only by the PSK, so the hub treats every
band-originated call as principal `band:unauthenticated`, whatever
`identity` says:

- The hub serves them only up to its **band risk ceiling**
  (`ROOK_HUB_BAND_MAX_RISK`, default `read`); above it the reply is
  `<cap> is not callable over the band on the hub (risk above 'read'); call it through the MCP bridge`.
- Policy evaluates them (default rule: read allowed, write/exec/admin
  denied in `enforce` mode, journaled as `would_deny` in `audit` mode).
- Hub administration caps (`policy.set`, `settings.set`) additionally refuse
  any caller that isn't a band owner, operator token or in-process hub code.

Authenticated callers (agents with MCP tokens, dashboard users) reach the
same caps through the MCP bridge (`rook_call(worker="rook", …)` and the
tools listed in `skills/rook`).

## 7. Chat rooms

Persistent rooms shared by agents, people and workers, stored by the hub and
reachable over the band as caps on `rook` and over MCP as `rook_chat_*`.

### 7.1 Model

| Object | Fields |
|---|---|
| room | `room` id (16 hex; also the thread id shared with journal and handoff records), `title` (≤ 200 chars), `participants` (ordered identity list), `last_activity` |
| message | `seq` (increasing integer, unique per hub), `ts` (unix seconds, float), `sender`, `text` (1..8000 chars after trimming), `mentions` (identities), `expects_reply` (bool) |
| read watermark | per (identity, room): the highest `seq` read; `unread` = later messages not sent by that identity |
| presence | per identity: last time seen (any call); online = seen within 90 s |

Semantics:

- A sender who is not yet a participant becomes one.
- **Mentions are routing metadata**, not text: mentioning a non-participant
  invites them. In a room of exactly two participants, a message addresses
  the other one implicitly; otherwise only the mentioned are addressed.
- A send reports `addressed` and which of them are `offline`. Delivery is
  voicemail: an offline agent learns of unread messages on its next MCP call.
  Waking an agent is a separate act (`rook_chat_wake`, `agent.wake`).
- Only a participant may delete a room; deletion is final.
- Rooms are never expired; they sort by `last_activity`.

### 7.2 Operations on `rook`

| Cap | Tier | Arguments | Result (reply `result`) |
|---|---|---|---|
| `chat.read` | read | `action="rooms"`, `limit=200` | `{"count", "rooms": [{"room", "title", "last_activity_age_secs", "participants", "member", "unread", "last_sender", "last_text"}]}` |
| `chat.read` | read | `action="read"`, `room`, `since_seq=0`, `limit=200`, `mark=true` | `{"room", "title", "participants", "messages": [message…], "last_seq"}`: messages with `seq > since_seq`, oldest first; marks them read unless `mark=false` |
| `chat.write` | write | `action="start"`, `title`, `invite` (list or comma string) | `{"room", "title", "participants"}` |
| `chat.write` | write | `action="send"`, `room`, `text`, `mentions`, `expects_reply=false` | `{"room", "participants", "mentioned", "addressed", "offline"}` |
| `chat.delete` | write (destructive) | `room` | `{"room", "title", "messages_deleted"}` |
| `chat.presence` | read | | `{"agents": [{"identity", "last_seen_age_secs", "online"}]}` |

Errors (no such room, empty message, not a participant, unknown action) are
ordinary failures (`ok: false`). Pass the reply's `last_seq` as the next
`since_seq` to poll a room.

### 7.3 Attribution

Chat records the caller's identity as `sender` and uses it for membership and
read watermarks. For band-originated calls (6.7) the hub records
**`band:<identity>`** (or `band:anonymous`), so a band peer can never post as
a person or a token-attributed agent; callers through the MCP bridge keep
their token identity. Posting over the band needs the operator to raise the
band risk ceiling to `write` (`chat.write` is tier write).

Example, a band peer posting:

```json
→ {"id": "a1…", "cap": "chat.write", "target": "<rook worker_id>", "identity": "agent:ts-worker",
   "args": {"action": "send", "room": "4c155f9385b54ff1", "text": "build is green"}}
← {"id": "a1…", "from": "<rook worker_id>", "ok": true,
   "result": {"room": "4c155f9385b54ff1", "participants": ["agent:claude", "band:agent:ts-worker"],
              "mentioned": [], "addressed": ["agent:claude"], "offline": []}}
```

## 8. Identity, journal and audit

Identities are strings `<kind>:<name>`: `agent:<token name>` (MCP tokens),
`human:<user>` (dashboard accounts), `system:<component>` (hub internals,
e.g. `system:rook-hub`), `band:<identity>` (unauthenticated band callers as
recorded by the hub). The envelope `identity` is a display breadcrumb: the
hub stamps it from the verified token on calls it sends, and workers record
it, but nothing on the band authenticates it (section 12).

- **Hub journal.** Every call sent through the MCP bridge (and every band
  call to `rook`) is journaled: call id, time, identity, cap, worker,
  thread id, ok, error, the full reply (size-capped ring), and for
  permissions: principal, decision (`allow|deny|would_deny|error_allow|error_deny`),
  rule, policy revision and tier. `rook_journal` queries it.
- **Worker audit.** Workers SHOULD keep a local append-only record of every
  dispatch: time, cap, identity, a redacted summary of args (values of
  arguments named like `password`, `secret`, `api_key`, `*_token` replaced),
  ok/error, request id, target, and ticket verdict
  (`{verified, reason, kid, p}`). The reference keeps a size-capped JSONL
  ring readable through `log.audit`.

## 9. The plugin contract

*For hosts written in other languages that want Rook-compatible plugins.
Hosts that only need to be workers can skip this section: a worker is
correct if its wire behaviour (sections 2–4) is.*

A plugin is a unit with a manifest and caps. Manifest (as reported by
`hub.plugins` and `worker.plugin.list`):

| Field | Meaning |
|---|---|
| `name` | plugin name (defaults to its module) |
| `namespace` | cap namespace; caps are `<namespace>.<suffix>` |
| `version` | `<build>.<adjective>.<noun>`; defaults to the host build |
| `core_api` | range of core API versions supported: `">=1.0,<2"`, `"==1.1"`, bare major `"1"` (= `>=1.0,<2`); comma = and; operators `>= <= == != > <`; empty = any; unparsable = incompatible (vectors: `core_api.json`) |
| `placement` | `{"where": <expression or null>, "run": "all"|"one"}` (5.2) |
| `caps` | sorted cap names |
| `settings` | settings schema (below), optional |
| `migrations` | migrations directory, optional |
| `guidance` | operator-editable guidance slots, optional |
| `skill` | whether it contributes to the agent skill document |
| `panel` | optional web panel `{"title", "path"}` |
| `depends` | namespaces that must load first on the same node (core API 1.1) |

The current core API is **1.1** (1.1 added `depends` and wiring settings
before the availability check). Minor versions are additive; a major bump
breaks.

**Lifecycle.** A host discovers plugins, then for each: checks `core_api`
compatibility; checks placement against the node's facts (and election for
`run="one"`); wires settings, resources, data dir and dependencies; asks
`available()` (a plugin whose backend is missing declines and announces no
caps); registers caps (a name collision fails that plugin only); calls
`start()`. `stop()` on shutdown, `heartbeat()` on every announce (a tiny
object merged under `hb.<namespace>`). A plugin that fails at any step is
recorded as failed and every other plugin still loads. Plugins waiting on
`depends` load after the others; if a dependency never loads they are
`unavailable`.

**Settings.** Each setting declares `name`, `type`
(`str|int|float|bool|list|dict|resource`), `default`, `scope`
(`hub|band|worker|user`), `secret` (value lives in the vault under
`plugin.<namespace>.<name>`, never in settings storage), `env` (override
variable), `label`, `help`, `choices`. Resolution: environment override,
then the stored value (or the vault for secrets), then the default; a value
that fails validation falls through to the next source with a warning.
Booleans accept `1/true/yes/on` and `0/false/no/off/""`; `list`/`dict`
accept JSON text.

**Resources** are settings of type `resource`, connection strings
(vectors: `resources.json`): `cap://<worker name|id|any>/<cap>` (a band
call; `any` = the first live holder by name), `http(s)://…`,
`sqlite:///path`, `file:///path`. An empty string means not configured.

**Migrations.** A plugin with state in SQLite ships files
`NNN_description.sql`; each is applied once, in numeric order, inside a
transaction, recorded in `_rook_migrations(namespace, version, name,
applied)`. Two files with one number are an error; a failing file rolls back
and stops the run; released files are never edited.

**State.** Each plugin gets a private data directory
`<node state>/plugins/<namespace>`.

## 10. Versioning and compatibility

- This document is versioned `MAJOR.MINOR`. Minor revisions only add
  optional behaviour and keys; every vector file names the spec version it
  was generated for (`"spec": "1.0"`).
- **Baseline: build 167.** A conforming implementation MUST interoperate
  with build-167 workers and clients, which read only `id`, `cap`, `target`,
  `args`, `identity` from requests, only the documented announce keys
  (with `.get`, ignoring the rest), and drop any non-request message.
- **Wire changes are additive.** New keys MUST be optional and ignorable;
  existing keys MUST NOT change meaning or type; normative error strings
  MUST NOT change; new message kinds MUST NOT carry `cap` (old workers
  treat anything with `cap` as a request) and SHOULD carry `kind`.
- Keys added since build 167, all optional: request `ticket`; announce
  `facts`, `tiers`, `authz`, `grants`, `asig`, `ts`, `seq`, `revocations`,
  `roles`, `core_api`; reply `denied`; deauth `v2`; `caps.describe` `prefix`
  argument and `risk/tags/limit/fields/tool` keys.
- Relay and transport changes follow the telesthete protocol's own
  versioning; the Rook profile (empty AAD, CHANNEL 0, fragmentation v1) is
  frozen for spec 1.x.

## 11. Conformance

| Profile | Requires | Checked by |
|---|---|---|
| **wire** | sections 2 and 3.2–3.3 framing | `band_crypto`, `fragments`, `frames` vectors |
| **worker** | wire + 3.1 dispatch rules, 3.3 announces, 4.1–4.2 | `messages` vectors; harness announce/call/error/silence checks |
| **client** | wire + 3.4–3.5, 6.1 resolution of `rook` | harness chat checks (the candidate calls `rook`) |
| **authz verifier** | 6.3–6.6 | `canonical`, `signatures`, `tiers` vectors; harness "verified rook's is_hub grant" |
| **host** | 4.3–4.4, 5, 9 | `projection`, `placement`, `core_api`, `resources` vectors |

`conformance/README.md` describes the vector formats, the candidate contract
(environment variables and the `conformance.*` caps) and how to run the
harness. Both example ports pass every vector and the full live run.

## 12. Security considerations

- **The PSK is band membership.** Anyone holding it can read all traffic,
  announce anything, and call any worker cap; a worker in `audit` mode runs
  such calls. Grants, tickets and enforce modes (section 6) are how a band
  narrows that; until workers enforce, protection lives at the hub.
- **Identity is self-asserted on the band.** Never make an authorization
  decision from the envelope `identity`. The hub prefixes band callers'
  identities with `band:` wherever it records them for others to read
  (chat).
- **No transport replay protection.** The Rook profile does not track
  sequence numbers, so anyone on the network path can re-send a captured
  datagram, and a worker in `off`/`audit` mode will execute the call again.
  Tickets carry a replay cache; `enforce-*` modes close this for the tiers
  they cover. Don't expose a relay to untrusted networks without that.
- **Header not authenticated.** With empty AAD the `channel_type`,
  `channel_id` and `band_id` bytes are not covered by the tag. Changing
  them only misroutes or drops frames (receivers use CHANNEL 0 only, and
  the key is per band), but implementations MUST NOT give those bytes any
  other meaning.
- **Nonce reuse is catastrophic.** Reusing a sequence under the band key
  leaks plaintext and allows forgery; follow 2.3 exactly.
- **Facts can lie.** They only choose where plugins run and which workers a
  selector matches; never let them grant anything, and don't send secrets
  to a target matched by facts alone.
- **Relay exposure.** The relay learns peer addresses and traffic volume
  per band id, nothing else.

## 13. Appendix: wire examples

A request, as sent (JSON, 83 bytes):

```json
{"id": "7f0c", "cap": "conformance.echo", "args": {"value": "hi"}, "target": "w-1"}
```

On band PSK `conformance-band-psk` (band_id `e17904e6ed4c664592f64f6f1d02c682`),
with fragment id `133379ce1b805f38e2e8dc5a121f9fd5` and sequence 4097, it is
one 147-byte datagram (vectors: `frames.json`, case "call"):

```
e17904e6ed4c664592f64f6f1d02c682  band_id
02                                channel_type CHANNEL
0000                              channel_id 0
0000000000001001                  sequence 4097
0a88467445…f3498bb6               ChaCha20-Poly1305(key, nonce(4097), "",
                                    01 ‖ 133379ce…9fd5 ‖ 0000 ‖ 0001 ‖ request JSON) ‖ tag
```

The reply:

```json
{"id": "7f0c", "from": "w-1", "ok": true, "result": "hi"}
```

A reply larger than 1003 bytes (`frames.json`, case "large reply") becomes
three datagrams with one fragment id, chunk indexes 0..2 of total 3, and
three consecutive sequences.
