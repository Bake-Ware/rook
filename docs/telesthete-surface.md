# Telesthete surface report

What the pinned Telesthete gives Rook, checked against the code and its own tests, and where it does not match PLAN.md (Layering, R1, R5, U2, W2, W7, S1 to S5).

File and line references are to Telesthete at the pinned commit.

## Pin

**`8538e7c4d0ff201851723b556f3880e3cbb7a8fa`** (`main` as of 9 November 2026, "Stream messages carry their datagram's receive time (#16)").

- `v0.3.0` is `f3fc597` (14 October 2026). Head is two commits past it: #15 gates the Rust AF_UNIX transport on `cfg(unix)` so the Rust crate builds on Windows, and #16 adds a receive time to Rust Stream messages. Neither touches the Python package, the tests or SPEC.md (`git diff --stat v0.3.0 8538e7c` lists only five files under `rust/`). For Python the two are identical; both report version `0.3.0`.
- I pin head rather than the tag because Rook has Windows workers and a Rust core is planned (U1), and #15 is what lets the Rust crate build there. The pin is recorded here only: this pull request does not import Telesthete (see "Where PLAN.md and Telesthete disagree").
- `/home/bake/telesthete` is checked out at `b6290c7`, which is older than `v0.3.0` (22 commits behind `origin/main`) and has a modified `telesthete.egg-info/SOURCES.txt`. Nothing below refers to it.

Tests run at the pin, in a clone under `/tmp`:

- Python 3.14, `pip install -e .[aes] pytest pytest-asyncio` as CI does: `138 passed`. Without `pytest-asyncio` 31 async tests fail to collect; that is setup, not code.
- `cargo test --workspace --locked`: `telesthete` 113 passed, `telesthitium` 56 unit and 21 integration passed, 0 failed.

## Public surface

**Python.** `telesthete/__init__.py:7-11` exports `Band` and `Peer` only. A `Band` (`band.py:37-46`) takes one `psk: str`, binds one UDP socket of its own (`band.py:97`) and offers `connect_peer`, `stream(id)`, `channel(id, peer_addr)`, `board(id)`, `drop_sender`/`drop_receiver`, `get_peers`, `start`/`stop`. The `telesthete.protocol.*` and `telesthete.transport.*` modules can be imported, but only the `route` helpers are named as API anywhere (README changelog, PROTOCOL_STATUS.md:45). I treat everything else under them as internals.

**Rust.** `rust/telesthete/src/lib.rs:14-55` makes every module public and re-exports `Band`, `ChannelHub`/`ChannelEndpoint`, `StreamHub`, `Transport`, `BoardHub`, `DropHub` and the crypto and framing helpers. So the Rust public surface is much wider than the Python one, including a raw `Transport`.

**Relay.** `rust/telesthitium` builds `telesthete-hub`, a relay that routes by the cleartext `band_id` and holds no keys. It serves UDP, WebSocket/WSS, WebTransport and AF_UNIX (`rust/telesthitium/README.md`).

Implemented and tested, both languages: framing, PSK key derivation, ChaCha20-Poly1305 and AES-256-GCM, cipher negotiation, replay watermark and session epochs, Control, Stream, the reliable Channel with §6.6 fragmentation, Board, Drop, LAN discovery, AF_UNIX, addressed routing (§10.1).

Marked future in SPEC.md: WebSocket (§9.3, SPEC.md:819) and the hub section (§10, SPEC.md:943), although the Rust relay implements most of it. WebTransport (§9.6) is specified but implemented only in the relay.

The status file is stale. PROTOCOL_STATUS.md:30 says the Python Channel is a "PoC (no real retransmit)", line 33 says Board and Drop are not done, and lines 56-58 repeat the retransmit gap. LIBRARY_COMPLETION.md:49-71 records that phases 1 to 8 landed, and the code agrees (next section). Trust LIBRARY_COMPLETION.md and the code, not PROTOCOL_STATUS.md.

## Answers

### a. Reliable request and response over UDP

**Yes, the pinned Python library can carry it, with conditions Rook has to work around.**

- The Channel is real now. Unacknowledged frames are resent after the RTO (`protocol/channel.py:489-518`), each resend re-encrypted under a fresh outer sequence (`channel.py:299-311`). It has a sliding window, reordering by inner sequence, deduplication and §6.6 fragmentation of large messages (`channel.py:225-234`, `360-404`). `tests/test_channel.py` (22 tests, including `test_rto_retransmission_fresh_outer_sequence`) passes, but it runs over a mock transport.
- I also checked it over real UDP on localhost with two `Band`s: 50 request/reply pairs of up to 10 KB arrived complete and in order, both with no loss and with 20% of datagrams dropped in both directions after the handshake (loss injected by wrapping the probe's `transport.send`). The throwaway probe script is in the pull request description; the observations in conditions 3 and 4 and in answer b come from it too.

The conditions, all visible through the public API:

1. **One peer per channel id per Band.** `Band.channels` is keyed by `channel_id` alone (`band.py:365-376`), and a channel is bound to one peer address (`band.py:401-404`). The Rust `ChannelHub` keys by `(peer, channel_id)` (`channel.rs:733`), so Rust does not have this limit.
2. **The receiving side must create the channel before the SYN arrives.** Otherwise the frame is dropped with "No channel with ID" (`band.py:395-398`). Nothing in the API tells you that a new peer has said HELLO; you have to poll `get_peers()`.
3. **A lost HELLO is not resent.** `connect_peer` sends a single HELLO (`band.py:499-514`), and the keepalive loop never resends it (`band.py:575-590`). With the first datagram dropped, neither side saw the other after 6 seconds. The application has to call `connect_peer` again.
4. **A restarted peer cannot reuse the old channel.** The Python Channel's inner sequence starts at 0 only when the object is built (`channel.py:150-152`), `open()` requires `CLOSED` (`channel.py:186-189`), and `Band.channel(id)` returns the cached object (`band.py:365-366`). I restarted the worker side: its new SYN (inner seq 0) was treated as a duplicate by the hub's old channel, and the channel stayed in `SYN_SENT`. There is no public way to drop a channel from a `Band`, so the other side has to move to a new channel id or a new `Band`.
5. **A silent peer tears the channel down** after 10 resends (`channel.py:41`, `506-511`), about 5 seconds, and it does not reopen. Peers are dropped after 15 seconds without a keepalive (`peer.py:33`, `band.py:583-590`).

### b. Keys

**Under the public surface, one Telesthete key means one `Band` and one UDP port, in both languages. One hub process cannot serve many workers, each under its own key, on one UDP port.**

- Python: a `Band` takes one PSK string (`band.py:39`), derives one `band_id` from it (`protocol/crypto.py:47-49`) and binds its own `UDPTransport` (`band.py:97`). There is no parameter for a shared socket. When I bound a second `Band` with a different PSK to the same port, `SO_REUSEADDR` (`transport/udp.py:57`) let the bind succeed, but Linux delivered every datagram to the second socket, and the first `Band` never saw its peer.
- Rust: `Transport::bind(addr, key, band_id)` takes one base key and one `band_id` (`transport.rs:184`). It decrypts HELLOs only under that key (`transport.rs:323`) and drops other `band_id`s (`transport.rs:332`). It does keep per-peer data keys (`register_peer_key`, `transport.rs:218`), but they are session keys derived from the same PSK. `band.rs:1` states "One Band per PSK".
- A per-worker keypair can become a Telesthete key only by deriving a PSK string from it. For example, both sides could compute an X25519 shared secret between the worker key and a hub key, which PyNaCl (already a Telesthete dependency) provides, and pass its hex as `psk`. That stays on the public surface, but it gives each worker its own `Band`, so the hub needs one UDP port per worker.
- Two other shapes stay on the public surface, and each breaks a plan line: one hub-wide band PSK shared by every worker (revoking one worker's key then does not stop its frames), or the Telesthete relay on one public port in front of per-worker `Band`s (a second process, and the hub still binds one local socket per worker).

### c. First contact

A worker that has not been approved shares no key with the hub, so it cannot speak Telesthete to it at all: every frame is AEAD-sealed under a PSK-derived key (SPEC.md §3.1, lines 95-103). Telesthete has no anonymous or public-key handshake (SPEC.md:200-203). U2 says the bootstrap never speaks Telesthete and talks to the hub over plain HTTPS, and R1 says the hub URL is the only input.

So the knock has to be an HTTPS request to the hub URL, and the hub needs an HTTPS endpoint for it. Anything the worker needs before its first Telesthete frame (its identity, the hub's public key, where and how to connect) has to come back over that same HTTPS exchange once the knock is approved. This pull request does it that way (`docs/protocol.md`, "Knock").

### d. WebSocket fallback

- Specified as §9.3 and marked "Future, not yet implemented" (SPEC.md:819-825): one Telesthete frame per binary WebSocket message, no length prefix.
- **Exists only in the Rust relay**: `telesthitium/src/ws.rs` (WS and WSS, path `/band`, band learned from the first frame), covered by `tests/it_ws.rs` (5 tests, passing), including a WS-to-UDP bridge (CONFORMANCE.md F3).
- **No client exists** in either library: `LIBRARY_COMPLETION.md:63-65` scopes WS and WebTransport clients out of the reference libraries ("the references speak UDP + AF_UNIX"). The Python package mentions WebSocket only in a docstring (`transport/__init__.py:2`).
- What a Python hub would need in order to offer it: a WebSocket server (none in the Python standard library), plus a way to feed its frames into a `Band`. The Python `Band` has no such way: it builds its own `UDPTransport` and takes no transport argument. On the public surface the only route is to run `telesthete-hub` with `HUB_WS_BIND` and have the Python hub join the band as a UDP peer behind it.

### e. Streams

Telesthete's Stream (§5) is lossy, prioritized datagrams, sent to every peer in the Band (`band.py:344-346`). It suits audio and video, not a terminal. Its Channel is reliable and ordered but one-to-one. Nothing in Telesthete fans one source out to clients that come and go.

So Rook carries its streams as Rook messages over the worker-hub link (`stream_open`, `stream_data` and `stream_close` in `docs/protocol.md`). The hub does the fan-out to clients (S2), and title, description and lifetime are Rook state (S3, S4, S5). Telesthete has no stream lifetime that would conflict with S4: Channels have no idle timeout. Only a dead peer ends one (condition 5 in a), and that ends the link, not the Rook stream, which the worker owns.

Nothing in S1 to S5 is impossible to carry, given a reliable link. The limits are the ones in (a): the Python Channel's one-peer-per-id rule and its inability to reopen after a restart.

## Where PLAN.md and Telesthete disagree

1. **Keys (Layering, third bullet; R5; W7).** The plan says Telesthete authenticates frames with a key the application supplies, and that Rook decides which keys are accepted, per worker, with revoke and rekey. Telesthete authenticates with one PSK per `Band` and per socket. It has no per-peer key acceptance hook, and in Python no way to share a socket between keys (answer b). Per-worker keys are possible only as one `Band` and one UDP port per worker, or by going outside the public surface. How the link is keyed per worker is now settled in [link-design.md](link-design.md), signed by Bake on 13 November 2026: a key made for each connection over HTTPS (its section 2), which also answers point 2.
2. **"Approval is the hub signing that key" (R5).** Telesthete has no signatures or public keys (signatures are in the plan's out-of-scope list as Telesthete v2 extras). A hub signature on the worker key can be recorded and handed back, but Telesthete cannot check it. Something has to turn the approved keypair into a PSK.
3. **First contact (R1, R2, U2).** The plan does not say how a knock reaches the hub. Telesthete cannot carry one, because there is no shared key yet, so the knock is HTTPS (answer c). This agrees with U2 and is listed only so the reader knows it was checked.
4. **WebSocket fallback (Layering, first bullet: "Telesthete owns ... WebSocket fallback").** Only the Rust relay implements it, and no Telesthete library has a client (answer d). A Python hub cannot offer it through the Python public surface.
5. **ESP32 (W2).** SPEC.md §12.2 (lines 1180-1182) sizes the ESP32 and Kotlin clients as "Stream + Control only" over WebSocket, and §12.1 says a minimal client needs no Channel reliability. W2 needs an ESP32 to answer calls, which needs a reliable exchange. So an ESP32 worker must implement the §6 Channel, which is more than Telesthete's own minimal client, and it needs the WebSocket path from point 4 unless it speaks UDP.
6. **SPEC.md §11.1 (lines 1141-1147)** says Rook's protocol is msgpack. `docs/protocol.md` uses JSON. §11.1 defers to the Rook repository, so this is a stale sentence in Telesthete, not a conflict. Fixing it would mean changing Telesthete, which this pull request does not do.
7. **The Python Channel cannot reopen after a peer restarts (answer a, condition 4).** This is not a plan conflict, but R5 ("no re-pairing after approval") and H1 with H3 (a restarted worker reconnects and heartbeats again) assume a restart is cheap. On the Python surface, a restart means a fresh channel id or a fresh `Band` on the hub side.
