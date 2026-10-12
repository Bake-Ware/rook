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

Rook's own CHANNEL frames are sealed with EMPTY AAD (core spec 2.2: every
deployed peer, the ESP32/RP2040 firmware included, opens them that way). Two
frame kinds added for addressed routing and in-band file transfer use the
telesthete spec AAD ``[channel_type, id_hi, id_lo]`` instead:

* ROUTE (0x05) declarations (:meth:`BandCodec.route_frame`, SPEC §10.1). A
  routing hub consumes them; an old hub relays them, and every rook receiver
  drops them unopened here (old receivers fail the empty-AAD tag check).
* native DROP (0x04) frames (:meth:`BandCodec.seal_spec`, SPEC §8), sent only
  to peers that announce ``drop-native``. :meth:`ingest` opens them and hands
  them on re-wrapped in the pass-through shape :mod:`rook.worker.ota_drop`
  already understands, so OTA handlers accept both forms unchanged.

No I/O happens here, so the codec is unit-testable without sockets.
"""

from __future__ import annotations

import inspect
import logging
from typing import NamedTuple, Optional

import hashlib

from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.fragment import Fragmenter, Reassembler
from telesthete.protocol.framing import ChannelType, pack_packet, unpack_packet
from telesthete.protocol.sequence import SequenceSource

from . import envelope

try:
    from telesthete.protocol.framing import MIN_PACKET_SIZE
except ImportError:  # older vendored telesthete: header 27 + Poly1305/GCM tag 16
    MIN_PACKET_SIZE = 27 + 16

log = logging.getLogger("rook.worker.wire.codec")

# Spec channel types by value: ROUTE (0x05) is newer than some vendored
# telesthete builds, so it is not looked up on the enum.
CHANNEL = int(ChannelType.CHANNEL)
DROP = int(ChannelType.DROP)
ROUTE = 0x05

# What a pass-through Drop packet (rook.worker.ota_drop) carries in place of an
# AEAD tag. A native DROP frame is delivered re-wrapped with it (see ingest).
PASSTHROUGH_TAG = b"\x00" * 16

# -- route ids (SPEC §10.1) --------------------------------------------------
# The library owns these (telesthete.protocol.route, 0.3.0+). The fallback is
# byte-identical (pinned by tests/test_band_protocol.py) and only exists so a
# bundle vendoring an older telesthete still builds valid declarations.
try:
    from telesthete.protocol.route import route_id_for as _lib_route_id_for
except ImportError:  # pragma: no cover - exercised only with old telesthete
    _lib_route_id_for = None

ROUTE_ID_DOMAIN = b"telesthete-route-v1:"


def route_id_for(name: str) -> int:
    """Stable nonzero 16-bit route id for a peer name (SPEC §10.1)."""
    if _lib_route_id_for is not None:
        return _lib_route_id_for(name)
    digest = hashlib.sha256(ROUTE_ID_DOMAIN + name.encode("utf-8")).digest()
    for i in range(0, len(digest), 2):
        word = int.from_bytes(digest[i:i + 2], "big")
        if word:
            return word
    return 1


def spec_aad(channel_type: int, channel_id: int) -> bytes:
    """SPEC §3.2 AAD: ``[channel_type, channel_id_hi, channel_id_lo]``."""
    return bytes([channel_type & 0xFF, (channel_id >> 8) & 0xFF, channel_id & 0xFF])

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
    if "max_fragments" in params:
        # telesthete 0.3.0 caps a message at 16,384 fragments (~16 MB) by
        # default, below the 32 MiB a worker may send inline.
        kw["max_fragments"] = -(-REASSEMBLY_MAX_MESSAGE_BYTES // 1003)
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
        self.drops = {"short": 0, "malformed": 0, "band": 0, "decrypt": 0,
                      "route": 0, "inflate": 0}

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

    def seal_spec(self, plaintext: bytes, channel_type: int, channel_id: int) -> bytes:
        """Seal one unfragmented frame with the SPEC §3.2 AAD (native DROP,
        ROUTE). Never used for rook's CHANNEL traffic (empty AAD there)."""
        seq = self.seq.next()
        return pack_packet(
            band_id=self.band_id,
            channel_type=channel_type,
            channel_id=channel_id,
            sequence=seq,
            ciphertext=self.crypto.encrypt(seq, plaintext,
                                           associated_data=spec_aad(channel_type, channel_id)),
        )

    def route_frame(self, route_id: int) -> bytes:
        """A ROUTE declaration for ``route_id`` (SPEC §10.1): 43 bytes, empty
        payload, spec AAD, sequence from the one shared source. Same bytes as
        ``telesthete.protocol.route.pack_route_declaration``."""
        if not 0 <= route_id <= 0xFFFF:
            raise ValueError(f"route_id must be 0-65535, got {route_id}")
        return self.seal_spec(b"", ROUTE, route_id)

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
        if pkt.channel_type == DROP:
            return self._ingest_drop(pkt)
        try:
            cleartext = self.crypto.decrypt(pkt.sequence, pkt.ciphertext)
        except Exception as e:
            self.drops["decrypt"] += 1
            log.debug("decrypt failed seq=%d: %s", pkt.sequence, e)
            return None
        assembled = self.reassembler.feed(cleartext)
        if assembled is None or assembled == KEEPALIVE_PAYLOAD:
            return None
        if envelope.is_compressed(assembled) and assembled[:16] != self.band_id:
            # Negotiated compression (envelope "z"): only sent to peers that
            # asked for it, so inflating here is invisible to everyone else.
            inflated = envelope.decompress(assembled)
            if inflated is None:
                self.drops["inflate"] += 1
                return None
            assembled = inflated
        return Inbound(assembled, pkt.channel_id, pkt.channel_type)

    def _ingest_drop(self, pkt) -> Optional[Inbound]:
        """Open a native DROP frame (spec AAD) and re-wrap it as the
        pass-through packet the Drop bridge feeds on (same header, the
        plaintext, :data:`PASSTHROUGH_TAG`). Handlers can't tell the two
        forms apart, so an OTA receiver accepts both."""
        try:
            plaintext = self.crypto.decrypt(
                pkt.sequence, pkt.ciphertext,
                associated_data=spec_aad(pkt.channel_type, pkt.channel_id))
        except Exception as e:
            self.drops["decrypt"] += 1
            log.debug("drop frame decrypt failed seq=%d: %s", pkt.sequence, e)
            return None
        wrapped = pack_packet(self.band_id, pkt.channel_type, pkt.channel_id,
                              pkt.sequence, plaintext + PASSTHROUGH_TAG)
        return Inbound(wrapped, pkt.channel_id, pkt.channel_type)
