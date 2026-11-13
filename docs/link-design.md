# The worker-hub link: design

**Status: design only, for Bake to sign. Nothing here is built.** Written 13 November 2026 for brief 26.

Rook core has no real link between a worker and its hub yet: the tests carry protocol v1 messages over in-memory links (`tests/memory_link.py`). This document says how a worker and a hub will talk over Telesthete, what each side does, what changes in `docs/protocol.md`, and in what order it is built.

Sources, all read in full for this design: `PLAN.md` (Layering, Topology, W1 to W7, H1 to H5, C and S lines, R1 to R5, T1 to T6, U1 to U9, A1 to A3, Explicitly out), `docs/protocol.md`, `protocol/python/rook_protocol.py`, `core/python/rook_worker.py`, `Hub.serve_link` and the knock route in `hub/rook_hub/`, `tests/memory_link.py`, and the reference copies of the approved Telesthete design (`telesthete-terminating-endpoints.md`, called "the Telesthete design" below, with its section numbers), the Telesthete wire spec (`telesthete-SPEC.md`, "SPEC §"), the hand-over note to Rook (`telesthete-handover-for-rook.md`, "hand-over §") and the replay reproduction (`replay-after-restart.py`).

## Contents

1. [Fixed points and the one proposal](#1-fixed-points-and-the-one-proposal)
2. [The exchange that makes a connection's key](#2-the-exchange-that-makes-a-connections-key)
3. [The life of a connection](#3-the-life-of-a-connection)
4. [How Rook's messages ride the link](#4-how-rooks-messages-ride-the-link)
5. [The hub side](#5-the-hub-side)
6. [The worker side, per form](#6-the-worker-side-per-form)
7. [Unattended joins and the dongle](#7-unattended-joins-and-the-dongle)
8. [What Rook needs from Telesthete, and when](#8-what-rook-needs-from-telesthete-and-when)
9. [How it will be proven](#9-how-it-will-be-proven)
10. [Build plan](#10-build-plan)
11. [Changes to docs/protocol.md, exactly](#11-changes-to-docsprotocolmd-exactly)
12. [What this leaves room for](#12-what-this-leaves-room-for)
13. [Decisions needed](#13-decisions-needed)
14. [What I checked, and what I could not verify](#14-what-i-checked-and-what-i-could-not-verify)

---

## 1. Fixed points and the one proposal

Bake has ruled these. The design builds on them and does not reopen them:

- **The hub is its own Telesthete terminating endpoint.** It is one process, with no relay and no attachment (Bake 13 Nov; Topology). Every worker's band has two members, the worker and the hub (Telesthete design §5.1).
- **Keys are Rook's.** Telesthete carries frames under a key the application supplies. Deriving keys and deciding which are accepted is Rook's job (Layering; Bake 13 Nov).
- **A worker has a separate X25519 key for key agreement.** Its public half goes in the knock, signed by the worker's identity key (Bake 13 Nov: "Separate key.").
- **Humans log in with a password and never type tokens** (T3, T4).

**The proposal the design is built for: a fresh key for every connection.** This is not yet confirmed by Bake, and it is decision D1 in section 13.

Telesthete does not protect a receiver that restarts against a replay of a session recorded before the restart. Its replay defences live in the receiver's memory, and its data key depends on nothing the receiver contributes (Telesthete design §7.6; SPEC §3.3 note in the addendum; the reproduction). A worker acts on what it receives, so a replayed `call` would run a command again.

So Rook never puts a long-lived key under its messages. Each connection's key is made new during an exchange over HTTPS, from the two sides' long-term keys plus something fresh from each side. Neither side keeps a session key after it restarts, so neither can accept an earlier key.

---

## 2. The exchange that makes a connection's key

### 2.1 Which request carries it

It is a **new HTTPS request after approval**, `POST <hub URL>/link`, made **every time the worker connects**: at start, and whenever it loses the link. It is not the knock. Why:

- The knock happens once per identity: on first start, and again only on a rekey (R5, W7). Every connection needs its own exchange.
- Anyone may knock (R2), but only an approved key may make a session. Keeping the two apart keeps each check simple.
- The knock already travels as HTTPS to the hub URL, the only input a worker has (R1, protocol.md "Carriage"). `/link` uses the same path, so a worker that can knock can make a session.

The request is a new message, `link_open`; the response is `link_accept`, or an `error`. Both are JSON, encoded as protocol v1 encodes every message (`docs/protocol.md`, "Encoding").

### 2.2 The keys involved

| Key | Who holds the secret | Lifetime | Where the public half travels |
|---|---|---|---|
| Worker identity key (Ed25519) | worker (state, W7) | until rekey | `knock.key` (exists) |
| Worker link key (X25519) | worker (state, next to the identity key) | until rekey | `knock.link_key`, signed by the identity key (new; Bake's ruling) |
| Hub key (Ed25519) | hub (`hub.key` in its data directory, exists) | the hub's life | `knock_reply.hub_key` (exists); the worker keeps it (U9) |
| Hub link key (X25519) | hub (new file `hub_link.key` in its data directory, 0600) | the hub's life | `link_accept.hub_link_key`, signed by the hub key |
| Worker ephemeral (X25519) | worker, in memory | one exchange | `link_open.ephemeral` |
| Hub ephemeral (X25519) | hub, in memory | one exchange | `link_accept.ephemeral` |

The ephemeral secrets are erased as soon as the session key is computed. Neither side ever writes a session key or an ephemeral secret to disk.

### 2.3 Fields

`knock` gains two fields (exact text in section 11):

| Field | Type | Meaning |
|---|---|---|
| `link_key` | string, 64 lowercase hex | The worker's X25519 public key. |
| `link_signature` | string, 128 lowercase hex | Ed25519 signature by the identity key (`knock.key`) over the ASCII text `rook-link-key-v1:` followed by the 32 raw bytes of `link_key`. |

The hub checks `link_signature` before it keeps a knock. It stores `link_key` with the knock, and with the key once approved: the `keys` table gains a column.

`link_open`, worker to hub, the body of `POST /link`:

| Field | Type | Meaning |
|---|---|---|
| `protocol` | integer | Version (U8). |
| `key` | string, 64 hex | The worker's identity key, as approved. |
| `ephemeral` | string, 64 hex | A new X25519 public key, made for this exchange only. |
| `signature` | string, 128 hex | Ed25519 signature by the identity key over `rook-link-open-v1:` ‖ hub key (32 bytes, as the worker stored it at approval) ‖ `key` (32) ‖ `ephemeral` (32). |

`link_accept`, hub to worker, the HTTPS response:

| Field | Type | Meaning |
|---|---|---|
| `protocol` | integer | The hub's version. |
| `ephemeral` | string, 64 hex | The hub's new X25519 public key for this exchange. |
| `hub_link_key` | string, 64 hex | The hub's X25519 public key. |
| `udp_port` | integer or null | The UDP port the hub serves the link on, at the hub URL's host; null when it serves none. |
| `signature` | string, 128 hex | Ed25519 signature by the hub key over `rook-link-accept-v1:` ‖ `key` (32) ‖ the worker's `ephemeral` (32) ‖ this `ephemeral` (32) ‖ `hub_link_key` (32). |

The WebSocket URL is not sent. It is the hub URL with `https` replaced by `wss` (or `http` by `ws`) and path `/band`, the path Telesthete servers SHOULD use (Telesthete decision 7). The worker derives it.

**Why each signature:**

- **On `link_key` in the knock (ruled).** It shows that the holder of the identity key chose this link key. Without it, anyone in the knock's path could swap the link key.
- **On `link_open`.** A session changes state at the hub: it adds a key, and it will replace the live session when used. Only the holder of the identity key may cause that. The hub key is inside the signed bytes, so a request signed for one hub is worthless at another.
- **On `link_accept`.** The worker learns the hub's ephemeral and link key from a source it can check (the hub key it kept at approval), whatever happens to TLS. The signed bytes include the worker's own ephemeral, so an old `link_accept` never answers a new request.

### 2.4 The derivation

Both sides compute, the worker as shown and the hub with the roles swapped:

```
ee  = X25519(worker ephemeral secret, hub ephemeral public)      32 bytes
ss  = X25519(worker link secret,      hub link public)           32 bytes
      reject if either is all zero
okm = HKDF-SHA256(salt = "rook-link-v1",
                  ikm  = ee || ss                                 64 bytes
                  info = key || worker ephemeral || hub ephemeral 96 bytes (raw public keys)
                  L    = 32)
psk = lowercase hexadecimal of okm                                64 ASCII characters
```

Telesthete then takes `psk` as an ordinary PSK. Its `band_id` is `SHA256(psk)[:16]` (SPEC §3.1), and its base and data keys follow from it as for any band.

Why each step:

- **X25519 ephemerals on both sides.** Each side contributes something fresh, so no session key is ever made twice. The ephemerals also give forward secrecy (2.6) at no extra round trip. They replace the 32-byte nonces of the hand-over's sketch (hand-over §1.7), which would have done the freshness part only.
- **`ss`, from the two long-term link keys (Bake's ruling).** Only the true worker and the true hub can compute it. A slip in a signature check therefore still yields a key nobody else can derive. It is the one use of the ruled X25519 key: the signatures alone already authenticate the ephemerals.
- **Fixed order, no sorting.** Roles are fixed (the worker always opens), so `ikm` and `info` put the worker first. The hand-over's sorting was for a role-free derivation.
- **`info` binds the session to this identity and these two ephemerals,** so a key derived for one exchange can never match another.
- **The salt names Rook and a version.** It separates this use of X25519 output from any other, and a later derivation is `rook-link-v2`.
- **Hex string.** Telesthete's Python API takes `psk: str` and its Rust API takes bytes (hand-over §1.2). ASCII hex is the same bytes in both, and on an ESP32.
- **Primitives:** X25519 (RFC 7748), Ed25519 (RFC 8032), HKDF-SHA256 (RFC 5869). PyNaCl, which Rook already depends on, provides all three apart from HKDF. HKDF-SHA256 is a few lines over `hmac`, or `cryptography` if Bake prefers a library (section 10, PR L1).

### 2.5 What each side must check

**The hub, on `link_open`:**

1. The message is valid (protocol library).
2. `key` is approved and not revoked. Otherwise it answers `unknown_key` or `key_revoked`, as the knock does, and keeps nothing.
3. `signature` verifies under `key`. Otherwise `bad_signature` (a new code), and it keeps nothing.
4. `ephemeral` is 32 bytes. `ee` and `ss` are not all zero (an all-zero result means a small-order point). Otherwise `bad_message`.

Then it computes `psk`, adds the key to its Telesthete `Endpoint` as this worker's **pending session**, and answers `link_accept`. If the worker already had a pending session, that one is removed first (`Endpoint.remove_key`). The worker's **live** session, if any, is left alone until the new one is used (section 3.1).

**The worker, on `link_accept`:**

1. The message is valid, and it answers the request just sent: the signature covers that ephemeral.
2. `signature` verifies under the hub key it stored at approval. Otherwise it treats the attempt as failed and tries again later (section 3.2). It never uses the key.
3. `ee` and `ss` are not all zero.

### 2.6 What an attacker can and cannot do

**An eavesdropper on the HTTPS path.** It sees TLS ciphertext. Even with TLS stripped (a LAN hub on plain `http`, or a TLS-terminating proxy that logs bodies), it learns only public keys and signatures. It cannot compute `ee` or `ss` without one of the secrets, so it cannot derive the session key or read the link. On the link itself it sees each session's `band_id` in cleartext. That is a new value per connection, so it does not even link one session to the next.

**A recorded exchange replayed later.**
- A replayed `link_open` makes the hub produce a fresh hub ephemeral, and the replayer cannot compute `ee` without the worker's ephemeral secret, long erased. The hub adds a pending key that never authenticates a frame.
- The live session is untouched, because only a session that is used replaces it. At worst a replay displaces the real worker's pending exchange if it lands in the same moment, and the worker tries again.
- A replayed `link_accept` is ignored: its signature covers an old worker ephemeral.

**Recorded link frames replayed.** This is the case brief 26 is about.
- To a worker or a hub that restarted: neither holds the old session key any more. The hub drops the frames before decryption, because their `band_id` is not in its table (Telesthete design T1, §3.6 rule 1). The worker's new band is under a different key, so they fail authentication.
- To a side that did not restart: Telesthete's own replay check applies within the session (SPEC §3.3; T15 at the hub).

Nothing is delivered either way.

**A stolen worker key** (the identity and link secrets from the worker's state directory):
- **It can** make exchanges as that worker and so connect as it. It would receive the calls meant for it, including arguments into which the hub substituted secrets (X2). It could report false heartbeats and results. This lasts until the key is revoked (R5, W7). When the thief and the real worker both connect, the newer session replaces the older each time. The flapping shows on the Workers page (online and offline) and in the journal of approvals and revocations.
- **It cannot** read sessions recorded before the theft: forward secrecy, below. It cannot pose as the hub to the real worker, because `link_accept` is signed by the hub key. It cannot touch any other worker.

**A stolen hub key** (`hub.key` and `hub_link.key` from the hub's data directory, or from a backup, which brief 24 encrypts with the vault key). This lets the thief pose as the hub to every worker that can reach it. That is the hub's whole trust, as today with `hub.key` alone. Forward secrecy still protects sessions recorded before the theft.

**Forward secrecy: yes.** The session key depends on `ee`, and both ephemeral secrets are erased as soon as the key is made. A thief who later takes every long-term secret still cannot compute `ee` for a recorded session.

The protection does not cover a side that is compromised during a session. That side holds the session key in memory.

What it costs: one X25519 key generation and one extra X25519 per side per connection, all under a millisecond on a PC (an ESP32 estimate is in section 6.2). There is no extra round trip.

Without it (the nonce sketch of hand-over §1.7, decision D2): the session key would follow from the two long-term link keys plus public nonces. Anyone who later stole either long-term link secret could decrypt every recorded session of that worker, including any secret the hub substituted into a call.

---

## 3. The life of a connection

Numbers used below come from the Telesthete wire spec and design:
- `KEEPALIVE_INTERVAL` is 5 s and `PEER_TIMEOUT` is 15 s (SPEC §4.5, §12.4).
- HELLO is retried after 0.5, 1, 2 and 4 s, then every 5 s (Telesthete design §7.2, §4.11).
- A Channel retransmits after 500 ms and gives up after 10 tries, about 5 s, then resets with `PeerUnresponsive` (§7.2 g).
- A WebSocket client reconnects after 0.5 s, doubling to 30 s (§6.2).
- The hub's heartbeat interval is 15 s by default, and a worker is shown offline after three missed intervals (H2, H3).

### 3.1 From start to the first heartbeat

1. **An approved worker starts.** It has its identity, its link secret and the hub key in its state (W7). It does `POST /link` (section 2) and gets `link_accept`.
2. It starts a Telesthete `Band` under the session `psk`, with reconnection on (`reconnect=True`, Telesthete design §7.2). Over UDP it uses the hub URL's host and `udp_port`; over WebSocket, `<hub URL>/band` (section 6.1 says which first).
3. The Band sends HELLO, retried until answered. The hub's `Endpoint` holds the pending key, so the HELLO authenticates, the hub answers HELLO_ACK, and the hub's band now knows the worker's path (§5.4).
4. **The worker opens Channel 0** (the link profile's initiator role, SPEC §12.6). At the hub the accept hook (`Band.on_channel`, §6.7 b) creates the Channel. The hub wraps it as a link object and starts `Hub.serve_link(link)` (section 5.2). This session is now the worker's **live** session.
5. **The worker sends its first heartbeat** (H1: connect is the first heartbeat). `serve_link` checks the heartbeat's key against the link's key, records the worker online, and closes the worker's previous link if it had one. Closing a link removes its key from the Endpoint, so frames under the old session are dropped before decryption from then on.

The first heartbeat arrives one HTTPS round trip plus one UDP or WebSocket round trip after the worker starts. That is well under a second on a normal network.

### 3.2 The worker's rule

**The worker makes a new session whenever it loses the old one, and never reuses a session key.** "Loses" means any of:
- Channel 0 resets, for any reason (§6.7 c: the peer restarted, left, reset, or was unresponsive);
- the Band declares the hub lost after `PEER_TIMEOUT`;
- the worker process starts.

It then repeats section 3.1 from step 1. If the HTTPS exchange itself fails (no network, the hub down), it waits 0.5 s, doubling to 30 s with ±20 % jitter, and tries again. These are the Telesthete WebSocket client's numbers, so the two layers back off alike.

A `key_revoked` or `unknown_key` answer is not retried: the worker stops and reports, as it does today for a refused knock (`Refused` in `rook_worker.py`).

### 3.3 Case by case

| Case | Who notices, and how | How long | What is lost | Who acts |
|---|---|---|---|---|
| **Worker restarts** (crash, update, reboot) | The worker, by starting. The hub, when the new session's first heartbeat arrives; the old session is then closed (3.1 step 5). | One exchange: under a second. | Calls in flight on the old session fail with `worker_disconnected` (`serve_link` already does this when a link closes). The restarted worker's streams are gone, because it owned them (S2); see D5. | Rook: the new exchange. Telesthete: nothing. |
| **Core update** (U3: start new core, hand over, exit old) | As a restart: the new core makes its own session. The hub moves to it at its first heartbeat, and the old core's session closes. | Under a second. | As a restart. The old core's streams end when it exits. | Rook. |
| **Hub restarts** | The hub has no session keys; the worker's frames are dropped. The worker notices when Channel 0 gives up on unacknowledged data (about 5 s after its next send: at most the next heartbeat) or at `PEER_TIMEOUT` (15 s), whichever comes first. A WebSocket worker's connection drops at once, but its Telesthete client reconnects and sends HELLO under the old key, which the hub cannot answer; it learns no sooner (see D7). | Up to about 20 s: 15 s plus the exchange. | Everything in flight both ways. Calls the hub had open (long calls are already cut short at start, brief 22). Stream output the worker sent during the gap. | Rook: the worker's rule (3.2). The hub's `Endpoint.connect` at start does not apply, because the hub holds no keys (hand-over §1.7). |
| **Network drops briefly** (under about 5 s, no data waiting) | Possibly nobody. Telesthete retransmits, and the path is followed when it returns (§5.4). | 0. | Nothing. | Telesthete. |
| **Network drops longer** | The worker: Channel give-up (about 5 s after its next send) or `PEER_TIMEOUT` (15 s). The hub: the new session's first heartbeat, or a give-up on a call it was sending. | 5 to 20 s, plus the exchange once the network is back. | Messages that were unacknowledged when the Channel reset: calls in flight (they fail with `worker_disconnected`) and stream data in flight. | Rook: a new session (3.2). |
| **The worker's address changes** (NAT rebinding, Wi-Fi to wired, UDP to WebSocket) | The hub's Endpoint, from the next accepted frame (§5.4, T7). | 0, or one keepalive (5 s) if a frame was in flight. | Nothing: the session and Channel 0 carry on. | Telesthete. |
| **Key revoked at the hub** (`attach` revokes old keys, W7; a revoke of the worker's key) | The hub: at once. It removes every session of that key from the Endpoint, so no further frame is decrypted (§4.4, T4), and it closes the link. The worker: by give-up or `PEER_TIMEOUT`; its next `link_open` is answered `key_revoked`, and it stops. | Hub at once; worker within about 20 s. | Everything in flight. | Rook: the remove, the refusal and the stop. |
| **Rekey** (W7) | A new knock, attached to the identity by a person or agent. Attaching revokes the old key, as in the row above, and the worker's next session uses the new key. | As revoke. | As revoke. | Rook. |
| **Two processes with one identity** (a thief, or an old core that has not exited) | Each makes its own sessions, and the hub keeps the newest live one. | — | The older process's calls in flight. | Rook (section 2.6). |

**Where Rook acts and Telesthete does not:**
- the exchange and its refusals;
- the rule that a lost session is never resumed;
- replacing a live session when a newer one is used;
- removing keys on revoke;
- failing open calls when a link closes (already in `serve_link`).

**What Telesthete provides and Rook uses as is:** HELLO retry, `PEER_TIMEOUT`, Channel give-up and reset with a reason, the accept hook, path following, `remove_key`, and WebSocket reconnection. All of these are designed in the Telesthete design and not yet built (section 8).

**The hub sets no timer for links.** A session whose worker vanished without a word stays in the Endpoint until one of three things removes it:
- the worker's next session replaces it;
- a revoke removes it;
- the hub sends it a call, and the Channel gives up, resets and closes the link.

That is at most one live and one pending session per worker. The Workers page's "offline" is H3, computed from the last heartbeat as today.

---

## 4. How Rook's messages ride the link

### 4.1 The Channel

Every protocol v1 message travels as **one Telesthete Channel message** (channel type `0x02`, SPEC §6), on **channel id 0**, **opened by the worker**. This keeps to what `docs/protocol.md` already says ("one message per link message") and to hand-over §2.

- **Why a Channel.** It is reliable and ordered (SPEC §6). Control is not reliable (Telesthete decision 5), and Streams are lossy (SPEC §5). A worker answers calls, so it needs the Channel. That is why the link profile includes it (Telesthete design §8.1).
- **Why the worker opens it.** The link profile's client is an initiator (SPEC §12.6), and a hub that has just restarted holds no session to open from. The hub's accept hook means a worker's SYN is never dropped (§6.7 b).
- **Mapping.** The bytes of one Rook message (canonical JSON, UTF-8) are the data of one Channel message. Telesthete's §6.6 envelope splits it into chunks of at most 1,003 bytes and reassembles it, so Rook adds no length prefix and no framing.

### 4.2 One Channel for everything

Heartbeats, calls, results, errors, events, plugin pushes and every `stream_*` message share channel 0.

- **Why one.** Protocol v1 depends on order across message types: the worker sends `stream_open` before any `stream_data` of that stream, and the hub records the stream "before any data follows" (`Hub.serve_link`). Separate Channels would lose that order and need a new rule to restore it. One Channel per worker also keeps the hub's per-key state small, and an ESP32 link client implements only one Channel (SPEC §12.6).
- **The cost.** A large message delays everything behind it. A heartbeat queued behind a 16 MB `result` waits for it.
  - At Telesthete's default window of 32 packets of about 1 KB, that is about 0.6 MB/s on a 50 ms round trip, so about 27 s for 16 MB.
  - That is under H3's three missed intervals (45 s at the default interval), so a worker sending its largest message is not shown offline on such a path.
  - On a slower path it can be. The sizes below keep such waits rare, and large file transfer belongs to Drop or peer-to-peer later (section 12).

### 4.3 Sizes

- **Largest message: 16,000,000 bytes**, the encoded message, in either direction. Telesthete's reference reassembler accepts up to 16,384 chunks (SPEC §6.6), and 16,384 × 1,003 is 16,433,152 bytes. A round number under that means every conformant receiver with reference defaults accepts every message Rook may send.
  - The hub refuses a call whose encoded size is over the limit before sending it, with the new error `message_too_large`. It refuses typed input the same way.
  - A worker whose result would be over the limit answers `error` with `cap_failed`, saying the result is too large.
  - Today's hub has no limit: the MCP door takes call arguments of any size so that they can carry files. That changes, and section 11 says so.
- **Stream data: at most 65,536 bytes of data per `stream_data`.** A sender splits larger output into several messages. Stream output then never holds a heartbeat or a call for long: 64 KB at about 0.6 MB/s is about 0.1 s. Input from clients is typed text and stays far below this.
- **Small workers.** An ESP32 link client has a local reassembly cap (the Telesthete design suggests 16 KB, §8.2). A call larger than that would never arrive, and the hub would wait until the Channel or the worker gave up. D4 proposes how a worker says its limit.

### 4.4 What else stays the same

- **No protocol v1 message changes** apart from the two new `knock` fields. `heartbeat`, `call`, `result`, `error`, `stream_*`, `event` and `plugins` are carried as they are.
- **The heartbeat still names its key.** `heartbeat.identity.key` must equal the link's key, which is now the identity key that made the session's exchange. `serve_link` already checks this.
- **No Telesthete Streams, Board or Drop** are used by core. Telesthete Control carries HELLO, KEEPALIVE and GOODBYE as it does for any band.

---

## 5. The hub side

### 5.1 Feeding the Endpoint

The hub owns its transports and feeds one Telesthete `Endpoint` through the seam of Telesthete design §5.3 (`Endpoint(hostname="hub", send=send_to_path)`, `receive(path, frame)`, `close_path(path)`).

- **UDP.** One asyncio datagram socket, bound to the start command's `--udp` address. Each datagram goes to `ep.receive(("udp", source), data)`. The `send` callback answers with `sendto` on the same socket, so replies come from the address and port the worker sent to (§5.2, T5). On a multi-homed host bound to a wildcard address this needs one socket per local address, or `IP_PKTINFO` (§5.2). A single-address VM, the recommended host (Topology), does not need it; the first build binds one address and says so.
- **WebSocket.** A route `GET /band` on the hub's existing aiohttp server, which already serves the knock, the MCP door and the site. Each connection is one path, `("ws", connection number)`. Binary messages go to `ep.receive`. Text messages and binaries under 43 bytes are ignored. The connection's frames go out through a bounded queue of 256 frames that drops when full (the seam's contract: `send` must not block, T9). The route answers pings, closes a connection silent for 60 s (aiohttp's `receive_timeout`), and calls `ep.close_path` when it ends. All of this is from §5.2 and the §9.3 server rules.
- **Session epoch.** Milliseconds since the Unix epoch at start, passed as `session_epoch`, so it increases across restarts on a synced clock (§4.2, SPEC §4.3).
- **TLS.** None in the hub process. Whatever serves HTTPS on 443 (Caddy on the recommended VM) proxies everything to the hub's HTTP server, `/band` upgrades included (hand-over §3). The installer is not part of this design.

### 5.2 One more carrier; nothing above it changes

The hub already takes any link object with `key`, `async send(bytes)`, `async recv()` (raising `EOFError` once closed) and `close()` (`hub/rook_hub/__init__.py`, module docstring). The Telesthete carrier is one more such object, `TelestheteLink`, in a new module `hub/rook_hub/link.py`:

- `key` is the identity key that made the session's exchange.
- `send(data)` sends one Channel message on channel 0. It refuses data over the 16,000,000-byte limit (section 4.3).
- `recv()` returns the next Channel message. It raises `EOFError` when the Channel resets (any reason) or the link is closed.
- `close()` calls `Endpoint.remove_key` for the session, then ends `recv`.

`link.py` also holds the `/link` route (section 2), the UDP socket and the `/band` route. When the accept hook hands over Channel 0 of a pending session, it builds a `TelestheteLink` and starts `hub.serve_link(link)` as a task, exactly as the tests start it for a memory link.

`Hub.serve_link`, the call path, streams, plugins and everything above stay as they are. `tests/memory_link.py` and every test that uses it stay valid: the in-memory link remains a carrier the hub accepts.

Two small changes fall below `serve_link`:

- **The size check in `Hub.call` and `Streams.write`.** It answers `message_too_large` instead of sending (section 4.3).
- **A revoke closes a live link.** The hub already closes a worker's link when `attach` revokes its key (`Hub.attach`). A revoke of a worker's only key does the same, through the session table in `link.py`.

### 5.3 What the start command gains

One option: `--udp ADDRESS:PORT`, the UDP address the hub serves the link on. Its default is the `--host` address and port 7474, the port the hand-over keeps (hand-over §2). `--udp off` serves no UDP, and `link_accept.udp_port` is then null.

The WebSocket route needs no option: it is on the HTTP server the hub already serves. The hub link key file is made in the data directory on first start, like `hub.key` and `token.secret`, so it needs no option either.

---

## 6. The worker side, per form

### 6.1 The Python core, now

- **State.** Next to `key` and `identity.json`, a file `link_key` (0600) holds the X25519 secret. It is generated on first start with the identity key and knocked with it (section 2.3).
- **The exchange.** `POST /link` with the standard library's `urllib`, as the knock already does. The signatures and X25519 use PyNaCl, which the worker already depends on.
- **The carrier.** A Telesthete `Band(session psk, reconnect=True)`, with Channel 0 opened by the worker and wrapped as a link object of the same shape as the hub's. `Worker.run` and `_serve` stay as they are above it.
- **The loop.** `Worker.run` today returns when its link closes. It becomes a loop: make a session (section 3.1), serve it, and on loss start again (section 3.2). It ends when `stop()` is called or the hub refuses the key. Tests that pass an in-memory `connect` keep working: the loop calls it again only after a loss, and `stop()` ends it.
- **Transport order: UDP first, then WebSocket.** The worker tries UDP to `udp_port` when the hub serves it. If no HELLO_ACK arrives within 4 s (the HELLO retries at 0.5, 1 and 2 s have then all gone unanswered), it starts a WebSocket Band under the **same** session key; the hub's path following accepts either (§5.4). Without `udp_port` it goes straight to WebSocket. This is Telesthete's own order (SPEC §9.1 primary, §9.3 fallback). D3 offers WebSocket only.
- **Dependencies.** Telesthete (pinned, section 8), with its `ws` extra for the WebSocket client (`websockets`, Telesthete decision 4).

### 6.2 The ESP32 worker (W2, U5, the link profile)

What an ESP32 needs, and nothing chosen here shuts it out:

- **The link profile** (SPEC §12.6, Telesthete design §8.2): the baseline cipher, Control, one Channel as initiator with a window of 1 to 4, reconnection, and one transport. About 1,100 lines of C (the Telesthete design's estimate). Rook needs exactly channel 0 as initiator, which the profile is built around.
- **The exchange.**
  - One HTTPS POST (`esp_http_client` over mbedTLS, which a Wi-Fi device links already) and JSON (cJSON).
  - Per connection: one X25519 key generation, two X25519 operations, one Ed25519 signature, one Ed25519 verification and one HKDF-SHA256.
  - mbedTLS provides HKDF. X25519 and Ed25519 come from the libsodium component for ESP-IDF, about 30 to 60 KB of flash (hand-over §5, not measured).
  - Time per connection: tens of milliseconds at most for the curve operations (not measured). A device that reconnects a few times a day does not notice.
- **No epoch storage.** Telesthete requires a session epoch that increases across restarts, persisted when a device has no clock (SPEC §4.3). With a fresh key per connection, no receiver has ever seen an epoch under the new key, so any epoch is accepted. The ESP32 needs no NVS write per connection: one less wear and one less failure mode.
- **Stored secrets.** The identity seed and the link secret, 32 bytes each, in NVS (U5: the ESP32 is bootstrap and core in one). Session keys are never stored.
- **Message size.** Its reassembly cap is well under 16 MB. D4 proposes the optional `max_message` heartbeat field, so that the hub never sends it more than it can take.
- **Transport.** UDP is the cheaper one: no TLS per frame, no TCP. The HTTPS exchange already uses TLS once per connection. The firmware picks one transport, as the profile allows.

### 6.3 The Android app (A1 to A3)

- **Joining.** The app signs in (A2). The hub issues the device a token under that user, and the app's core knocks with it. A good token approves the knock at once (R3), which is A3's implicit approval. From then on the device is an ordinary worker: the same exchange, the same link.
- **The link client** is whichever Telesthete library the Android core uses. A Rust core would need the Rust link lifecycle and WebSocket client (Telesthete PRs 1b and 4, section 8), not the Rust Endpoint.
- **Reconnects.** Doze and network switches drop connections often. Each loss costs one HTTPS round trip, then a new Band: about the cost of the TLS handshake a WebSocket reconnect makes anyway. WebSocket over 443 is the transport that survives mobile networks best; D3 applies.
- **A1** (the hub stamps its URL into the APK) gives the app its hub URL, the only input the exchange needs.

### 6.4 U2: the bootstrap never speaks Telesthete

The bootstrap holds the identity, keeps state that survives core reinstalls, and downloads over plain HTTPS (U2). In this design:
- the exchange is plain HTTPS;
- it is made by the core, not the bootstrap;
- Telesthete is linked only into the core.

The bootstrap's part is to keep the new `link_key` file with the identity key it already keeps (W7). It never opens a Band and never derives a session key. U3's failure report, "over HTTPS so the Workers page shows it", is unaffected.

---

## 7. Unattended joins and the dongle

**Join tokens (R3) do not touch the link exchange.** A join token rides in the knock (`knock.token`, which exists). A good one approves the knock at once, and the new worker's owner is the token's user (R3, W6). The approved worker then makes sessions like any other: `/link` needs an approved key, and how the key was approved does not matter. The knock's new `link_key` and `link_signature` are required with or without a token.

**The dongle** (R3) is an ESP32-S3 worker with its own identity. To set up a host it must:

1. Install a worker on the attached host from its core payload (U5).
2. Mint a fresh one-use join token through its own identity.
3. Hand the token to the new worker over its tty or HID connection. The host never sees the dongle's key or token, only the new token, which is spent by its first use.

Step 2 needs a way for a worker to ask its hub for something. Protocol v1 has none today: calls go from the hub to workers. The design does not need to settle this now, because nothing else in it depends on it. D6 proposes a link message, which keeps step 2 on the authenticated, fresh session with no new crypto. The token is owned by the dongle's owner (T5), so the new worker is too (R3).

---

## 8. What Rook needs from Telesthete, and when

The Telesthete design's section 12 lists eight implementation pull requests: 0, 1a, 1b, 2, 3a, 3b, 4 and 5. Rook's Python hub and Python worker need:

| Telesthete PR | What Rook uses from it | Needed by |
|---|---|---|
| **1a** Python link lifecycle | `Band(reconnect=True)`, HELLO retry, `on_channel` (accept hook), `Channel.on_reset` with a reason, reopen, give-up reset | worker carrier (L5); hub carrier (L4), through 3a |
| **2** Python `Band(transport=)` and `WebSocketTransport` | the worker's WebSocket path; `transport=` is also what 3a builds on | worker carrier (L5) |
| **3a** Python `Endpoint` with the path seam | `add_key`, `remove_key`, `receive`, `close_path`, `send` callback, path following, keys kept across GOODBYE (T15) | hub carrier (L4) |
| **0** stale text | nothing to build against; it corrects SPEC §11.1, which still says Rook uses msgpack | — |
| **5** interop and release | a release to pin (Python 0.4.0) instead of a commit | pinning |

Rook's Python side needs **neither 1b, 3b nor 4** (the Rust pull requests). 1b and 4 matter when an Android or Rust core is built (section 6.3). 3b matters only for a Rust hub, which no plan line calls for.

Order: 1a and 2 in parallel, then 3a (which needs both), then 5. Rook pins a commit containing 1a, 2 and 3a until 5 releases.

**What Rook can build and test before each exists:**

- **Before any of them** (Rook PRs L1 to L3, section 10):
  - the derivation and its vectors;
  - the new `knock` fields and the `link_open` and `link_accept` messages;
  - the hub's `/link` route, link key file and session table (with an Endpoint stand-in that records `add_key` and `remove_key`);
  - the worker's exchange, `link_key` file and session loop, over in-memory links.

  Everything is tested through real HTTP, as the knock is today.
- **After 1a and 2:** the worker's Telesthete carrier against a plain Telesthete `Band` playing the hub, over loopback UDP. WebSocket needs a server, which arrives with the hub carrier.
- **After 3a:** the hub carrier and every end-to-end test of section 9.

---

## 9. How it will be proven

All through the one command in README.md, on localhost with ephemeral ports and throwaway keys.

**Vectors** (`protocol/vectors.json`, a new `link` section):
- **Valid:** fixed identity, link and ephemeral keys on both sides, the four signatures, `ee`, `ss`, `okm`, `psk`, and `band_id` (`SHA256(psk)[:16]`, which pins the Telesthete side too).
- **Must be refused:** a small-order ephemeral (`ee` all zero); a small-order link key (`ss` all zero); a `link_open` signature over another hub key; a `link_accept` signature over another worker ephemeral.
- **Message vectors,** as for every message: `link_open` and `link_accept` valid and invalid, and `knock` with and without its new fields.

**The exchange, through real HTTP** (no Telesthete):
- unknown key, revoked key and pending knock are refused with what to do;
- a bad signature is refused with `bad_signature`;
- a good `link_open` gets a `link_accept` that verifies, and both sides derive the same `psk`;
- a second `link_open` replaces the pending session, and the live one stays;
- a replayed `link_open` leaves the live session working;
- a `link_accept` for another ephemeral is refused by the worker;
- the knock refuses a bad `link_signature`.

**The link, over loopback UDP and WebSocket**, with a real `rook_worker` and the real hub:
- a call and a stream round trip;
- a message of 16,000,000 bytes passes and 16,000,001 is refused with `message_too_large`;
- the worker restarts: the new session takes over, and a call in flight fails with `worker_disconnected`;
- the hub restarts: the worker is back within 20 s;
- partitions of 3 s (the session carries on) and of 20 s (a new session);
- UDP blocked: the worker falls back to WebSocket within 4 s;
- the path changes from UDP to WebSocket during a session (the session carries on);
- revoke: frames are dropped at once, and the worker stops on `key_revoked`;
- rekey through `attach`;
- the `--udp off` start.

**Replay, from `replay-after-restart.py`,** as tests that must fail to replay:

1. **Against a restarted worker.**
   - A proxy between hub and worker records every datagram from the hub during a session in which the hub sends the worker a `call` (the test's own cap counts its runs).
   - The worker process stops, and a new one starts with the same identity and makes a new session.
   - The proxy then re-sends every recorded datagram from the hub's address to the worker.
   - The test asserts that the cap ran exactly once and that the new session carried on undisturbed.
2. **Against a restarted hub.**
   - The same recording, of the worker's datagrams: heartbeats, a `result`, `stream_data`.
   - The hub restarts (same data directory) and the recording is re-sent from the worker's address.
   - The test asserts that nothing reached `serve_link`: no heartbeat recorded, no stream data recorded, no call answered. It also asserts that the Endpoint counted the frames as unknown (`dropped_unknown`, Telesthete design §4.2).
3. **The same two over WebSocket.**
4. **A control test that proves the test can see a replay.** With fresh keys disabled (a fixed session key reused across the restart, test-only), the replay is accepted. It is the Telesthete non-claim, kept so that a change in behaviour is noticed.

---

## 10. Build plan

Rook pull requests, in order. Each is small enough to review against this document line by line.

| PR | Delivers | Tested by | Needs |
|---|---|---|---|
| **L1 Protocol** | `docs/protocol.md` changes of section 11. The protocol library: validation of `link_open`, `link_accept` and the new `knock` fields; the derivation as one function, `session_psk(...)`, with its checks; signing and verifying the four signed texts. Vectors. HKDF-SHA256 written over `hmac` (about 15 lines), unless Bake prefers a dependency. | the vectors (section 9) | — |
| **L2 Hub exchange** | `hub_link.key` in the data directory. The `keys` and `knocks` tables gain `link_key`, and the knock checks `link_signature`. `POST /link` with its checks and refusals. A session table (pending and live per worker) behind a small interface that L4 points at the Endpoint. The size limit in `Hub.call` and `Streams.write`. | the exchange tests through real HTTP | L1 |
| **L3 Worker exchange** | The `link_key` file; the knock's new fields; the `/link` client and its checks; `Worker.run` as a loop (section 6.1), over in-memory links; stream output split at 65,536 bytes. | the exchange from the worker's side; the loop's restart and stop over memory links; every existing test unchanged | L1, L2 |
| **L4 Hub carrier** | `hub/rook_hub/link.py`: the Endpoint, the UDP socket, `GET /band`, `TelestheteLink`, the accept hook to `serve_link`, remove on close and on revoke. The `--udp` option. README updated. | link tests with a Telesthete `Band` playing the worker, over UDP and WebSocket; the hub-restart replay test | L2; Telesthete 1a, 2, 3a pinned |
| **L5 Worker carrier** | The worker's Band, UDP first then WebSocket, wrapped as its link; the loss rules of section 3.2. | the whole link matrix of section 9; the worker-restart replay test | L3, L4 |

L1 to L3 can be built and reviewed now. L4 and L5 wait for Telesthete's 1a, 2 and 3a.

Not in this plan: the installer (hand-over §3), the ESP32 link client, the Android core, the dongle's mint path (D6), and D4 and D5 if Bake takes them. Each is its own brief.

---

## 11. Changes to docs/protocol.md, exactly

Protocol v1 is not released, so these changes keep version 1 and change its vectors. They are made in PR L1, not by this design.

1. **"Carriage", second bullet,** replaced by:
   > **Every other message** travels over the worker's link to its hub, one message per Telesthete Channel message (channel type `0x02`, channel id 0, opened by the worker; SPEC §6, §6.6). Telesthete's fragmentation keeps message boundaries, so Rook adds no length prefix. The link is a Telesthete band under a key made for this connection only, by `link_open` and `link_accept` over HTTPS (below). It runs over UDP to the hub URL's host at `link_accept.udp_port`, or over WebSocket at the hub URL with scheme `wss` (or `ws`) and path `/band`.
2. **"Carriage", a third bullet added:**
   > **The link exchange** (`link_open`, and its reply `link_accept` or an `error`) travels as one HTTPS `POST` to `<hub URL>/link`, each time the worker connects.
3. **"Minimum worker":** `link_open` and `link_accept` are added to the messages every worker must implement.
4. **`knock`:** two rows added, `link_key` and `link_signature`, both required, as in section 2.3. One paragraph added: the signed text, and that the hub refuses a knock whose `link_signature` does not verify (`bad_signature`).
5. **New sections `link_open` and `link_accept`:** the fields of section 2.3, the derivation of section 2.4, the checks of section 2.5, and the rule that neither side stores a session key or an ephemeral secret.
6. **A new section "Sizes":**
   > An encoded message is at most 16,000,000 bytes, in either direction. The hub refuses a call or input that would be larger, with `message_too_large`. A worker whose result would be larger answers `cap_failed`. `stream_data` carries at most 65,536 bytes of data; a sender splits larger output.
7. **Error codes** gain `bad_signature` (hub: a knock's `link_signature` or a `link_open`'s `signature` does not verify) and `message_too_large` (hub: a message would exceed the size limit). The `unknown_key` and `key_revoked` rows add "or a `link_open`".
8. **"Sequences"** gains the exchange and the first link messages:
   ```
   worker                                   hub
     | POST /link  link_open{key, ephemeral, signature}
     |<-- link_accept{ephemeral, hub_link_key, udp_port, signature}
     |    both derive psk; the hub holds it as the worker's pending session
     | Telesthete band under psk: HELLO / HELLO_ACK, then Channel 0 SYN
     |--- heartbeat ------------------------->|   this session is now live
     |<-- heartbeat_reply{interval} ----------|
   ```
   The paragraph after it becomes: "A restart reuses the stored key and identity, makes a new `link_open`, and goes straight to the link (R5). A worker that loses its link makes a new `link_open`; it never reuses a session key."

Only if Bake takes D4, D5 or D6: an optional `heartbeat.max_message` (integer), an optional `heartbeat.open_streams` (list of stream handles), and a worker-to-hub `mint` message, each worded as in section 13.

Outside `docs/protocol.md`: `docs/telesthete-surface.md` says "How the link is keyed per worker is open". L1 updates that sentence to point here.

---

## 12. What this leaves room for

Hub and spoke only (Topology). This design does not add peer-to-peer, relaying or federation. It leaves room for them:

- **Federation** (F4 to F8) is hub to hub. A share between two hubs can use the same exchange shape, two identity keys plus ephemerals over HTTPS, with each hub one side of a two-member band. The `info` and salt would name the use (`rook-share-v1`).
- **Heavy streams** later go peer to peer with the hub as introducer (Topology). The hub can introduce two of its workers by running an exchange with each and handing each the other's address, without changing the worker-hub link.
- **A stock Telesthete relay** can run beside the hub on another port or path if something ever needs WebTransport or relaying (Telesthete design §5.7). Workers cannot tell which they reached.

None of this is designed here.

---

## 13. Decisions needed

Each with my recommendation first and the alternative in one line.

**D1. A fresh key for every connection** (the reviewer's proposal, which Bake has not confirmed).
- **Recommended:** yes, as designed in sections 2 and 3. A recorded session is useless after either side restarts, and calls are never run twice by a replay.
- **Costs:**
  - one HTTPS round trip per connection;
  - after a hub restart, workers are back in up to about 20 s instead of at once for WebSocket workers;
  - a new session (and its in-flight losses) after every loss, instead of resuming;
  - per-connection curve operations on an ESP32 (tens of milliseconds).
- **Alternative:** Rook's own freshness instead. Every `call` carries an id and an expiry, and each worker keeps the ids it has run on disk until they expire. Every hub-side message that causes an action needs the same, and workers need clocks within the expiry window (hand-over §1.7).

**D2. Forward secrecy, through ephemeral X25519 keys on both sides.**
- **Recommended:** yes (section 2.4). It costs one extra X25519 per side per connection and no extra round trip.
- **Alternative:** 32-byte nonces instead of ephemerals (hand-over §1.7). Fresh keys, but anyone who later steals a long-term link key can read every recorded session of that worker, including secrets substituted into calls.

**D3. The Python worker's transport order.**
- **Recommended:** UDP first, WebSocket after 4 s without an answer (section 6.1), as Telesthete orders its transports.
- **Alternative:** WebSocket only for workers, through the same port and TLS as the knock. It is one path and works wherever the knock works; the hub still serves UDP for an ESP32.

**D4. How a small worker states its largest message.**
- **Recommended:** an optional heartbeat field `max_message` (integer bytes). The hub refuses with `message_too_large` any call or input above it, as it does above 16,000,000. Python workers leave it out.
- **Alternative:** no field. An ESP32 drops what it cannot reassemble, and the hub learns only when the Channel gives up and the link resets.

**D5. Streams across a worker restart.** This is a gap the link design surfaced, not one it created: it exists with memory links today. A restarted worker has lost its streams (it owns them, S2), but the hub keeps them open: a new session looks the same whether the process restarted or only the network dropped.
- **Recommended:** an optional heartbeat field `open_streams`, the handles of the worker's open streams. The hub closes, as `closed_by: worker`, any open stream of that worker not listed.
- **Alternative:** leave it. Dead streams then stay open on the Workers page until someone closes them (S5).

**D6. The dongle's way to mint a join token** (R3).
- **Recommended:** a worker-to-hub message on the link, `mint` (`id`, `purpose: "join"`), answered with `result` (the token) or `error`. It is authenticated and fresh by the session, needs no new crypto, and the token is owned by the worker's owner (T5). Older hubs answer `bad_message`.
- **Alternative:** a signed HTTPS request, `POST /mint`, which needs its own freshness against replay.

**D7. Telesthete lacks a connection signal Rook could use.**
- After a hub restart, a WebSocket worker's Telesthete client reconnects silently and keeps sending HELLO under the old key. The worker learns of the restart only at `PEER_TIMEOUT` (15 s) or a Channel give-up.
- A connection-state callback on `WebSocketTransport` (or a reset reason when the server's connection closes) would let it make a new session at once. That would bring WebSocket workers back in about a second.
- **Recommended:** accept the 15 s for now. Note it for Telesthete as an additive request.
- **Alternative:** ask Telesthete for the callback before L5.

**D8. PLAN.md, Topology, still says "The Rook hub implements the Telesthete relay as its base."** Bake's 13 Nov ruling replaced that with a terminating endpoint, and the hand-over §4 says the sentence "now means" the server side of the spec.
- **Recommended:** amend the line to "The Rook hub is its own Telesthete terminating endpoint: one process, no relay." Bake signs the amendment.
- **Alternative:** leave the line, with the hand-over's reading as its meaning.

**D9. For information, no action: Telesthete decision 9** (whether Telesthete later makes its data key depend on the receiver too) is open for Bake. This design does not wait for it. If Telesthete adopts it, Rook's fresh keys are still correct and nothing here needs to change; Rook could then keep its sessions across a Telesthete-level reconnect, but it would not have to.

---

## 14. What I checked, and what I could not verify

**Checked by reading the reference copies and Rook's code:**
- every Telesthete number used here: `KEEPALIVE_INTERVAL`, `PEER_TIMEOUT`, RTO, retries, window, 1,024-byte frame data, 1,003-byte chunks, the 16,384-chunk reassembly default, 43-byte minimum (SPEC §4.5, §6.4, §6.6, §12.4; Telesthete design §7.2, §8.2);
- that the data key depends on the PSK and the sender's epoch only, and that HELLO is under the base key (SPEC §3.1, §4.3);
- the Endpoint API, the seam, path following, keys kept across GOODBYE, T1 to T15 and the stated non-claim (Telesthete design §4.2, §5.3, §5.4, §5.6);
- the lifecycle, the accept hook, reset reasons and the WebSocket client (§6.2, §7.2, §10);
- the eight pull requests and their dependencies (§12);
- the reproduction's method (`replay-after-restart.py`);
- that `Hub.serve_link` takes any object with `key`, `send`, `recv` and `close`, checks the heartbeat key against `link.key`, closes an older link for the same worker, and fails open calls with `worker_disconnected` when a link ends;
- that the knock is HTTPS today and the worker knocks only until approved;
- that `Worker.run` returns when its link closes.

**Not verified, because it is not built:**
- Every Telesthete behaviour this design relies on is designed, not built: the `Endpoint`, the seam, path following, `on_channel`, `on_reset`, reconnection, `WebSocketTransport`. In particular, the Telesthete design itself says path following is not prototyped (its §13).
- Whether the Python `Band(transport=)` of PR 2 can carry a `WebSocketTransport` with the same session key after a UDP attempt (section 6.1) rests on reading §4.2 and §6.2, not on trying it.

**Not run:**
- The replay reproduction. Running it needs a Telesthete checkout, and the brief keeps me out of the Telesthete repository. I rely on the Telesthete design's report of it (§3, §7.6).
- No code at all: this is a design.

**Estimates, not measurements:**
- the curve operation times on an ESP32;
- libsodium's flash size there (hand-over §5's 30 to 60 KB);
- the about 0.6 MB/s Channel throughput at a 50 ms round trip (window × frame size ÷ round trip; Telesthete has not measured it, its §5.7);
- the throughput of a Python terminating endpoint (not measured by the Telesthete design either).

**Not reviewed by a cryptographer:** the exchange (section 2). It is a signed ephemeral Diffie-Hellman with a static-static term mixed in. That is a common shape (close to SIGMA-style and Noise-style patterns), but I have not proved it and did not model it formally. The checks in section 2.5 are the ones the hand-over and RFC 7748 call for.
