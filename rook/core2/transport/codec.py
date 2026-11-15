"""Band frame codec: everything between a payload and a wire frame.

It owns the band crypto, the per-sender sequence source, the §6.6
fragmenter and the reassembler:

* outbound: :meth:`BandCodec.split` cuts a payload into fragment chunks, and
  :meth:`BandCodec.seal` turns ONE chunk into one AEAD-sealed CHANNEL frame
  (or, addressed to a route id, one ADDRESSED frame).
  Sealing is per chunk (not per message) so a writer can assign sequences at
  emission time and interleave the fragments of different messages: the
  receiver reassembles by ``fragment_id``, never by arrival order.
* inbound: :meth:`BandCodec.ingest` takes one raw frame and returns the
  completed message it finishes (an :class:`Inbound`), or ``None``.

Rook's CHANNEL frames are sealed with EMPTY AAD (core spec 2.2: every
deployed peer, the ESP32/RP2040 firmware included, opens them that way).
ADDRESSED (0x06) frames (a message's chunks sent to one peer's declared
route id, SPEC §10.1, core spec 14.1) are sealed the same way, so a
receiver that ignores ``channel_type`` still opens one a broadcasting hub
relays to it; ``channel_id`` is the destination route id. ROUTE (0x05)
declarations (:meth:`BandCodec.route_frame`) use the telesthete spec AAD
``[channel_type, id_hi, id_lo]``; a routing hub consumes them and every
rook receiver drops them unopened here.

No I/O happens here, so the codec is unit-testable without sockets.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.fragment import Fragmenter, Reassembler
from telesthete.protocol.framing import MIN_PACKET_SIZE, ChannelType, pack_packet, unpack_packet
from telesthete.protocol.route import route_id_for
from telesthete.protocol.sequence import SequenceSource

log = logging.getLogger("rook.core2.transport.codec")

CHANNEL = int(ChannelType.CHANNEL)
ROUTE = 0x05
ADDRESSED = 0x06

__all__ = ["BandCodec", "Inbound", "KEEPALIVE_PAYLOAD", "route_id_for"]


def spec_aad(channel_type: int, channel_id: int) -> bytes:
    """SPEC §3.2 AAD: ``[channel_type, channel_id_hi, channel_id_lo]``."""
    return bytes([channel_type & 0xFF, (channel_id >> 8) & 0xFF, channel_id & 0xFF])

# A bare 1-byte payload still goes through the fragmenter (single fragment) so
# the wire shape is uniform. Receivers detect keepalives on the assembled
# payload, never by sniffing inside frames (core spec 2.5).
KEEPALIVE_PAYLOAD = b"\x00"

# Reassembly bounds. 32 MiB is far above any real reply (bulk rides Drop in
# 1 KiB chunks) and well under the 65535 x 1003 B the envelope can express.
REASSEMBLY_TIMEOUT = 30.0
REASSEMBLY_BUFFERS = 256
REASSEMBLY_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
REASSEMBLY_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _reassembler() -> Reassembler:
    return Reassembler(timeout=REASSEMBLY_TIMEOUT, buffer_limit=REASSEMBLY_BUFFERS,
                       max_fragments=-(-REASSEMBLY_MAX_MESSAGE_BYTES // 1003),
                       max_buffered_bytes=REASSEMBLY_MAX_TOTAL_BYTES)


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
        # SECURITY: the band key is HKDF(PSK), one static key shared by every
        # peer, and the AEAD nonce is 4 zero bytes || the 8-byte sequence. If
        # every peer started at 0 they would reuse (key, nonce) pairs, which is
        # catastrophic for ChaCha20-Poly1305/AES-GCM. SequenceSource starts at
        # a CSPRNG-seeded 63-bit value and increments atomically, so cross-peer
        # and cross-restart collisions are negligible.
        self.seq = SequenceSource()
        self.fragmenter = Fragmenter()
        self.reassembler = _reassembler()
        # Why frames were dropped on ingest (cheap counters for debugging).
        self.drops = {"short": 0, "malformed": 0, "band": 0, "decrypt": 0,
                      "route": 0, "type": 0}

    # -- outbound ------------------------------------------------------------

    def split(self, payload: bytes) -> list[bytes]:
        """Fragment ``payload`` into §6.6 chunks (always at least one)."""
        return self.fragmenter.split(payload)

    def seal(self, chunk: bytes, route: int = 0) -> bytes:
        """Encrypt one chunk under a fresh sequence and frame it, empty AAD.

        ``route`` 0 makes a CHANNEL frame on ``channel_id`` 0 (broadcast);
        a nonzero ``route`` makes an ADDRESSED frame to that route id, which a
        routing hub delivers only to the peer(s) that declared it.
        """
        if not 0 <= route <= 0xFFFF:
            raise ValueError(f"route must be 0-65535, got {route}")
        seq = self.seq.next()
        return pack_packet(
            band_id=self.band_id,
            channel_type=ADDRESSED if route else CHANNEL,
            channel_id=route,
            sequence=seq,
            ciphertext=self.crypto.encrypt(seq, chunk),
        )

    def frames(self, payload: bytes, route: int = 0) -> list[bytes]:
        """``split`` + ``seal`` every chunk: the whole message as frames."""
        return [self.seal(c, route) for c in self.split(payload)]

    def route_frame(self, route_id: int) -> bytes:
        """A ROUTE declaration for ``route_id`` (SPEC §10.1): 43 bytes, empty
        payload, spec AAD, sequence from the one shared source. Same bytes as
        ``telesthete.protocol.route.pack_route_declaration``."""
        if not 0 <= route_id <= 0xFFFF:
            raise ValueError(f"route_id must be 0-65535, got {route_id}")
        seq = self.seq.next()
        return pack_packet(
            band_id=self.band_id,
            channel_type=ROUTE,
            channel_id=route_id,
            sequence=seq,
            ciphertext=self.crypto.encrypt(seq, b"", associated_data=spec_aad(ROUTE, route_id)),
        )

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
        if pkt.channel_type == ROUTE:
            # A declaration an old hub relayed: hub-consumed, never ours (§2).
            self.drops["route"] += 1
            return None
        # CHANNEL (0x02) and ADDRESSED (0x06) are both empty-AAD rook
        # messages; an ADDRESSED frame reaches us because we declared its
        # route id, or as a broadcast from an old hub (then it may be someone
        # else's: callers still match replies by id and requests by target).
        # Any other type can't be ours: drop it without spending a decrypt.
        if pkt.channel_type not in (CHANNEL, ADDRESSED):
            self.drops["type"] += 1
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
