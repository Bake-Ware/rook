"""Rook's wire-stack helpers (SPEC §6 fragmentation + reassembly).

The telesthete repo owns the *spec*; this package is rook's local
implementation that conforms to it. Wire-format primitives (frame
pack/unpack, AEAD) are still imported from the `telesthete` package
since those are the spec — only the application/control logic
(Channel-level fragmentation, ordering, ack/window — currently just
fragmentation) lives here.
"""

from .channel import Fragmenter, Reassembler, fragment, HEADER_SIZE, MAX_CHUNK_PAYLOAD

__all__ = ["Fragmenter", "Reassembler", "fragment",
           "HEADER_SIZE", "MAX_CHUNK_PAYLOAD"]
