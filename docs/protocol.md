# Rook protocol v1

The protocol is a set of messages (W1). Anything that sends and answers them is a worker, hardware or software. This document defines the messages and nothing else. How the hub stores them, and what it does with them, belongs to the hub.

Test vectors: [`protocol/vectors.json`](../protocol/vectors.json). Python library: [`protocol/python/rook_protocol.py`](../protocol/python/rook_protocol.py).

## Carriage

- **Knock** (`knock`, and its reply `knock_reply`, or an `error`) travels as one HTTPS `POST` to `<hub URL>/knock`: the request body is the knock, the response body is the reply. A worker that has not been approved shares no key with the hub, so it cannot reach the hub over Telesthete yet ([telesthete-surface.md](telesthete-surface.md), answer c). The hub URL is the only input the worker needs (R1, U2).
- **Every other message** travels over the worker's link to its hub, one message per link message. On Telesthete that is one Channel message (SPEC §6, §6.6), which already preserves message boundaries, so Rook adds no length prefix. How the link is keyed per worker is open ([telesthete-surface.md](telesthete-surface.md), "Where PLAN.md and Telesthete disagree", point 1). The messages do not depend on it.

## Encoding

A message is a JSON object (RFC 8259) encoded as UTF-8, with a string field `type` naming the message.

**Why JSON:** every language a worker is written in, an ESP32 included, already has a JSON parser, and messages stay readable in the journal and in logs.

- Encoders emit the canonical form: object keys sorted, no whitespace between tokens, non-ASCII characters written as UTF-8 rather than `\u` escapes, no `NaN` or `Infinity`. The vectors check the canonical form byte for byte. Decoders accept any valid JSON.
- Bytes (`stream_data.data`) are standard base64 with padding (RFC 4648, section 4).
- Integers are JSON numbers without a fraction. `true` and `false` are not integers.
- Times and sizes: seconds and bytes.

## Versions (U8)

- `knock`, `knock_reply`, `heartbeat` and `heartbeat_reply` carry `protocol`, an integer. This document is version **1**.
- A receiver ignores fields it does not know, on every message and in every block. Adding an optional field therefore needs no new version. Removing a field, changing its meaning, or adding a message type that a worker must answer does.
- The hub records the version each worker announces and sends that worker only the messages and fields of that version. A hub keeps speaking every older version, so a worker that lags behind its hub still connects.

## Minimum worker (W2)

A worker must implement `knock`, `knock_reply`, `heartbeat`, `heartbeat_reply`, `call`, `result` and `error`. That is register, heartbeat and answer calls. The three `stream_*` messages are optional: a worker that announces no cap returning a stream never sees them. `event` is optional too, and so is `plugins`: the hub sends it only to worker forms it pushes plugin worker parts to (today `python`).

## Shared blocks

### Cap (W3, S1)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | string | yes | Dot-separated lowercase segments of `a-z` and `0-9`; a hyphen only inside a segment (`shell.exec`, `wake-on-lan.send`). |
| `description` | string | yes | One line. |
| `schema` | object | yes | JSON Schema for `call.args`. |
| `returns` | `"result"` or `"stream"` | yes | Whether a call returns a result or opens a stream (S1). |

### Health (H4)

Every field is optional; a device fills what it can. Sizes are objects `{"total": int, "free": int}` in bytes.

| Field | Type | Meaning |
|---|---|---|
| `cpu` | number | CPU busy, percent. |
| `ram` | size | Memory. |
| `disk` | size | Disk holding the worker's state. |
| `gpu` | number | GPU busy, percent. |
| `vram` | size | GPU memory. |
| `npu` | number | NPU busy, percent. |
| `battery` | number | Charge, percent. |
| `temperature` | number | Degrees Celsius. |
| `heap_free` | integer | Free heap, bytes (ESP32). |
| `uptime` | number | Seconds since the device started. |

## Messages

### `knock`: worker to hub, over HTTPS (R2, R4, R5)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `protocol` | integer | yes | Version (U8). |
| `name` | string | yes | The name the worker offers, normally its hostname. Shown on the pending view (R4). |
| `key` | string | yes | The public half of the keypair the worker generated: an Ed25519 public key, 64 lowercase hex characters (R5). |
| `macs` | list of string | yes | The worker's own MAC addresses, used to recognise a known identity (W7). May be empty. |
| `caps` | list of cap | yes | The caps it offers, shown read-only before approval (R4). |
| `token` | string | no | A join token (a JWT the hub minted with the `join` scope). A good one approves the knock at once, with no pending step, and the worker's owner is the user the token belongs to (R3, W6). Added in v1 as an optional field (U8). |

A worker that has not been approved repeats the same knock, with the same key, until the reply says `approved`. A join token is spent by its first use; the repeats that follow are answered from the approved key, not the token.

### `knock_reply`: hub to worker, the HTTPS response (R2, R5, W7)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `protocol` | integer | yes | The hub's version (U8). |
| `status` | `"pending"` or `"approved"` | yes | `pending` until a person or agent approves the key, or attaches it to a known identity. |
| `guid` | string | when approved | The worker's identity, minted at first approval (W7, G1). |
| `moniker` | string | when approved | Its unique three-word moniker (W7, G1). |
| `signature` | string | when approved | The hub's Ed25519 signature over the worker key, hex (R5). |
| `hub_key` | string | when approved | The hub's Ed25519 public key, 64 lowercase hex characters (R5, U9). |

The signed bytes are the ASCII text `rook-worker-key:` followed by the 32 raw key bytes. Before keeping anything, the worker verifies `signature` with `hub_key`; if it does not verify, the worker keeps nothing. It then stores `guid`, `moniker`, `signature` and `hub_key` in its state with its key (W7, U2). The hub key is what it will later check signed builds with (U9).

A knock with a revoked key, one that is not valid, or one whose token is refused gets an `error` instead, and nothing is approved or left pending.

### `heartbeat`: worker to hub (H1, H4, S4)

Connecting is the first heartbeat. The worker sends one at the interval the hub gave it, and immediately whenever its state changes (H1).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `protocol` | integer | yes | Version (U8). |
| `identity` | object | yes | `name` (string) and `key` (string, as in `knock`). The key must be the one the link authenticated. |
| `facts` | object | yes | `os`, `arch`, `hostname`, `form` (the kind of worker, for example `python` or `esp32`) and `version`, all strings; `macs`, a list of strings (W7). |
| `caps` | list of cap | yes | Everything the worker has (W5). |
| `band` | object | yes | `gateway_mac`: the MAC address of the worker's default gateway, or `null` when it has none, as on cellular (B2). This is the band fingerprint. |
| `health` | health | yes | See above. May be `{}`. |
| `streams` | integer | no | Number of open streams (S4). Workers without streams leave it out. |
| `plugins` | list of object | no | The plugin worker parts it holds (PL3), each `{name, version}` as the `plugins` message gave them, with `error` (string) for one that failed to load. Workers that take no plugin parts leave it out. Added in v1 as an optional field (U8). |

### `heartbeat_reply`: hub to worker (H2)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `protocol` | integer | yes | The hub's version (U8). |
| `interval` | integer | yes | Seconds until the next heartbeat. Set by the hub per worker. |

The hub answers every heartbeat. A worker that misses three intervals is shown offline (H3).

### `call`: hub to worker (C3, W5)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `id` | string | yes | Chosen by the hub; unique among the calls open on this link. |
| `cap` | string | yes | Cap name. |
| `args` | object | yes | Arguments, per the cap's schema. |

The worker answers with exactly one `result`, `error` or (for a stream cap) `stream_open` carrying the same `id`.

### `result`: worker to hub

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `id` | string | yes | The call's `id`. |
| `result` | any JSON | yes | What the cap returned. |

### `error`: either direction (C3, W5)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `id` | string | no | The call it answers, when there is one. |
| `code` | string | yes | Short machine-readable reason. |
| `message` | string | yes | What went wrong, in a sentence. |
| `next` | string | yes | What to try next, in a sentence (C3). |

The hub hands the same message back to whoever made the call. Codes in use:

| Code | Sent by | When |
|---|---|---|
| `unknown_cap` | worker or hub | The worker does not announce or serve that cap. |
| `cap_failed` | worker | The cap ran and failed. |
| `unknown_worker` | hub | No worker has that GUID or moniker. |
| `worker_offline` | hub | The worker is not connected. |
| `worker_disconnected` | hub | The worker's link closed before it answered. |
| `unknown_key` | hub | The key was never approved or attached. |
| `key_revoked` | hub | The key was revoked, for example by a rekey (W7). |
| `bad_message` | either | The message could not be decoded or validated. |
| `token_invalid` | hub | The knock's token is not one this hub minted. |
| `token_expired` | hub | The knock's token is past its lifetime. |
| `token_spent` | hub | The knock's join token was already used. |
| `token_revoked` | hub | The knock's token, or its user, was revoked. |
| `token_scope` | hub | The knock's token is not a join token. |

New codes may be added without a version change; receivers treat an unknown code like any other error.

### `stream_open`: worker to hub, answers a call on a stream cap (S1, S3)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `id` | string | yes | The call's `id`. Opening a stream is a normal cap call (S1). |
| `stream` | string | yes | Stream handle, chosen by the worker, which owns the stream (S2). Unique on that worker. |
| `title` | string | yes | Free text (S3). |
| `description` | string | yes | Free text (S3). |

### `stream_data`: either direction (S1)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `stream` | string | yes | Stream handle. |
| `data` | string | yes | Bytes, base64. Worker to hub is output; hub to worker is input. |

### `stream_close`: either direction (S1, S4)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `stream` | string | yes | Stream handle. Streams live until one side closes them on purpose (S4). |

### `event`: worker to hub, optional (J2)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | string | yes | The event's name, which jobs match on. |
| `payload` | object | yes | The event's data. The hub adds `worker` (the worker's GUID) and `worker_moniker`, replacing any the worker sent. |

A worker sends it whenever something happens that jobs may care about, for example from inside a cap. The hub puts it on its event bus and answers nothing. Added in v1 as an optional message (U8): a worker that never sends it is unaffected.

### `plugins`: hub to worker, optional (PL3)

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `plugins` | list of object | yes | Every plugin worker part this worker should have now, in the order to load them. Each is `name` (the plugin's name), `version` (its version, free text) and `source` (the part's code, as text: for the form `python`, a Python module). May be empty. |

The list is the whole state, not a change. The worker drops what is no longer listed or whose version or source changed, loads what is new, and then heartbeats with its caps (W5) and its `plugins` report, which carries an `error` for each part that failed to load. It answers nothing else. The hub sends it when a worker connects (unless the worker holds no parts and should have none) and again whenever that worker's list changes (a plugin is installed or replaced, or the worker's opt-out changes). Added in v1 as an optional message (U8): the hub sends it only to the worker forms it pushes plugin parts to, which is `python` today.

## Sequences

First contact, approval, then the link (R2, R5, H1, H2):

```
worker                                   hub
  | POST /knock  knock{key K}              |   K is pending (R4)
  |<-- knock_reply{pending} ---------------|
  |          ... person or agent approves K ...
  | POST /knock  knock{key K}              |
  |<-- knock_reply{approved, guid, moniker, signature, hub_key}
  |    worker verifies signature with hub_key, then stores its identity
  | link (keyed by K)                      |
  |--- heartbeat ------------------------->|   connect = first heartbeat
  |<-- heartbeat_reply{interval} ----------|
  |<-- call{id, cap, args} ----------------|
  |--- result{id, result} ---------------->|
```

A restart reuses the stored key and identity and goes straight to the link (R5).
