"""Fragmentation + reassembly for Telesthete Channel (SPEC §6).

Each logical message is split into <=1024-byte chunks (matching the spec's
"Maximum packet payload: 1024 bytes" line). Every chunk becomes the payload
of a single Telesthete CHANNEL frame, AEAD-encrypted independently. The
receiver decrypts each frame, feeds the cleartext chunk through
:class:`Reassembler`, and gets the full payload back once every chunk for
that ``fragment_id`` has arrived.

Wire layout of one chunk (inside Telesthete CHANNEL ciphertext):

    Offset  Size  Field
    0       1     version   (currently 0x01)
    1       16    fragment_id (random per logical message)
    17      2     seq       chunk index, uint16 BE, 0-based
    19      2     total     total chunks, uint16 BE, >=1
    21      var   data      raw chunk bytes

A single-fragment message (payload <= MAX_CHUNK_PAYLOAD) still uses the
envelope with seq=0 total=1 — keeps the parser stateless. Per-frame
overhead is 21 bytes.

This is a *proof-of-concept* implementation, not full Channel reliability.
Backing it later with ACK/window/retransmit (SPEC §6.4) is straightforward
once we agree the fragmentation envelope and reassembly buffer model.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger("rook.worker.wire.channel")

VERSION = 0x01
_HEADER = struct.Struct(">B16sHH")
HEADER_SIZE = _HEADER.size  # 21 bytes

# 1024 byte total chunk (SPEC §6.4) - 21 byte fragment envelope.
MAX_CHUNK_PAYLOAD = 1024 - HEADER_SIZE  # 1003

DEFAULT_REASSEMBLY_TIMEOUT = 30.0
DEFAULT_BUFFER_LIMIT = 256  # bound memory: at most N concurrent in-flight messages


@dataclass
class _Partial:
    total: int
    parts: dict[int, bytes] = field(default_factory=dict)
    first_seen: float = 0.0

    def complete(self) -> bool:
        return len(self.parts) == self.total

    def assemble(self) -> bytes:
        return b"".join(self.parts[i] for i in range(self.total))


def fragment(payload: bytes,
             chunk_size: int = MAX_CHUNK_PAYLOAD) -> list[bytes]:
    """Split `payload` into one or more wire chunks. Returns a list of
    ``HEADER + data`` byte strings ready to be encrypted + framed by the
    Telesthete CHANNEL layer."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    fid = os.urandom(16)
    if not payload:
        return [_HEADER.pack(VERSION, fid, 0, 1)]
    out: list[bytes] = []
    pieces = [payload[i:i + chunk_size]
              for i in range(0, len(payload), chunk_size)]
    if len(pieces) > 0xFFFF:
        raise ValueError("payload too large; max 65535 fragments")
    total = len(pieces)
    for seq, piece in enumerate(pieces):
        out.append(_HEADER.pack(VERSION, fid, seq, total) + piece)
    return out


class Fragmenter:
    """Convenience wrapper — same as :func:`fragment` plus optional reuse of
    an existing ``fragment_id`` (e.g. when retransmitting)."""

    def __init__(self, chunk_size: int = MAX_CHUNK_PAYLOAD) -> None:
        self.chunk_size = chunk_size

    def split(self, payload: bytes) -> list[bytes]:
        return fragment(payload, self.chunk_size)


class Reassembler:
    """Stateful buffer that collects fragments and emits full payloads.

    Thread-safety: not thread-safe; intended for use from a single asyncio
    task. The transport layer that owns it is single-task by construction.
    """

    def __init__(self,
                 timeout: float = DEFAULT_REASSEMBLY_TIMEOUT,
                 buffer_limit: int = DEFAULT_BUFFER_LIMIT) -> None:
        self._buffers: dict[bytes, _Partial] = {}
        self._timeout = timeout
        self._limit = buffer_limit

    def feed(self, chunk: bytes) -> bytes | None:
        """Feed one decrypted Telesthete CHANNEL payload. Returns the full
        assembled message if this chunk completes one, otherwise ``None``.
        Junk / wrong-version chunks return ``None`` and are dropped."""
        if len(chunk) < HEADER_SIZE:
            log.debug("chunk too short (%d B), dropping", len(chunk))
            return None
        version, fid, seq, total = _HEADER.unpack_from(chunk, 0)
        if version != VERSION:
            log.debug("unknown wire version %d, dropping", version)
            return None
        if total == 0 or seq >= total:
            log.debug("nonsense fragment seq=%d total=%d", seq, total)
            return None
        data = chunk[HEADER_SIZE:]

        # GC stale buffers opportunistically (cheap walk; counts on map size
        # staying small thanks to the buffer_limit cap).
        self._gc_stale()

        buf = self._buffers.get(fid)
        if buf is None:
            if len(self._buffers) >= self._limit:
                # Evict the oldest to keep memory bounded.
                oldest = min(self._buffers.items(),
                             key=lambda kv: kv[1].first_seen)
                log.warning("reassembly buffer full; evicting fragment_id=%s",
                             oldest[0].hex()[:8])
                del self._buffers[oldest[0]]
            buf = _Partial(total=total, first_seen=time.monotonic())
            self._buffers[fid] = buf
        elif buf.total != total:
            # A peer changed total mid-message — corrupt. Reset that buffer.
            log.warning("fragment_id=%s total mismatch (%d→%d), resetting",
                         fid.hex()[:8], buf.total, total)
            buf = _Partial(total=total, first_seen=time.monotonic())
            self._buffers[fid] = buf

        if seq in buf.parts:
            return None  # duplicate, ignore silently
        buf.parts[seq] = data
        if not buf.complete():
            return None
        del self._buffers[fid]
        return buf.assemble()

    def _gc_stale(self) -> None:
        if not self._buffers:
            return
        now = time.monotonic()
        cutoff = now - self._timeout
        stale = [k for k, v in self._buffers.items() if v.first_seen < cutoff]
        for k in stale:
            v = self._buffers[k]
            log.warning("dropping incomplete reassembly: fragment_id=%s "
                         "parts=%d/%d age=%.1fs",
                         k.hex()[:8], len(v.parts), v.total,
                         now - v.first_seen)
            del self._buffers[k]
