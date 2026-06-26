"""Transport plugin contract."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol


OnMessage = Callable[[bytes, tuple], Awaitable[None]]
"""``async def on_message(payload, peer_id) -> None``. `peer_id` is opaque to
the worker; transports pick whatever they want to use as a peer identity
(``(host, port)``, a band peer hash, a session id …)."""


OnConnect = Callable[[], Awaitable[None]]
"""``async def on_connect() -> None``. Invoked by the transport every time the
underlying link is (re)established, so the worker can (re)announce itself. May
be called more than once over a transport's lifetime (e.g. after a WS
reconnect). Optional — transports without a reconnect notion never call it."""


class Transport(Protocol):
    NAME: str

    async def start(self, on_message: OnMessage,
                    on_connect: OnConnect | None = None) -> None: ...
    async def send(self, payload: bytes, peer_id: tuple | None = None) -> None: ...
    async def stop(self) -> None: ...
