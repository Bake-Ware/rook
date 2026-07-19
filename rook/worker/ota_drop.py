"""In-band bundle transfer over the band, using telesthete Drop (SPEC §8).

The worker's OTA path historically fetched the bundle over HTTP (a signed
manifest is pushed in-band, but the *bytes* came from a URL). That fails on a
worker behind strict NAT, on a network that blocks the CDN, or wherever the
installer host simply isn't reachable. This module carries the bundle **in
band** instead: the controller offers the file with telesthete's receiver-driven
`DropSender`, the worker pulls the chunks it lacks with `DropReceiver`
(resumable across restarts, sha256-verified), and the existing signed-manifest
verification remains the trust anchor — the transport is untrusted either way.

Design notes:
  * The rook hub transport already AEAD-encrypts and authenticates every band
    payload, so Drop rides it with a **pass-through crypto**: the Drop frames
    are cleartext *inside* rook's encrypted envelope. This avoids double AEAD
    and, crucially, avoids a cross-layer nonce collision — Drop and the
    transport would otherwise both drive `BandCrypto(psk)` from independent
    sequence counters and reuse `(key, nonce)` pairs.
  * The hub is a broadcast relay with no per-peer addressing, so both endpoints
    speak to a single sentinel peer (`_BAND_PEER`). Transfers are scoped by
    ``drop_id``: only the target worker (told the id via a signed handshake)
    arms a receiver for it; other workers drop unknown-id Drop packets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional

from telesthete.protocol.drop import DropReceiver, DropSender
from telesthete.protocol.framing import ChannelType, unpack_packet
from telesthete.protocol.sequence import SequenceSource

log = logging.getLogger("rook.worker.ota_drop")

# Single logical peer for the relay (see module docstring): the hub broadcasts,
# so there is no meaningful per-peer address. Scoping is by drop_id.
_BAND_PEER = ("band", 0)

# How often the receiver re-requests chunks that never arrived (§8.2). Kept
# short: the relay path is lossy and reordering, and a stalled OTA is worse
# than a few redundant REQUESTs.
_TICK_SECS = 0.5


# The AEAD would append a 16-byte tag; the framing layer enforces a 43-byte
# minimum packet (27-byte header + 16-byte tag). A pass-through must mimic that
# tag or a tiny frame (e.g. a 7-byte final chunk) produces a sub-43-byte packet
# that `unpack_packet` rejects. A fixed, always-stripped 16-byte suffix keeps
# every packet at or above the minimum without a real MAC (rook's transport is
# the authenticating layer).
_FAKE_TAG = b"\x00" * 16


class _PassthroughCrypto:
    """Satisfies the telesthete Drop crypto interface without encrypting: the
    rook transport is the encryption layer (see module docstring). ``band_id``
    is read by Drop when packing/checking packets."""

    def __init__(self, band_id: bytes) -> None:
        self.band_id = band_id

    def encrypt(self, seq: int, plaintext: bytes, aad: bytes) -> bytes:
        return bytes(plaintext) + _FAKE_TAG

    def decrypt(self, seq: int, ciphertext: bytes, aad: bytes) -> bytes:
        if len(ciphertext) < len(_FAKE_TAG):
            raise ValueError("drop frame shorter than tag")
        return bytes(ciphertext[:-len(_FAKE_TAG)])


class _RelayShim:
    """Adapts telesthete Drop's synchronous ``transport.send(dest, packet)`` to
    the rook hub transport's async ``send(payload)``. The dest is ignored (the
    relay broadcasts by band_id)."""

    def __init__(self, send_coro: Callable[[bytes], Awaitable[None]]) -> None:
        self._send_coro = send_coro
        self._inflight: set[asyncio.Task] = set()

    def send(self, dest: tuple, packet_bytes: bytes) -> None:
        task = asyncio.ensure_future(self._safe_send(packet_bytes))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _safe_send(self, packet_bytes: bytes) -> None:
        try:
            await self._send_coro(packet_bytes)
        except Exception:
            log.debug("drop frame send failed (transport reconnecting?)", exc_info=True)


def is_drop_packet(payload: bytes, band_id: bytes) -> bool:
    """True if ``payload`` is a telesthete DROP-channel packet for our band.
    Lets the worker's JSON dispatch hand binary Drop traffic to this module
    instead of trying to ``json.loads`` it."""
    if len(payload) < 27 or payload[:16] != band_id:
        return False
    return payload[16] == int(ChannelType.DROP)


class OtaDropReceiver:
    """Worker-side: pulls one offered bundle over the band for a given drop_id.

    Constructed when a signed push handshake arrives (``worker.ota_begin``).
    Feed it inbound DROP packets via :meth:`feed`; it drives REQUEST/CHUNK/DONE
    and calls ``on_complete(data, sha_ok)`` once the file is whole and verified.
    """

    def __init__(self, band_id: bytes, drop_id: int,
                 send_coro: Callable[[bytes], Awaitable[None]],
                 have: Optional[Dict[int, bytes]] = None) -> None:
        self.band_id = band_id
        self.drop_id = drop_id
        self._shim = _RelayShim(send_coro)
        self._crypto = _PassthroughCrypto(band_id)
        self._recv = DropReceiver(
            band_id, drop_id, have=have,
            crypto=self._crypto, transport=self._shim,
            seq_source=SequenceSource(),
        )
        self._tick_task: Optional[asyncio.Task] = None
        self._done = asyncio.Event()
        self._user_cb: Optional[Callable[[bytes, bool], None]] = None
        # Always wire our own completion so wait() works whether or not a caller
        # registered a callback.
        self._recv.on_complete(self._on_complete)

    def _on_complete(self, data: bytes, ok: bool) -> None:
        self._done.set()
        if self._user_cb is not None:
            self._user_cb(data, ok)

    def on_complete(self, cb: Callable[[bytes, bool], None]) -> None:
        self._user_cb = cb

    def start(self) -> None:
        """Begin the re-request tick loop. Idempotent."""
        if self._tick_task is None:
            self._tick_task = asyncio.ensure_future(self._tick_loop())

    def feed(self, packet_bytes: bytes) -> None:
        """Route one inbound DROP packet for this drop_id into the receiver."""
        try:
            pkt = unpack_packet(packet_bytes)
        except Exception:
            return
        if pkt.channel_type != ChannelType.DROP or pkt.channel_id != self.drop_id:
            return
        self._recv.handle_packet(_BAND_PEER, packet_bytes)

    @property
    def verified(self) -> Optional[bool]:
        return self._recv.verified

    async def wait(self, timeout: float) -> bool:
        """Block until the transfer completes (or times out). Returns whether
        it completed and verified."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._recv.verified is True

    async def _tick_loop(self) -> None:
        while not self._done.is_set():
            try:
                await asyncio.sleep(_TICK_SECS)
                self._recv.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                log.debug("drop receiver tick failed", exc_info=True)

    def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None


class OtaDropSender:
    """Controller-side: offers one bundle to the band for a given drop_id.

    Feed it inbound DROP packets via :meth:`feed` (REQUEST/DONE come back from
    the receiver); it serves the requested chunks. ``on_complete(sha_ok)`` fires
    when the receiver reports its DONE verdict.
    """

    def __init__(self, band_id: bytes, drop_id: int, name: str, data: bytes,
                 send_coro: Callable[[bytes], Awaitable[None]]) -> None:
        self.band_id = band_id
        self.drop_id = drop_id
        self._shim = _RelayShim(send_coro)
        self._crypto = _PassthroughCrypto(band_id)
        self._sender = DropSender(
            band_id, drop_id, name, data,
            crypto=self._crypto, transport=self._shim,
            seq_source=SequenceSource(),
        )
        self._done = asyncio.Event()
        self._ok = False
        self._user_cb: Optional[Callable[[bool], None]] = None
        # Always wire our own completion so wait() resolves on the receiver's
        # DONE regardless of whether a caller registered a callback.
        self._sender.on_complete(self._on_complete)

    @property
    def sha256(self) -> str:
        return self._sender.sha256

    @property
    def total_chunks(self) -> int:
        return self._sender.total_chunks

    def _on_complete(self, peer: tuple, ok: bool) -> None:
        self._ok = ok
        self._done.set()
        if self._user_cb is not None:
            self._user_cb(ok)

    def on_complete(self, cb: Callable[[bool], None]) -> None:
        self._user_cb = cb

    def offer(self) -> None:
        """Announce the file to the band (§8.2 OFFER)."""
        self._sender.offer(_BAND_PEER)

    def feed(self, packet_bytes: bytes) -> None:
        try:
            pkt = unpack_packet(packet_bytes)
        except Exception:
            return
        if pkt.channel_type != ChannelType.DROP or pkt.channel_id != self.drop_id:
            return
        self._sender.handle_packet(_BAND_PEER, packet_bytes)

    async def wait(self, timeout: float) -> bool:
        """Block until the receiver reports DONE (or timeout). Returns the
        receiver's sha-verified verdict."""
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._ok
