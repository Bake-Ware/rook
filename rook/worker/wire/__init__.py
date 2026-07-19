"""Rook's wire-stack helpers — now a thin re-export of the telesthete library.

The telesthete repo owns the *spec* AND the reference implementation. This
package used to carry rook's own conforming copy of the §6.6 fragmentation
envelope; that copy was byte-identical to `telesthete.protocol.fragment`, so
it has been retired in favour of the library (single source of truth, and rook
picks up the reviewed/hardened Reassembler — bounded eviction + stale GC — for
free). The worker bundle already vendors `telesthete.protocol`, so this import
resolves in the pyz and the Android source set alike.
"""

from telesthete.protocol.fragment import (
    Fragmenter,
    Reassembler,
    fragment,
    HEADER_SIZE,
    MAX_CHUNK_PAYLOAD,
)

__all__ = ["Fragmenter", "Reassembler", "fragment",
           "HEADER_SIZE", "MAX_CHUNK_PAYLOAD"]
