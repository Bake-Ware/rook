"""Backward-compat shim — the fragmentation envelope now lives in the library.

This module was rook's local copy of the §6.6 Channel fragmentation envelope.
It was byte-identical to `telesthete.protocol.fragment`, which is now a real
dependency, so the copy is gone and everything re-exports the library. Kept as
a shim so any `from rook.worker.wire.channel import ...` keeps resolving.
"""

from telesthete.protocol.fragment import (  # noqa: F401
    Fragmenter,
    Reassembler,
    fragment,
    pack_chunk,
    parse_chunk,
    HEADER_SIZE,
    MAX_CHUNK_PAYLOAD,
    VERSION,
    DEFAULT_REASSEMBLY_TIMEOUT,
    DEFAULT_BUFFER_LIMIT,
)
