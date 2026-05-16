"""Transport plugin contract."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol


OnMessage = Callable[[bytes, tuple], Awaitable[None]]
"""``async def on_message(payload, peer_id) -> None``. `peer_id` is opaque to
the worker; transports pick whatever they want to use as a peer identity
(``(host, port)``, a band peer hash, a session id …)."""


class Transport(Protocol):
    NAME: str

    async def start(self, on_message: OnMessage) -> None: ...
    async def send(self, payload: bytes, peer_id: tuple | None = None) -> None: ...
    async def stop(self) -> None: ...
