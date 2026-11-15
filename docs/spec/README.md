# Rook core specification

Rook's core is small enough to reimplement: an encrypted band of peers, a JSON
call/reply/announce protocol, and dot-named capabilities. This directory
specifies it so another application can adopt the design, in any language.

| Document | What it is |
|---|---|
| [core-v2.md](core-v2.md) | **Core v2.0 (Castling, Rook 0.5), draft.** Sections 2 to 9: what every hub and worker must do (transport, announces and heartbeats with manifests, calls, dispatch, the journal, enrollment). Section 10: the optional extensions (tickets and signatures, worker keys, events, streams, transport extensions, signed updates, appliances; elevation and several bands, parked). Vectors in [../../conformance/core-v2/](../../conformance/core-v2/README.md). A clean cut: v1 and v2 peers don't interoperate. |
| [worker-profile.md](worker-profile.md) | **The minimal worker profile**: join, announce caps, answer calls, heartbeat. All a worker (firmware, a script, a native module) must do; the rest of core v2 is optional for workers. |
| [core-v1.md](core-v1.md) | The v1.0 specification. The sections core-v2.md 2.1 lists (its transport, mostly) stay normative under v2, with their vectors in [../../conformance/vectors/](../../conformance/README.md); the rest is history: the 0.4 reference it was written from is gone. |
