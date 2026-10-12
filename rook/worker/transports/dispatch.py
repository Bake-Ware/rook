"""Bounded inbound dispatch for band transports.

Every reassembled message runs its handler in its own task so a slow
capability never stalls the receive loop (a stalled WS reader stops answering
pings and gets the connection killed). That used to be unbounded: a burst of
inbound requests spawned a task each. Now at most ``concurrency`` handlers run
at once; further messages wait in a FIFO backlog of ``backlog`` entries, and
when that is full new messages are dropped with a rate-limited warning. The
receive loop itself never waits on handlers.

Note: a handler that awaits a band reply (``Worker.request``) holds a slot
while it waits, and that reply is itself dispatched through here. With every
slot held by such handlers, replies queue in the backlog until the handlers
time out — degraded, not deadlocked. The default bound (64) is far above any
real fan-in.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("rook.worker.transports.dispatch")

DEFAULT_CONCURRENCY = 64
DEFAULT_BACKLOG = 1024
WARN_EVERY = 10.0   # seconds between "backlog full" warnings

Handler = Callable[[bytes, tuple], Awaitable[None]]


class Dispatcher:
    def __init__(self, handler: Optional[Handler],
                 concurrency: int = DEFAULT_CONCURRENCY,
                 backlog: int = DEFAULT_BACKLOG,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.backlog_limit = max(0, int(backlog))
        self._backlog: collections.deque[tuple[bytes, tuple]] = collections.deque()
        self._running: set[asyncio.Task] = set()
        self._clock = clock
        self._closed = False
        self.dropped = 0
        self._dropped_since_warn = 0
        self._last_warn = float("-inf")

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def queued(self) -> int:
        return len(self._backlog)

    def submit(self, payload: bytes, peer: tuple) -> bool:
        """Hand one message to a handler. Never blocks. Returns False if it
        was dropped (backlog full, or stopped)."""
        if self.handler is None or self._closed:
            return False
        if len(self._running) < self.concurrency:
            self._spawn(payload, peer)
            return True
        if len(self._backlog) < self.backlog_limit:
            self._backlog.append((payload, peer))
            return True
        self.dropped += 1
        self._dropped_since_warn += 1
        now = self._clock()
        if now - self._last_warn >= WARN_EVERY:
            log.warning("band dispatch backlog full (%d running, %d queued); "
                        "dropped %d inbound message(s)", len(self._running),
                        len(self._backlog), self._dropped_since_warn)
            self._last_warn = now
            self._dropped_since_warn = 0
        return False

    def _spawn(self, payload: bytes, peer: tuple) -> None:
        task = asyncio.get_running_loop().create_task(self._run(payload, peer))
        self._running.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._running.discard(task)
        if self._closed:
            return
        while self._backlog and len(self._running) < self.concurrency:
            self._spawn(*self._backlog.popleft())

    async def _run(self, payload: bytes, peer: tuple) -> None:
        try:
            await self.handler(payload, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_message handler raised")

    async def stop(self) -> None:
        self._closed = True
        self._backlog.clear()
        tasks = list(self._running)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._running.clear()
