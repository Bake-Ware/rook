# Example ports

Two minimal Rook workers written from [the core spec](../../docs/spec/core-v1.md),
each in its language's idiom:

| Port | Runtime deps | Build | Offline vectors | Live run |
|---|---|---|---|---|
| [typescript/](typescript/) | none (Node ≥ 23.6 runs `.ts` directly) | none | `npm test` | `node src/main.ts` |
| [rust/](rust/) | chacha20poly1305, hkdf, sha2, ed25519-dalek, serde_json, base64, getrandom | `cargo build --release` | `cargo test` | `target/release/rook-port` |

Both implement the same subset:

- the band transport over UDP to the relay (keys, frames, fragmentation,
  keepalive), section 2;
- the worker dispatch rules and announces, section 3;
- a cap registry with declared params, risk tiers, `caps.describe` and the
  `limit`/`fields` contract, section 4;
- placement expressions, section 5;
- grant, signed-announce and ticket verification, section 6 (used live to
  find the real hub worker `rook`; they don't enforce tickets on incoming
  calls and announce `authz.mode = "off"`);
- calls to `rook`'s chat room caps, section 7.

Each exposes `port.info` plus the conformance caps (`conformance.echo`,
`conformance.add`, `conformance.chat_post`, `conformance.chat_read`), passes
all eleven vector files, and passes the live harness 18/18 against a local
test hub. Start from either one: replace the conformance caps with your own.

Configuration is by environment (the candidate contract in
[conformance/README.md](../../conformance/README.md)):
`ROOK_RELAY=host:port`, `ROOK_PSK`, `ROOK_NAME`, `ROOK_IDENTITY`,
`ROOK_ANCHOR` (the hub's root public key, base64; without it `rook` is found
by name only), `ROOK_ANNOUNCE_SECS`, `ROOK_WORKER_ID` (else a random id per
start).

What they leave out, deliberately: the WebSocket bridge (UDP only), worker
state persistence (a stable `worker_id` comes from `ROOK_WORKER_ID`), ticket
enforcement, the audit log, OTA, and a plugin host (caps are registered
directly).
