"""Telesthete-hub transport — hub-routed, band-id addressable.

Every outbound frame goes to the configured hub, which forwards it to all
other peers in the same band (matched by the 16-byte band_id). The worker
registers implicitly with its first frame and stays registered while it
keeps sending (the relay evicts idle UDP peers after its TTL).

Layers (bottom up):

* a **link** moves raw frames: :class:`UdpLink` (datagrams straight to the
  relay, LAN) or :class:`WsLink` (one frame per binary WebSocket message,
  through the hub's ``/band`` bridge, e.g. behind Cloudflare);
* :class:`rook.worker.wire.codec.BandCodec` is the shared frame pipeline —
  crypto, sequence source, §6.6 fragmenter/reassembler, ingest filtering;
* :class:`rook.worker.transports.writer.FrameWriter` is the single writer
  task per transport: control (single-fragment) messages first, bulk
  (multi-fragment) messages interleaved fragment-by-fragment, optional
  pacing of bulk on UDP;
* :class:`rook.worker.transports.dispatch.Dispatcher` runs inbound handlers
  with bounded concurrency so a slow capability never stalls the receiver.

:class:`TelestheteHubTransport` is the public facade (``TRANSPORT``) with the
:mod:`rook.worker.transports.base` contract.

**Addressed routing** (telesthete SPEC §10.1). Once :meth:`set_route` names
this peer (the worker sets its ``worker_id``, a band client its node id), the
transport declares ``route_id_for(name)`` with a ROUTE frame at start, after
every WS (re)connect, and once per keepalive interval while up. A routing hub
then delivers ADDRESSED (0x06) frames whose ``channel_id`` is our route id to
us only (``send(payload, route=...)`` sends them; CHANNEL frames are always
broadcast); an old hub relays the 43-byte declaration to everyone, where it
fails the empty-AAD tag check (old receivers) or is dropped by type (this
codec), and broadcasts ADDRESSED frames, which every rook receiver opens.

The declaration is soft state on the hub's peer entry. The hub forgets it
when it evicts us (``HUB_PEER_TTL_SECS``, 15 s by default in telesthitium,
60 s in the shipped deployment) or restarts; the next frame we send
re-registers us WITHOUT a route until the next declaration. So we re-declare
every ``keepalive_secs`` (20 s) whether or not other traffic flowed — the
idle-keepalive skip never suppresses it — and a declaration, being a frame,
also refreshes our registration, so on a routing transport it doubles as the
keepalive. With a 15 s TTL an idle peer is still evicted for up to 5 s per
cycle (as before); keep the TTL at 60 s.

This intentionally bypasses the LAN-broadcast discovery in
``telesthete.transport.udp.UDPTransport`` — discovery is the hub's job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Awaitable, Callable, Optional

from ...core.backoff import Backoff
from ..wire import HEADER_SIZE as FRAG_HEADER
from ..wire.codec import KEEPALIVE_PAYLOAD, BandCodec, route_id_for
from .base import OnConnect, OnMessage
from .dispatch import DEFAULT_BACKLOG, DEFAULT_CONCURRENCY, Dispatcher
from .writer import FrameWriter, TokenBucket

log = logging.getLogger("rook.worker.transports.telesthete-hub")

# Kept for importers of the old module-level name.
_KEEPALIVE_PAYLOAD = KEEPALIVE_PAYLOAD

# Bulk pacing on UDP. The relay forwards each datagram into one egress queue
# shared by every UDP peer (1024 slots in telesthitium) and drops on overflow,
# so an unpaced 1 MB reply (~1,050 frames) fanned out to a few peers overflows
# it. 2,000 frames/s (~2 MB/s) with a 64-frame burst keeps small multi-fragment
# messages instant and big ones well under the queue. Rate 0 disables pacing.
DEFAULT_UDP_PACE_RATE = 2000.0
DEFAULT_UDP_PACE_BURST = 64
# Best-effort socket buffers: absorb bursts on receive, and let the kernel
# queue a paced burst on send. The OS may clamp these (net.core.*mem_max).
UDP_SOCKET_BUFFER = 4 * 1024 * 1024

# A WS session that lasted at least this long resets the reconnect backoff;
# one that dies sooner keeps backing off (a hub that accepts then drops).
WS_STABLE_SECS = 10.0

# Receive loops yield to the event loop after this many frames in a row.
# sock_recvfrom / ws.receive return without suspending while data is
# buffered, so without it a backlog (e.g. 3,000 queued replies) is all
# ingested before a single handler runs — and the dispatcher, seeing every
# slot taken, used to drop most of it.
RECV_YIELD_EVERY = 32


def _env_num(name: str, default: float, cast=float, minimum: float = 0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("ignoring %s=%r (not a number)", name, raw)
        return default
    if value < minimum:
        log.warning("ignoring %s=%r (below %s)", name, raw, minimum)
        return default
    return value


def udp_pacer_from_env() -> Optional[TokenBucket]:
    """The UDP bulk pacer from ``ROOK_BAND_UDP_PACE_RATE`` (frames/s, 0 = off)
    and ``ROOK_BAND_UDP_PACE_BURST`` (frames)."""
    rate = _env_num("ROOK_BAND_UDP_PACE_RATE", DEFAULT_UDP_PACE_RATE)
    burst = int(_env_num("ROOK_BAND_UDP_PACE_BURST", DEFAULT_UDP_PACE_BURST, int, 1))
    if rate <= 0:
        return None
    return TokenBucket(rate, burst)


def set_socket_buffers(sock: socket.socket, size: int = UDP_SOCKET_BUFFER) -> None:
    """Best-effort SO_SNDBUF/SO_RCVBUF (also used by the hub's WS bridge)."""
    _set_buffers(sock, size)


def _set_buffers(sock: socket.socket, size: int = UDP_SOCKET_BUFFER) -> None:
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, size)
        except OSError as e:
            log.debug("setsockopt(%s, %d) failed: %s", opt, size, e)


OnFrame = Callable[[bytes], None]
# Awaited by a receive loop between frames when it returns an awaitable:
# the dispatcher's backpressure (see Dispatcher.room).
Hold = Callable[[], Optional[Awaitable[None]]]


async def _pace(count: int, hold: Optional[Hold]) -> None:
    """Between two received frames: wait for dispatch room if the
    dispatcher is saturated, else yield every RECV_YIELD_EVERY frames."""
    waiter = hold() if hold is not None else None
    if waiter is not None:
        await waiter
    elif count % RECV_YIELD_EVERY == 0:
        await asyncio.sleep(0)


class UdpLink:
    """Raw frames over one UDP socket to the relay."""

    KIND = "udp"

    def __init__(self, hub: tuple[str, int], bind_port: int = 0) -> None:
        self.hub = hub
        self.bind_port = bind_port
        self.sock: Optional[socket.socket] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _set_buffers(sock)
            sock.bind(("0.0.0.0", self.bind_port))
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.sock = sock

    def start_recv(self, on_frame: OnFrame, hold: Optional[Hold] = None) -> None:
        self._task = asyncio.get_running_loop().create_task(self._recv_loop(on_frame, hold))

    async def write(self, frame: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("transport not started")
        await asyncio.get_running_loop().sock_sendto(self.sock, frame, self.hub)

    async def _recv_loop(self, on_frame: OnFrame, hold: Optional[Hold] = None) -> None:
        assert self.sock is not None
        loop = asyncio.get_running_loop()
        count = 0
        while not self._stopping:
            try:
                data, _src = await loop.sock_recvfrom(self.sock, 65535)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("recv error: %s", e)
                await asyncio.sleep(0.1)
                continue
            on_frame(data)
            count += 1
            try:
                await _pace(count, hold)
            except asyncio.CancelledError:
                break

    async def close(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None


class WsLink:
    """Raw frames over a (re)connecting WebSocket to the hub's ``/band``.

    :meth:`run` owns the connection lifecycle: connect, call ``on_up`` (which
    registers + re-announces), serve frames until the link drops, call
    ``on_down``, back off (jittered, :class:`rook.core.backoff.Backoff`),
    reconnect. This is what makes a dropped band link self-heal instead of
    leaving the worker silently off the band until restart.
    """

    KIND = "ws"

    def __init__(self, url: str, backoff: Optional[Backoff] = None) -> None:
        self.url = url
        self.conn = None       # aiohttp.ClientWebSocketResponse while up
        self._session = None
        self._stopping = False
        self.backoff = backoff or Backoff(base=1.0, cap=30.0)

    @property
    def connected(self) -> bool:
        return self.conn is not None

    async def write(self, frame: bytes) -> None:
        ws = self.conn
        if ws is None:
            raise RuntimeError("WS not connected")
        await ws.send_bytes(frame)

    async def _connect(self):
        import aiohttp
        # total=None: never impose a lifetime cap on the long-lived websocket
        # (aiohttp's default ClientTimeout would force-close it after 5
        # minutes). sock_connect bounds only the initial TCP connect so an
        # unreachable hub fails fast into the retry instead of hanging.
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=20.0))
        # heartbeat: aiohttp sends WS pings and tears the connection down if
        # pongs stop — dead-peer detection, and keeps the tunnel from idling.
        return await self._session.ws_connect(self.url, heartbeat=30.0, autoping=True)

    async def run(self, on_frame: OnFrame,
                  on_up: Callable[[], Awaitable[None]],
                  on_down: Callable[[], None],
                  hold: Optional[Hold] = None) -> None:
        while not self._stopping:
            began = None
            try:
                ws = await self._connect()
                self.conn = ws
                began = time.monotonic()
                log.info("WS connected: %s", self.url)
                try:
                    await on_up()
                except Exception:
                    log.exception("post-connect (re)announce failed")
                await self._recv_loop(ws, on_frame, hold)  # returns when the link drops
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("WS connect failed: %s", e)
            finally:
                self.conn = None
                on_down()
                await self._close_session()
            if self._stopping:
                break
            if began is not None and time.monotonic() - began >= WS_STABLE_SECS:
                self.backoff.reset()
            delay = self.backoff.next()
            log.info("WS reconnecting in %.1fs", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _recv_loop(self, ws, on_frame: OnFrame, hold: Optional[Hold] = None) -> None:
        """Feed binary frames to ``on_frame``. Returns (rather than looping)
        as soon as the connection closes or errors, so :meth:`run` can
        reconnect. ``on_frame`` never awaits handlers (see Dispatcher), so a
        slow capability can't stall this loop — that stall used to stop pong
        replies and get the connection killed. ``hold`` only waits (bounded)
        while the dispatcher drains a burst."""
        import aiohttp
        count = 0
        while not self._stopping:
            try:
                msg = await ws.receive()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS recv error: %s", e)
                return
            if msg.type == aiohttp.WSMsgType.BINARY:
                on_frame(msg.data)
                count += 1
                await _pace(count, hold)
            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.ERROR):
                log.info("WS closed by peer (type=%s)", msg.type.name)
                return

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    async def close(self) -> None:
        self._stopping = True
        ws, self.conn = self.conn, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        await self._close_session()


class TelestheteHubTransport:
    NAME = "telesthete-hub"

    def __init__(
        self,
        psk: str,
        hub_host: str,
        hub_port: int = 7474,
        keepalive_secs: float = 20.0,
        bind_port: int = 0,
        use_ws: bool = False,
        ws_url: str | None = None,
    ) -> None:
        self.codec = BandCodec(psk)
        self.band_id = self.codec.band_id
        self._crypto = self.codec.crypto        # kept for existing callers/tests
        self._seq_source = self.codec.seq
        self._hub = (hub_host, hub_port)
        self._keepalive = keepalive_secs
        self._bind_port = bind_port
        self._use_ws = use_ws
        # Port 443 (or 8443) is TLS at the edge — must use wss://, not ws://.
        _scheme = "wss" if hub_port in (443, 8443) else "ws"
        # ``ws_url`` (worker --ws-url / ROOK_HUB_WS_URL) points at any WS
        # endpoint instead of the derived bridge URL — e.g. telesthitium's
        # native listener (HUB_WS_BIND, path HUB_WS_PATH) — and implies WS.
        self._ws_url: str = ws_url or f"{_scheme}://{hub_host}:{hub_port}/band"
        if ws_url:
            self._use_ws = True
        # Called with each inbound request the dispatcher sheds under a
        # burst (Worker sets it to answer targeted ones "busy"). Read at start.
        self.on_shed = None
        # Addressed routing: our declared route id (None = never declare).
        self._route_id: Optional[int] = None
        self._route_name: Optional[str] = None
        self._last_declared = float("-inf")
        self.declarations_sent = 0

        self.link: UdpLink | WsLink | None = None
        self.writer: Optional[FrameWriter] = None
        self.dispatcher: Optional[Dispatcher] = None
        self._on_message: Optional[OnMessage] = None
        self._on_connect: Optional[OnConnect] = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    @property
    def hub(self) -> tuple[str, int]:
        return self._hub

    # -- addressed routing ---------------------------------------------------

    @property
    def route_id(self) -> Optional[int]:
        return self._route_id

    def set_route(self, name: Optional[str]) -> Optional[int]:
        """Declare ``route_id_for(name)`` from now on (``None`` stops
        declaring). Re-declares at once when the link is up. Returns the id."""
        self._route_name = name
        self._route_id = route_id_for(name) if name else None
        self._last_declared = float("-inf")
        if self._route_id is not None and self._link_up():
            try:
                asyncio.get_running_loop().create_task(self._declare_quietly())
            except RuntimeError:
                pass  # no loop yet: start() declares
        return self._route_id

    def _link_up(self) -> bool:
        return (self.link is not None and self.link.connected
                and self.writer is not None and not self._stopping)

    async def declare_route(self) -> bool:
        """Send our ROUTE declaration now. False when there is nothing to
        declare. Raises like :meth:`send` when the link is down."""
        rid = self._route_id
        if rid is None:
            return False
        self._check_link()
        self._last_declared = time.monotonic()
        await self.writer.submit_frame(b"", lambda _pt, _rid=rid: self.codec.route_frame(_rid))
        self.declarations_sent += 1
        return True

    async def _declare_quietly(self) -> None:
        try:
            await self.declare_route()
        except Exception as e:
            log.debug("route declaration skipped: %s", e)

    # -- lifecycle -----------------------------------------------------------

    async def start(self, on_message: OnMessage,
                    on_connect: OnConnect | None = None) -> None:
        self._on_message = on_message
        self._on_connect = on_connect
        self._stopping = False
        loop = asyncio.get_running_loop()
        self.dispatcher = Dispatcher(
            on_message,
            concurrency=int(_env_num("ROOK_BAND_DISPATCH_CONCURRENCY",
                                     DEFAULT_CONCURRENCY, int, 1)),
            backlog=int(_env_num("ROOK_BAND_DISPATCH_BACKLOG", DEFAULT_BACKLOG, int, 0)),
            on_shed=self.on_shed)

        if self._use_ws:
            link = WsLink(self._ws_url)
            self.link = link
            # WS is TCP: the stream already applies backpressure, no pacing.
            self.writer = FrameWriter(self.codec.split, self.codec.seal, link.write)
            self.writer.start()
            log.info("telesthete-hub transport up (WS): hub=%s:%d band_id=%s",
                     self._hub[0], self._hub[1], self.band_id.hex()[:16])
            # Registration happens in _ws_up (we can't send before connecting).
            self._tasks.append(loop.create_task(
                link.run(self._on_frame, self._ws_up, self._ws_down, self._hold)))
        else:
            link = UdpLink(self._hub, self._bind_port)
            link.open()
            self.link = link
            self.writer = FrameWriter(self.codec.split, self.codec.seal, link.write,
                                      pacer=udp_pacer_from_env())
            self.writer.start()
            log.info("telesthete-hub transport up: hub=%s:%d band_id=%s local=%s "
                     "frag_overhead=%dB", self._hub[0], self._hub[1],
                     self.band_id.hex()[:16], link.sock.getsockname(), FRAG_HEADER)
            # Implicit registration: the hub learns our (NAT'd) address from
            # this frame before any real traffic. The declaration right after
            # is also our second frame, which a validating hub
            # (HUB_UDP_VALIDATION_PACKETS=2) needs before it delivers to us.
            await self.send(KEEPALIVE_PAYLOAD)
            await self.declare_route()
            link.start_recv(self._on_frame, self._hold)

        self._tasks.append(loop.create_task(self._keepalive_loop()))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.dispatcher is not None:
            await self.dispatcher.stop()
        if self.writer is not None:
            await self.writer.stop()
        if self.link is not None:
            await self.link.close()
        log.info("telesthete-hub transport down")

    # -- send/recv -----------------------------------------------------------

    async def send(self, payload: bytes, peer_id: tuple | None = None, *,
                   route: int = 0) -> None:
        """Fragment + encrypt + send ``payload``; returns once every frame of
        it is on the link. Big payloads are split across several frames
        (SPEC §6.4/§6.6) and interleaved with other traffic by the writer
        (see :mod:`.writer`). ``peer_id`` is ignored. ``route`` 0 sends
        CHANNEL frames the hub broadcasts to the band; a nonzero ``route`` (a
        peer's declared route id, core spec 14.1) sends every fragment as an
        ADDRESSED frame a routing hub delivers to that peer only."""
        self._check_link()
        await self.writer.submit(payload, route)

    async def send_native(self, channel_type: int, channel_id: int, plaintext: bytes,
                          *, bulk: bool = False) -> None:
        """Send ``plaintext`` as ONE unfragmented frame of ``channel_type``
        sealed with the spec AAD (native DROP frames, see
        :mod:`rook.worker.ota_drop`). Sealed at emission time by the writer,
        so its sequence comes from the same source as every other frame."""
        self._check_link()
        await self.writer.submit_frame(
            plaintext,
            lambda pt: self.codec.seal_spec(pt, channel_type, channel_id),
            bulk=bulk)

    def _check_link(self) -> None:
        if self._use_ws:
            if self.link is None or not self.link.connected:
                # Mid-reconnect (or not up yet). Caller loops catch + log;
                # the next announce after reconnect re-registers us.
                raise RuntimeError("WS not connected")
        elif self.link is None or not self.link.connected:
            raise RuntimeError("transport not started")

    def _hold(self) -> Optional[Awaitable[None]]:
        """Backpressure for the receive loops: an awaitable while the
        dispatcher has too many handlers in flight, else None."""
        d = self.dispatcher
        return d.room() if d is not None and d.saturated else None

    def _on_frame(self, data: bytes) -> None:
        msg = self.codec.ingest(data)
        if msg is not None and self.dispatcher is not None:
            self.dispatcher.submit(msg.payload, (msg.channel_id,))

    async def _ws_up(self) -> None:
        # Implicit registration (the hub learns us), our route (a fresh hub
        # connection has none), then let the worker re-announce its
        # capabilities onto the band.
        await self.send(KEEPALIVE_PAYLOAD)
        await self._declare_quietly()   # never block the re-announce below
        if self._on_connect is not None:
            await self._on_connect()

    def _ws_down(self) -> None:
        # Anything queued for the dead connection fails like a send on a
        # disconnected link always did; a half-sent message is abandoned.
        if self.writer is not None:
            self.writer.fail_all(RuntimeError("WS not connected"))

    async def _keepalive_loop(self) -> None:
        """Keep our relay registration (and route) alive.

        Any frame refreshes the relay's view of us, so a keepalive is only
        sent when nothing else went out for a whole interval. This applies on
        WS too: telesthitium's native WS endpoint doesn't TTL-prune
        connections, but the hub's ``/band`` bridge (rook-mcp) relays each
        WebSocket through its own UDP socket, which the relay does prune.

        The route declaration is NOT skipped on a busy link: busy traffic
        keeps us registered but cannot restore a route the hub lost when it
        restarted, so it goes out every interval regardless (see the module
        docstring). Being a frame itself, it also counts as link activity."""
        while not self._stopping:
            try:
                now = time.monotonic()
                if self._route_id is not None and now - self._last_declared >= self._keepalive:
                    await self.declare_route()
                    continue
                writer = self.writer
                # A declaration counts as activity from when it was queued.
                last = max(writer.last_tx if writer else 0.0, self._last_declared)
                idle_for = now - last
                if idle_for < self._keepalive:
                    wait = self._keepalive - idle_for
                    if self._route_id is not None:
                        wait = min(wait, self._keepalive - (now - self._last_declared))
                    await asyncio.sleep(max(wait, 0.01))
                    continue
                if not self._stopping:
                    await self.send(KEEPALIVE_PAYLOAD)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Benign while the WS link is reconnecting; it will recover.
                log.debug("keepalive skipped: %s", e)
                try:
                    await asyncio.sleep(self._keepalive)
                except asyncio.CancelledError:
                    break

TRANSPORT = TelestheteHubTransport
