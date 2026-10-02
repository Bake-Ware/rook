# Rook core specification

Rook's core is small enough to reimplement: an encrypted band of peers, a JSON
call/reply/announce protocol, dot-named capabilities with risk tiers, placement
over node facts, a hub worker `rook` proven by signed grants, and persistent
chat rooms that agents, people and workers share. This directory specifies it
so another application can adopt the design, in any language, and still talk
to Python Rook hubs and workers.

| Document | What it is |
|---|---|
| [core-v1.md](core-v1.md) | The specification (v1.0), written from the Python reference code. RFC 2119 language, wire examples. |
| [../../conformance/](../../conformance/README.md) | Language-neutral test vectors generated from the reference, and a harness that runs a candidate against a reference hub. |
| [../../examples/ports/](../../examples/ports/README.md) | Two minimal workers, TypeScript (Node, no runtime dependencies) and Rust, that pass every vector and the live run. |

## Using the design in your application

1. **Join a band.** Implement section 2 of the spec: SHA-256/HKDF from the
   PSK, ChaCha20-Poly1305 frames with empty associated data, the 21-byte
   fragmentation envelope, a UDP socket to the relay (or the `/band`
   WebSocket bridge). About 150 lines in either example port. Check it
   against `band_crypto.json`, `fragments.json` and `frames.json`.
2. **Be a worker.** Announce every 30 s with your caps, answer requests with
   the dispatch rules of section 3.1 (they decide when to stay silent), and
   implement `caps.describe`. Check it against `messages.json`.
3. **Add caps.** Name them `<yourapp>.<verb>`, declare parameters and a risk
   tier, and declare `limit`/`fields` for list-shaped results (section 4).
   Hub agents find them through `rook_caps` and call them with `rook_call`
   straight away; no hub change is needed.
4. **Use the hub.** Resolve the worker `rook` by its signed `is_hub` grant
   (section 6.1, 6.5) and call its caps: chat rooms (`chat.read`,
   `chat.write`), the shared wiki (`knowledge.*`) and tasks (`task.*`). Band
   calls to `rook` are unauthenticated, so the hub serves only up to its band
   risk ceiling (`ROOK_HUB_BAND_MAX_RISK`, default read); posting to chat
   over the band needs it at `write`.
5. **Prove it.** Run the offline vectors in your test suite and
   `conformance/harness.py --candidate "<your worker command>"` against a
   throwaway hub (`scripts/test-hub.sh`).

Staying compatible: ignore keys you don't know, add only optional keys, never
put `cap` on a message that isn't a request (section 10).

## Changing the spec

The spec follows the Python reference, not the other way round. A change to
wire behaviour or to `rook.core` shows up as a diff in
`conformance/vectors/` (`python conformance/generate.py`; the unit suite
fails while the committed vectors are stale). Update `core-v1.md` in the same
change, bump the spec minor version for additive changes, and keep the
example ports passing.
