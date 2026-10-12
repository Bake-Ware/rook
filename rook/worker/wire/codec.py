"""Band frame codec: everything between a payload and a wire frame.

Shared by every link of :class:`rook.worker.transports.telesthete_hub.
TelestheteHubTransport` (UDP and WS used to carry two copies of the ingest
pipeline). It owns the band crypto, the per-sender sequence source, the §6.6
fragmenter and the reassembler:

* outbound: :meth:`BandCodec.split` cuts a payload into fragment chunks, and
  :meth:`BandCodec.seal` turns ONE chunk into one AEAD-sealed CHANNEL frame.
  Sealing is per chunk (not per message) so a writer can assign sequences at
  emission time and interleave the fragments of different messages — the
  receiver reassembles by ``fragment_id``, never by arrival order.
* inbound: :meth:`BandCodec.ingest` takes one raw frame and returns the
  completed message it finishes (an :class:`Inbound`), or ``None``.

No I/O happens here, so the codec is unit-testable without sockets.
"""

from __future__ import annotations

import inspect
import logging
from typing import NamedTuple, Optional

from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.fragment import Fragmenter, Reassembler
from telesthete.protocol.framing import ChannelType, pack_packet, unpack_packet
from telesthete.protocol.sequence import SequenceSource

try:
    from telesthete.protocol.framing import MIN_PACKET_SIZE
except ImportError:  # older vendored telesthete: header 27 + Poly1305/GCM tag 16
    MIN_PACKET_SIZE = 27 + 16

log = logging.getLogger("rook.worker.wire.codec")

# A bare 1-byte payload still goes through the fragmenter (single fragment) so
# the wire shape is uniform. Receivers detect keepalives on the assembled
# payload, never by sniffing inside frames (core spec 2.5).
KEEPALIVE_PAYLOAD = b"\x00"

# Reassembly bounds. The library defaults are passed explicitly so they are
# visible here; byte budgets are only passed if this telesthete build accepts
# them (feature-detected, see _reassembler()). 32 MiB is far above any real
# reply (OTA bundles ride Drop in 1 KiB chunks) and well under the 65535 x
# 1003 B the envelope can express.
REASSEMBLY_TIMEOUT = 30.0
REASSEMBLY_BUFFERS = 256
REASSEMBLY_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
REASSEMBLY_MAX_TOTAL_BYTES = 64 * 1024 * 1024

# Candidate keyword names for byte budgets in a newer Reassembler. Whichever
# exist are passed; none existing is fine (count/timeout bounds still apply).
_PER_MESSAGE_KW = ("max_message_bytes", "message_byte_limit", "max_message_size")
_TOTAL_KW = ("max_total_bytes", "max_buffered_bytes", "byte_budget",
             "total_byte_limit", "byte_limit", "max_bytes")


def _reassembler() -> Reassembler:
    try:
        params = inspect.signature(Reassembler).parameters
    except (TypeError, ValueError):
        return Reassembler()
    kw: dict = {}
    if "timeout" in params:
        kw["timeout"] = REASSEMBLY_TIMEOUT
    if "buffer_limit" in params:
        kw["buffer_limit"] = REASSEMBLY_BUFFERS
    for name in _PER_MESSAGE_KW:
        if name in params:
            kw[name] = REASSEMBLY_MAX_MESSAGE_BYTES
            break
    for name in _TOTAL_KW:
        if name in params:
            kw[name] = REASSEMBLY_MAX_TOTAL_BYTES
            break
    return Reassembler(**kw)


class Inbound(NamedTuple):
    """One reassembled message off the band."""
    payload: bytes
    channel_id: int
    channel_type: int


class BandCodec:
    """Crypto + sequence + fragmentation for one band (one PSK)."""

    def __init__(self, psk: str) -> None:
        self.crypto = BandCrypto(psk)
        self.band_id: bytes = self.crypto.band_id
        # SECURITY: the band key is HKDF(PSK) — one static key shared by every
        # peer — and the AEAD nonce is 4 zero bytes || the 8-byte sequence. If
        # every peer started at 0 they would reuse (key, nonce) pairs, which is
        # catastrophic for ChaCha20-Poly1305/AES-GCM. SequenceSource starts at
        # a CSPRNG-seeded 63-bit value and increments atomically, so cross-peer
        # and cross-restart collisions are negligible. Wire-compatible: the
        # sequence travels in the frame and the receiver derives the nonce.
        self.seq = SequenceSource()
        self.fragmenter = Fragmenter()
        self.reassembler = _reassembler()
        # Why frames were dropped on ingest (cheap counters for debugging).
        self.drops = {"short": 0, "malformed": 0, "band": 0, "decrypt": 0}

    # -- outbound ------------------------------------------------------------

    def split(self, payload: bytes) -> list[bytes]:
        """Fragment ``payload`` into §6.6 chunks (always at least one)."""
        return self.fragmenter.split(payload)

    def seal(self, chunk: bytes, channel_id: int = 0,
             channel_type: int = ChannelType.CHANNEL) -> bytes:
        """Encrypt one chunk under a fresh sequence and frame it."""
        seq = self.seq.next()
        return pack_packet(
            band_id=self.band_id,
            channel_type=channel_type,
            channel_id=channel_id,
            sequence=seq,
            ciphertext=self.crypto.encrypt(seq, chunk),
        )

    def frames(self, payload: bytes, channel_id: int = 0) -> list[bytes]:
        """``split`` + ``seal`` every chunk: the whole message as frames."""
        return [self.seal(c, channel_id) for c in self.split(payload)]

    # -- inbound -------------------------------------------------------------

    def ingest(self, data: bytes) -> Optional[Inbound]:
        """Run one received frame through the pipeline: length check, unpack,
        band check, decrypt, reassemble, keepalive filter. Returns the message
        this frame completes, or ``None`` (partial, keepalive, or dropped)."""
        # A legal frame is header (27) + AEAD tag (16) at least; anything
        # shorter cannot authenticate, so drop it before touching it.
        if len(data) < MIN_PACKET_SIZE:
            self.drops["short"] += 1
            return None
        try:
            pkt = unpack_packet(data)
        except Exception as e:
            self.drops["malformed"] += 1
            log.debug("bad frame from hub: %s", e)
            return None
        if pkt.band_id != self.band_id:
            self.drops["band"] += 1
            return None
        try:
            cleartext = self.crypto.decrypt(pkt.sequence, pkt.ciphertext)
        except Exception as e:
            self.drops["decrypt"] += 1
            log.debug("decrypt failed seq=%d: %s", pkt.sequence, e)
            return None
        assembled = self.reassembler.feed(cleartext)
        if assembled is None or assembled == KEEPALIVE_PAYLOAD:
            return None
        return Inbound(assembled, pkt.channel_id, pkt.channel_type)
