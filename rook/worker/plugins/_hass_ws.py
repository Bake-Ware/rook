"""A small client for the Home Assistant WebSocket API.

``ws(s)://<ha>/api/websocket``: the server says ``auth_required``, the client
answers with a long-lived access token, then it is JSON commands by ``id``
(``get_states``, ``config/device_registry/list``, ``call_service``…) and
event subscriptions that keep answering on their command's ``id``.
https://developers.home-assistant.io/docs/api/websocket
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import ssl
from typing import Any, Callable

from ...core.aiocompat import timeout as aio_timeout

log = logging.getLogger("rook.worker.homeassistant")


class HAError(Exception):
    """Home Assistant refused or could not complete a command."""


class AuthFailed(HAError):
    """The access token was refused."""


def ws_url(url: str) -> str:
    """``https://ha.local:8123`` -> ``wss://ha.local:8123/api/websocket``."""
    base = url.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base.startswith(("ws://", "wss://")):
        base = "ws://" + base
    return base if base.endswith("/api/websocket") else base + "/api/websocket"


class HAClient:
    """One authenticated connection to one Home Assistant."""

    def __init__(self, url: str, token: str, *, insecure: bool = False,
                 connect_timeout: float = 8.0, request_timeout: float = 15.0) -> None:
        self.url = ws_url(url)
        self._token = token
        self.insecure = insecure
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.version: str | None = None
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, Callable[[dict], Any]] = {}
        self.closed = asyncio.Event()
        self.closed.set()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self.closed.is_set()

    async def connect(self) -> None:
        import websockets   # a Rook dependency; homeassistant stays off without it
        kwargs: dict[str, Any] = {"open_timeout": self.connect_timeout, "ping_interval": 30,
                                  "max_size": 32 * 1024 * 1024}
        if self.url.startswith("wss://"):
            ctx = ssl.create_default_context()
            if self.insecure:   # a self-signed certificate, as HA OS makes by default
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        self._ws = await websockets.connect(self.url, **kwargs)
        self.closed.clear()
        try:
            async with aio_timeout(self.connect_timeout):
                hello = json.loads(await self._ws.recv())
                if hello.get("type") == "auth_required":
                    await self._ws.send(json.dumps({"type": "auth",
                                                    "access_token": self._token}))
                    hello = json.loads(await self._ws.recv())
            if hello.get("type") == "auth_invalid":
                raise AuthFailed(str(hello.get("message") or "access token refused"))
            if hello.get("type") != "auth_ok":
                raise HAError(f"unexpected greeting {hello.get('type')!r}")
            self.version = hello.get("ha_version")
        except BaseException:
            await self.close()
            raise
        self._reader = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                # HA may batch several messages into one JSON array.
                for m in msg if isinstance(msg, list) else [msg]:
                    self._dispatch(m)
        except Exception as e:
            log.debug("homeassistant %s: connection ended: %s", self.url, e)
        finally:
            self._fail_pending(HAError("connection to Home Assistant closed"))
            self.closed.set()

    def _dispatch(self, m: dict) -> None:
        mid = m.get("id")
        if m.get("type") == "event":
            cb = self._subs.get(mid)
            if cb is not None:
                try:
                    cb(m.get("event") or {})
                except Exception:
                    log.debug("homeassistant event callback raised", exc_info=True)
            return
        fut = self._pending.pop(mid, None)
        if fut is not None and not fut.done():
            fut.set_result(m)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def command(self, kind: str, **fields: Any) -> Any:
        """One command; its ``result``, or :class:`HAError`."""
        if not self.connected:
            raise HAError("not connected to Home Assistant")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "type": kind, **fields}))
        try:
            async with aio_timeout(self.request_timeout):
                msg = await fut
        finally:
            self._pending.pop(mid, None)
        if not msg.get("success", True):
            err = msg.get("error") or {}
            raise HAError(str(err.get("message") or err.get("code") or "command failed"))
        return msg.get("result")

    async def subscribe(self, event_type: str, callback: Callable[[dict], Any]) -> None:
        """Receive every ``event_type`` event from now on."""
        if not self.connected:
            raise HAError("not connected to Home Assistant")
        mid = next(self._ids)
        self._subs[mid] = callback
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "type": "subscribe_events",
                                        "event_type": event_type}))
        async with aio_timeout(self.request_timeout):
            msg = await fut
        if not msg.get("success", True):
            self._subs.pop(mid, None)
            raise HAError(f"subscribing to {event_type} failed")

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
        self._reader = None
        self._subs.clear()
        self._fail_pending(HAError("connection to Home Assistant closed"))
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self.closed.set()
