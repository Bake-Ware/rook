"""Reference implementation of the pure functions in Rook core v2
(docs/spec/core-v2.md): canonical JSON, signed objects (tickets, leases,
memberships, hub keys), version negotiation, rights and presets, gate
verdicts and the journal hash chain.

No I/O and no state beyond what callers pass in, so other languages can port
it one function at a time against conformance/core-v2/vectors.
"""

CORE_VERSION = "2.0"
