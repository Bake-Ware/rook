"""Band links: raw payloads on one band. :class:`Transport` (conn.py) puts
core v2 messages on top of a link; a link only moves bytes.

A link is the Telesthete profile of spec 2.1 for one band (one PSK):

- ``band`` / ``band_id``: the band id, hex and raw (SHA-256(psk)[0:16]).
- ``route``: the route id this link declared (0 = none), sent as ``rr``.
- ``connected``: whether a send can go out now.
- ``set_route(name)``: declare ``route_id_for(name)`` (core v1 14.1).
- ``start(on_payload, on_connect)``: ``on_payload(bytes)`` gets every
  reassembled payload (JSON messages and pass-through Drop packets, never
  keepalives); ``on_connect()`` runs every time the link is (re)established,
  the first time included. Raises when the link can't be set up at all; the
  caller retries with backoff.
- ``send(payload, route=0)``: one payload, broadcast (route 0) or ADDRESSED
  to a declared route id. Raises when the link is down.
- ``stop()``.

:class:`TelestheteLink` wraps the band transport today's workers run
(``rook.worker.transports.telesthete_hub``: UDP or WebSocket to the relay,
the frame codec, route declarations, keepalives, bulk pacing and the WS
reconnect backoff), so core v2 rides the exact wire v1 workers proved out.
TODO(castling:transport): when Castling retires ``rook.worker``, that module,
``rook.worker.wire`` and ``rook.worker.ota_drop`` move under here unchanged.

:class:`LoopbackRelay` is an in-memory relay for tests: same contract, no
sockets, routing like a routing relay (ADDRESSED to a declared route goes to
that peer only, else to everyone).
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from rook.worker.wire.codec import route_id_for

OnPayload = Callable[[bytes], Awaitable[None]]
OnConnect = Callable[[], Awaitable[None]]

DEFAULT_RELAY_PORT = 7474


def band_of(psk: str) -> str:
    """The band id of a PSK, as 32 hex (spec 5.1)."""
    return hashlib.sha256(psk.encode("utf-8")).digest()[:16].hex()


class Link(Protocol):
    band: str
    band_id: bytes
    route: int

    @property
    def connected(self) -> bool: ...
    def set_route(self, name: str) -> None: ...
    async def start(self, on_payload: OnPayload, on_connect: OnConnect) -> None: ...
    async def send(self, payload: bytes, route: int = 0) -> None: ...
    async def stop(self) -> None: ...


class TelestheteLink:
    """One band over a Telesthete relay: ``relay="host:port"`` (UDP) or
    ``ws_url`` (WebSocket, e.g. the hub's ``/band`` bridge)."""

    def __init__(self, psk: str, relay: str | None = None, ws_url: str | None = None):
        from rook.worker.transports.telesthete_hub import TelestheteHubTransport

        if ws_url:
            host, port = urlsplit(ws_url).hostname or "", DEFAULT_RELAY_PORT
        elif relay:
            host, _, p = relay.rpartition(":")
            if not host:
                host, p = relay, ""
            port = int(p) if p else DEFAULT_RELAY_PORT
        else:
            raise ValueError("a band link needs relay or ws_url")
        self.ws = bool(ws_url)
        self.t = TelestheteHubTransport(psk, host, port, ws_url=ws_url or None)
        self.band_id = self.t.band_id
        self.band = self.band_id.hex()

    @property
    def route(self) -> int:
        return self.t.route_id or 0

    @property
    def connected(self) -> bool:
        link = self.t.link
        return link is not None and link.connected

    def set_route(self, name: str) -> None:
        self.t.set_route(name)

    async def start(self, on_payload: OnPayload, on_connect: OnConnect) -> None:
        async def on_message(payload: bytes, _peer: tuple) -> None:
            await on_payload(payload)

        try:
            # WS calls on_connect on every (re)connect; UDP has no connection.
            await self.t.start(on_message, on_connect if self.ws else None)
        except BaseException:
            await self.t.stop()
            raise
        if not self.ws:
            await on_connect()

    async def send(self, payload: bytes, route: int = 0) -> None:
        await self.t.send(payload, route=route)

    async def stop(self) -> None:
        await self.t.stop()


class LoopbackRelay:
    """An in-memory relay: ``relay.link(psk)`` gives a :class:`LoopbackLink`
    on that band. Delivery is asynchronous (one task per payload), like a
    network."""

    def __init__(self):
        self.links: list[LoopbackLink] = []
        self.sent: list[tuple[str, bytes, int]] = []    # (band, payload, route), for tests
        self._tasks: set[asyncio.Task] = set()

    def link(self, psk: str) -> "LoopbackLink":
        link = LoopbackLink(self, psk)
        self.links.append(link)
        return link

    def _forward(self, src: "LoopbackLink", payload: bytes, route: int) -> None:
        self.sent.append((src.band, payload, route))
        peers = [x for x in self.links if x is not src and x.band == src.band and x.connected]
        if route:
            declared = [x for x in peers if x.route == route]
            peers = declared or peers
        for x in peers:
            task = asyncio.get_running_loop().create_task(x._receive(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


class LoopbackLink:
    def __init__(self, relay: LoopbackRelay, psk: str):
        self.relay = relay
        self.band = band_of(psk)
        self.band_id = bytes.fromhex(self.band)
        self.route = 0
        self.fail_starts = 0            # tests: how many start() calls raise first
        self._up = False
        self._on_payload: OnPayload | None = None
        self._on_connect: OnConnect | None = None

    @property
    def connected(self) -> bool:
        return self._up

    def set_route(self, name: str) -> None:
        self.route = route_id_for(name)

    async def start(self, on_payload: OnPayload, on_connect: OnConnect) -> None:
        if self.fail_starts > 0:
            self.fail_starts -= 1
            raise OSError("loopback: relay unreachable")
        self._on_payload, self._on_connect = on_payload, on_connect
        self._up = True
        await on_connect()

    async def send(self, payload: bytes, route: int = 0) -> None:
        if not self._up:
            raise RuntimeError("link down")
        self.relay._forward(self, bytes(payload), route)

    async def stop(self) -> None:
        self._up = False

    # tests: lose and regain the relay
    def cut(self) -> None:
        self._up = False

    async def heal(self) -> None:
        self._up = True
        if self._on_connect is not None:
            await self._on_connect()

    async def _receive(self, payload: bytes) -> None:
        if self._up and self._on_payload is not None:
            await self._on_payload(payload)
