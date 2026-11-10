"""Reference implementation of the pure functions in Rook core v2
(docs/spec/core-v2.md): canonical JSON, signed objects (tickets, leases,
memberships, hub keys), version negotiation, rights and presets and the
journal hash chain.

No I/O and no state beyond what callers pass in, so other languages can port
it one function at a time against conformance/core-v2/vectors.
"""

CORE_VERSION = "2.0"
# The Rook release a hub or worker shows (the Workers page): 0.5, "Castling".
# CORE_VERSION is the wire protocol, not the release (Bake's review, Nov 9).
ROOK_VERSION = "0.5.0"
