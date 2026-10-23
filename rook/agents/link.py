"""The link to the hub (design 3.3): one websocket, ``/home/agents-link``.

``hello`` on every connect; events go out in order, and while the hub is
unreachable they wait in a bounded buffer (oldest dropped first) and are sent
on reconnect. Requests from the hub that carry ``req`` are answered with a
``reply``. Reconnects back off from 1 s, doubling, to 30 s.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("rook.agents.link")

MAX_MSG = 32 * 1024 * 1024


class OpError(Exception):
    """A request the host refuses (sent back as ``reply`` ok false)."""


class Link:
    def __init__(self, url: str, token: str, hello: Callable[[], dict],
                 handler: Callable[[dict], Awaitable[Any]], *,
                 backoff: tuple[float, float] = (1.0, 30.0), buffer: int = 1000) -> None:
        self.url = url
        self._token = token
        self.hello = hello
        self.handler = handler
        self.backoff = backoff
        self.buf: collections.deque = collections.deque(maxlen=buffer)
        self.dropped = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self.connected = asyncio.Event()
        self.connects = 0
        self._tasks: set[asyncio.Task] = set()

    def send(self, obj: dict, *, buffer: bool = True) -> None:
        """Queue ``obj`` for the hub. Unbuffered messages (replies) are dropped
        while the link is down."""
        if not buffer and self._ws is None:
            return
        if len(self.buf) == self.buf.maxlen:
            self.dropped += 1
        self.buf.append(obj)
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        for t in list(self._tasks):
            t.cancel()

    async def run(self) -> None:
        lo, hi = self.backoff
        delay = lo
        async with aiohttp.ClientSession() as session:
            while not self._closed:
                try:
                    async with session.ws_connect(
                            self.url, headers={"Authorization": f"Bearer {self._token}"},
                            heartbeat=30, max_msg_size=MAX_MSG) as ws:
                        delay = lo
                        await self._session(ws)
                except asyncio.CancelledError:
                    raise
                except aiohttp.WSServerHandshakeError as e:
                    log.warning("agents link refused: HTTP %s", e.status)
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    log.info("agents link down: %s", type(e).__name__)
                except Exception:  # noqa: BLE001 - the link must keep trying
                    log.exception("agents link failed")
                if self._closed:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, hi)

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_str(json.dumps(self.hello()))
        self._ws = ws
        self.connects += 1
        self.connected.set()
        if self.dropped:
            log.warning("agents link: %d events were dropped while it was down", self.dropped)
            self.dropped = 0
        writer = asyncio.create_task(self._writer(ws))
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        obj = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        t = asyncio.create_task(self._dispatch(obj))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.CLOSED):
                    break
                if writer.done():
                    break
        finally:
            self._ws = None
            self.connected.clear()
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer

    async def _writer(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            while self.buf:
                obj = self.buf[0]
                await ws.send_str(json.dumps(obj))
                if self.buf and self.buf[0] is obj:
                    self.buf.popleft()
            self._wake.clear()
            await self._wake.wait()
            if self._closed:
                return

    async def _dispatch(self, obj: dict) -> None:
        req = obj.get("req")
        try:
            result = await self.handler(obj)
            reply = {"t": "reply", "req": req, "ok": True, "result": result}
        except OpError as e:
            reply = {"t": "reply", "req": req, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - every request gets an answer
            log.exception("agents link: %s failed", obj.get("t"))
            reply = {"t": "reply", "req": req, "ok": False,
                     "error": f"internal error: {type(e).__name__}"}
        if req is not None:
            self.send(reply, buffer=False)
