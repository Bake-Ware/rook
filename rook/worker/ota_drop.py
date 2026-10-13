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
  * Both endpoints speak to a single sentinel peer (`_BAND_PEER`); transfers
    are scoped by ``drop_id``: only the target worker (told the id via a signed
    handshake) arms a receiver for it; other workers drop unknown-id packets.
    When the far end declares a route (telesthete SPEC §10.1) the shim sends
    each packet as ADDRESSED frames to its route id, so a routing hub delivers
    the transfer to that peer only instead of to the whole band.
  * **Native DROP frames.** A pass-through Drop packet (27 B header + 1029 B
    CHUNK + 16 B stand-in tag) is bigger than one 1003 B fragment, so every
    chunk costs two CHANNEL frames. A peer that announces ``drop-native``
    takes Drop as ONE native ``ChannelType.DROP`` outer frame per packet,
    really sealed (spec AAD) by the transport with its shared sequence source
    (:meth:`TelestheteHubTransport.send_native`). Native frames are never
    addressed (the hub routes only ADDRESSED frames) and are broadcast; old
    receivers fail their tag check and drop them. The receiving codec re-wraps
    them into the pass-through shape, so receivers accept both forms.

The same machinery carries **large replies** (:class:`DropReplyServer`,
:func:`pull_reply`): a worker answers a request that accepts ``drop`` with a
small descriptor and serves the serialized reply as a Drop; the caller pulls
it with the receiver-driven windows and retransmits.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Optional

from telesthete.protocol.drop import CHUNK_SIZE, DropFrameType, DropReceiver, DropSender
from telesthete.protocol.framing import ChannelType, pack_packet, unpack_packet
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


# ``send_native(channel_type, channel_id, plaintext, bulk=...)``: the transport
# method that seals and sends one native outer frame.
NativeSend = Callable[..., Awaitable[None]]


class _RelayShim:
    """Adapts telesthete Drop's synchronous ``transport.send(dest, packet)`` to
    the rook hub transport's async send. The dest is ignored (scoping is by
    drop_id). Pass-through packets go out via ``send_coro(packet)``, or
    ``send_coro(packet, route=route)`` (ADDRESSED frames) when the far end
    has a route; with ``native`` set they go out as native DROP frames.

    Pass-through packets are sent strictly one after another, in the order
    Drop produced them. Each is a whole band message, and the frame writer
    interleaves messages fragment by fragment (and puts single-fragment ones
    first), so packets queued together would complete out of order — e.g. a
    window's short 1-fragment final CHUNK before the 2-fragment CHUNKs ahead
    of it. Drop's per-peer sequence watermark then rejects every CHUNK that
    arrives after one with a higher sequence, and each window stalled until
    a re-request. Native frames need no such care: the transport assigns
    their (outer) sequence when it writes them."""

    def __init__(self, send_coro: Callable[..., Awaitable[None]], route: int = 0,
                 native: Optional[NativeSend] = None) -> None:
        self._send_coro = send_coro
        self.route = route
        self.native = native
        self._inflight: set[asyncio.Task] = set()
        self._ordered: collections.deque[bytes] = collections.deque()
        self._pump: Optional[asyncio.Task] = None
        self.sent = 0

    def send(self, dest: tuple, packet_bytes: bytes) -> None:
        self.sent += 1
        if self.native is None:
            self._ordered.append(packet_bytes)
            if self._pump is None or self._pump.done():
                self._pump = asyncio.ensure_future(self._drain())
            return
        task = asyncio.ensure_future(self._safe_send(packet_bytes))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _drain(self) -> None:
        while self._ordered:
            await self._safe_send(self._ordered.popleft())

    async def _safe_send(self, packet_bytes: bytes) -> None:
        try:
            if self.native is not None:
                pkt = unpack_packet(packet_bytes)
                plaintext = pkt.ciphertext[:-len(_FAKE_TAG)]
                # CHUNKs are the bulk of a transfer: paced and interleaved;
                # OFFER/REQUEST/DONE are small control frames.
                bulk = bool(plaintext) and plaintext[0] == DropFrameType.CHUNK
                await self.native(int(ChannelType.DROP), pkt.channel_id, plaintext, bulk=bulk)
            elif self.route:
                await self._send_coro(packet_bytes, route=self.route)
            else:
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


def drop_id_of(payload: bytes) -> int:
    """The drop_id (header channel_id) of a packet :func:`is_drop_packet` accepted."""
    return int.from_bytes(payload[17:19], "big")


class _Receiver(DropReceiver):
    """DropReceiver with a configurable re-request timeout.

    The library re-requests a window only after 1 s without progress, which
    is right for a long OTA but turns a reply that loses one fragment per
    64-chunk window into a ~1 s stall per window. Large replies use a shorter
    timeout (SPEC §8.2 leaves it to the implementation)."""

    def __init__(self, *a, rerequest: Optional[float] = None, **kw) -> None:
        super().__init__(*a, **kw)
        self.rerequest = rerequest

    def tick(self, now: Optional[float] = None) -> None:
        if self.rerequest is None:
            return super().tick(now)
        now = time.monotonic() if now is None else now
        if (self.offer is not None and self.verified is None
                and now - self._last_progress > self.rerequest):
            self._last_progress = now
            self.request_missing()


class OtaDropReceiver:
    """Worker-side: pulls one offered bundle over the band for a given drop_id.

    Constructed when a signed push handshake arrives (``worker.ota_begin``).
    Feed it inbound DROP packets via :meth:`feed`; it drives REQUEST/CHUNK/DONE
    and calls ``on_complete(data, sha_ok)`` once the file is whole and verified.
    """

    def __init__(self, band_id: bytes, drop_id: int,
                 send_coro: Callable[..., Awaitable[None]],
                 have: Optional[Dict[int, bytes]] = None, *,
                 route: int = 0, native: Optional[NativeSend] = None,
                 rerequest: Optional[float] = None, tick_secs: float = _TICK_SECS,
                 seq_source: Optional[SequenceSource] = None) -> None:
        self.band_id = band_id
        self.drop_id = drop_id
        self._shim = _RelayShim(send_coro, route=route, native=native)
        self._crypto = _PassthroughCrypto(band_id)
        self._recv = _Receiver(
            band_id, drop_id, have=have,
            crypto=self._crypto, transport=self._shim,
            # A retry against the same sender must keep counting up: the
            # sender's replay watermark would drop a fresh random start below it.
            seq_source=seq_source or SequenceSource(), rerequest=rerequest,
        )
        self._tick_secs = tick_secs
        self._tick_task: Optional[asyncio.Task] = None
        self._done = asyncio.Event()
        self.data: Optional[bytes] = None
        self._user_cb: Optional[Callable[[bytes, bool], None]] = None
        # Always wire our own completion so wait() works whether or not a caller
        # registered a callback.
        self._recv.on_complete(self._on_complete)

    def _on_complete(self, data: bytes, ok: bool) -> None:
        self.data = data if ok else None
        self._done.set()
        if self._user_cb is not None:
            self._user_cb(data, ok)

    def on_complete(self, cb: Callable[[bytes, bool], None]) -> None:
        self._user_cb = cb

    def accept_offer(self, name: str, size: int, sha256: str) -> None:
        """Start pulling a file whose OFFER we already know out of band (a
        large reply's descriptor): feed the receiver the OFFER it would have
        received, so it requests the first window right away. The sender
        needs no OFFER of its own — it serves whatever is REQUESTed.

        The synthetic packet carries sequence 0, below any real sender's
        CSPRNG-seeded start, so it never trips the replay watermark."""
        offer = {"name": name, "size": int(size), "chunk_size": CHUNK_SIZE,
                 "total_chunks": max(1, -(-int(size) // CHUNK_SIZE)), "sha256": sha256}
        frame = bytes([DropFrameType.OFFER]) + json.dumps(offer).encode("utf-8")
        self._recv.handle_packet(_BAND_PEER, pack_packet(
            self.band_id, ChannelType.DROP, self.drop_id, 0, frame + _FAKE_TAG))

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
                await asyncio.sleep(self._tick_secs)
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
                 send_coro: Callable[..., Awaitable[None]], *,
                 route: int = 0, native: Optional[NativeSend] = None) -> None:
        self.band_id = band_id
        self.drop_id = drop_id
        self._shim = _RelayShim(send_coro, route=route, native=native)
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

    @property
    def frames_sent(self) -> int:
        return self._shim.sent


# -- large replies over Drop -------------------------------------------------------

# Reply drops use the upper half of the 16-bit id space so they never collide
# with an operator-chosen OTA drop_id (0 by default).
REPLY_DROP_BASE = 0x8000
# How long a worker keeps serving a reply nobody finished pulling, and how
# many bytes of such replies it holds at most (oldest dropped first). 32 MiB
# is one maximum-size reply (ROOK_BAND_MAX_REPLY_BYTES): enough for the
# common case, and small enough for a Raspberry Pi or a phone to hold.
REPLY_DROP_TTL = 120.0
REPLY_DROP_MAX_BYTES = 32 * 1024 * 1024
# Caller side: re-request after this long without progress, checked this often.
REPLY_REREQUEST = 0.25
REPLY_TICK = 0.1


def valid_ota_drop_id(drop_id) -> bool:
    """An OTA push's drop_id must be below :data:`REPLY_DROP_BASE`: ids from
    0x8000 up belong to large replies, and a push on one would feed (and be
    fed by) a reply transfer of the same id."""
    return (isinstance(drop_id, int) and not isinstance(drop_id, bool)
            and 0 <= drop_id < REPLY_DROP_BASE)


def reply_drop_id(msg_id: str, worker_id: str = "") -> int:
    """The drop_id ``worker_id`` uses for its reply to request ``msg_id`` (when
    free). Both go in, so two workers answering one open call serve on
    different ids; a caller can predict it and avoid running two pulls on
    one id at once (see BandClient._new_id)."""
    h = hashlib.sha256(f"{msg_id}\n{worker_id}".encode("utf-8")).digest()
    return REPLY_DROP_BASE | (int.from_bytes(h[:2], "big") & 0x7FFF)


class DropReplyServer:
    """Worker side: serve serialized replies as Drops until pulled or expired."""

    def __init__(self, band_id: bytes, send_coro: Callable[..., Awaitable[None]],
                 ttl: float = REPLY_DROP_TTL, max_bytes: int = REPLY_DROP_MAX_BYTES,
                 clock: Callable[[], float] = time.monotonic, worker_id: str = "") -> None:
        self.band_id = band_id
        self.worker_id = worker_id
        self._send = send_coro
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._clock = clock
        # drop_id -> (sender, expires_at)
        self._serving: Dict[int, tuple[OtaDropSender, float]] = {}
        # Expiry timer: replies nobody pulls are released at their TTL even
        # if no further reply is ever served (pruning only in serve() kept
        # up to max_bytes alive on an idle worker indefinitely).
        self._timer: Optional[asyncio.TimerHandle] = None

    @property
    def active(self) -> int:
        return len(self._serving)

    def _held(self) -> int:
        return sum(len(s._sender.data) for s, _ in self._serving.values())

    def _prune(self) -> None:
        now = self._clock()
        for did in [d for d, (_, exp) in self._serving.items() if exp <= now]:
            self._serving.pop(did, None)

    def prune(self) -> None:
        """Release expired replies now (also runs on a timer, see serve)."""
        self._prune()
        self._arm()

    def _arm(self) -> None:
        """(Re)schedule the expiry timer for the earliest expiry, if any."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._serving:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # no loop (sync callers): serve() still prunes
        soonest = min(exp for _, exp in self._serving.values())
        # +50 ms so a timer that fires a hair early still finds it expired.
        self._timer = loop.call_later(max(soonest - self._clock(), 0.0) + 0.05, self.prune)

    def close(self) -> None:
        """Stop serving everything (worker shutdown)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._serving.clear()

    def serve(self, msg_id: str, blob: bytes, route: int = 0,
              native: Optional[NativeSend] = None) -> Optional[dict]:
        """Start serving ``blob`` for the reply to ``msg_id``; returns the
        descriptor for the reply envelope (``drop``), or ``None`` if it can't
        be held (then the caller gets the reply inline)."""
        self._prune()
        if len(blob) > self.max_bytes:
            return None
        while self._serving and self._held() + len(blob) > self.max_bytes:
            self._serving.pop(next(iter(self._serving)))  # oldest first
        did = reply_drop_id(msg_id, self.worker_id)
        while did in self._serving:   # another reply hashed here: next free id
            did = REPLY_DROP_BASE | ((did + 1) & 0x7FFF)
        sender = OtaDropSender(self.band_id, did, "reply", blob, self._send,
                               route=route, native=native)
        # Done once the caller verified it. A mismatch (e.g. chunks of another
        # transfer that shared the drop_id on a broadcast hub) keeps it served
        # so the caller can pull again until the TTL.
        sender.on_complete(lambda ok, _d=did: ok and self._serving.pop(_d, None))
        self._serving[did] = (sender, self._clock() + self.ttl)
        self._arm()
        return {"id": did, "size": len(blob), "sha256": sender.sha256}

    def feed(self, payload: bytes) -> bool:
        """Route an inbound Drop packet (REQUEST/DONE) to the reply it is for.
        True if consumed."""
        if not self._serving or not is_drop_packet(payload, self.band_id):
            return False
        entry = self._serving.get(drop_id_of(payload))
        if entry is None:
            return False
        entry[0].feed(payload)
        return True


def valid_descriptor(desc, max_size: int) -> Optional[tuple[int, int, str]]:
    """``(drop_id, size, sha256)`` from a reply's ``drop`` descriptor, or
    ``None`` if it is malformed or announces more than ``max_size`` bytes."""
    if not isinstance(desc, dict):
        return None
    did, size, sha = desc.get("id"), desc.get("size"), desc.get("sha256")
    if not (isinstance(did, int) and not isinstance(did, bool) and 0 <= did <= 0xFFFF):
        return None
    if not (isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= max_size):
        return None
    if not (isinstance(sha, str) and len(sha) == 64):
        return None
    try:
        int(sha, 16)
    except ValueError:
        return None
    return did, size, sha.lower()


async def pull_reply(band_id: bytes, desc: tuple[int, int, str],
                     send_coro: Callable[..., Awaitable[None]], *, route: int = 0,
                     timeout: float, register: Callable[[int, Optional[OtaDropReceiver]], None]
                     ) -> Optional[bytes]:
    """Caller side: pull one served reply. ``register(drop_id, rx)`` hooks the
    receiver into the caller's inbound Drop routing (and ``rx=None`` unhooks
    it). Returns the verified bytes, or ``None`` on timeout / hash mismatch."""
    did, size, sha = desc
    deadline = time.monotonic() + timeout
    seqs = SequenceSource()
    for _attempt in range(2):
        rx = OtaDropReceiver(band_id, did, send_coro, route=route,
                             rerequest=REPLY_REREQUEST, tick_secs=REPLY_TICK,
                             seq_source=seqs)
        register(did, rx)
        try:
            rx.start()
            rx.accept_offer("reply", size, sha)
            if await rx.wait(max(deadline - time.monotonic(), 0.0)):
                return rx.data
            if rx.verified is None:
                return None   # timed out
            # Assembled but the hash didn't match: pull once more from scratch.
            log.warning("reply drop %d failed its sha256 check; pulling again", did)
        finally:
            rx.stop()
            register(did, None)
    return None
