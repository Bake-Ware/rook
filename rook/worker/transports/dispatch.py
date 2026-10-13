"""Bounded inbound dispatch for band transports.

Every reassembled message runs its handler in its own task so a slow
capability never stalls the receive loop (a stalled WS reader stops answering
pings and gets the connection killed). Messages come in two classes:

* **requests** (a JSON object with ``cap`` and no ``ok``/``kind``): at most
  ``concurrency`` handlers run at once; further requests wait in a FIFO
  backlog of ``backlog`` entries, and when that is full new requests are shed
  with a rate-limited warning (``on_shed`` may answer them "busy").
* **everything else** — replies, announces, binary Drop packets: never shed
  and never queued behind requests. Their handlers are cheap and never wait
  on other messages (a reply completes a future, a Drop packet feeds a
  receiver), so each is spawned at once. Losing one instead (the old single
  class) turned a burst of replies into timed-out calls, a lost announce
  into a worker missing from the roster, and lost Drop chunks into stalled
  pulls.

That second class is bounded by backpressure, not by dropping: when
``URGENT_HIGH`` of its handlers are in flight, :attr:`Dispatcher.saturated` is
true and the transport's receive loop awaits :meth:`Dispatcher.room` before
reading on (UDP then queues in the kernel buffer, WS in TCP).

Receive loops must also yield to the loop now and then: ``sock_recvfrom`` and
``ws.receive`` return without suspending while data is buffered, so a backlog
of frames would otherwise all be ingested before a single handler ran.

Note: a request handler that awaits a band reply (``Worker.request``) holds a
slot while it waits; its reply is not a request, so it is never stuck behind
those slots.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("rook.worker.transports.dispatch")

DEFAULT_CONCURRENCY = 64
DEFAULT_BACKLOG = 1024
WARN_EVERY = 10.0   # seconds between "backlog full" warnings
# Non-request handlers in flight before the receive loop is held back, and
# the level it resumes at. Far above a real burst's needs: each such handler
# finishes in one loop step.
URGENT_HIGH = 2048
URGENT_LOW = 512
# Longest the receive loop waits for room before reading on regardless (a
# misclassified slow handler must not wedge the link).
ROOM_TIMEOUT = 1.0
# Payloads at most this big are classified by parsing them; bigger ones (in
# practice large inline replies) by a byte scan, so a 32 MiB reply isn't
# parsed twice.
PARSE_LIMIT = 64 * 1024

Handler = Callable[[bytes, tuple], Awaitable[None]]
OnShed = Callable[[bytes, tuple], None]


def is_request(payload: bytes) -> bool:
    """Whether ``payload`` is an inbound capability request, the only kind
    that may be shed. Errs toward False (never shed): anything that isn't
    plainly a JSON object with ``cap`` and without ``ok`` (a reply) or
    ``kind`` (announces and every other new message kind, core spec 10)."""
    if payload[:1] != b"{":
        return False   # binary (Drop packets start with the band id), junk
    if len(payload) <= PARSE_LIMIT:
        try:
            msg = json.loads(payload)
        except ValueError:
            return False
        return (isinstance(msg, dict) and bool(msg.get("cap"))
                and "ok" not in msg and "kind" not in msg)
    return b'"cap"' in payload and b'"ok"' not in payload and b'"kind"' not in payload


class Dispatcher:
    def __init__(self, handler: Optional[Handler],
                 concurrency: int = DEFAULT_CONCURRENCY,
                 backlog: int = DEFAULT_BACKLOG,
                 clock: Callable[[], float] = time.monotonic,
                 classify: Callable[[bytes], bool] = is_request,
                 on_shed: Optional[OnShed] = None) -> None:
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.backlog_limit = max(0, int(backlog))
        self._backlog: collections.deque[tuple[bytes, tuple]] = collections.deque()
        self._running: set[asyncio.Task] = set()      # request handlers
        self._urgent: set[asyncio.Task] = set()       # everything else
        self._room = asyncio.Event()
        self._room.set()
        self._classify = classify
        # Called (synchronously) with each request that is shed, e.g. to
        # answer "busy". Must not raise; errors are logged and ignored.
        self.on_shed = on_shed
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

    @property
    def urgent(self) -> int:
        """Non-request handlers in flight."""
        return len(self._urgent)

    @property
    def saturated(self) -> bool:
        """True while the receive loop should hold back (see :meth:`room`)."""
        return not self._room.is_set()

    async def room(self, timeout: float = ROOM_TIMEOUT) -> None:
        """Wait until non-request handlers in flight fall to ``URGENT_LOW``
        (or ``timeout``, so a stuck handler can't wedge the link)."""
        if self._room.is_set():
            return
        try:
            await asyncio.wait_for(self._room.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def submit(self, payload: bytes, peer: tuple) -> bool:
        """Hand one message to a handler. Never blocks. Returns False if it
        was dropped (a request beyond the backlog, or stopped)."""
        if self.handler is None or self._closed:
            return False
        try:
            request = self._classify(payload)
        except Exception:
            request = False
        if not request:
            self._spawn(payload, peer, self._urgent)
            if len(self._urgent) >= URGENT_HIGH:
                self._room.clear()
            return True
        if len(self._running) < self.concurrency:
            self._spawn(payload, peer, self._running)
            return True
        if len(self._backlog) < self.backlog_limit:
            self._backlog.append((payload, peer))
            return True
        self.dropped += 1
        self._dropped_since_warn += 1
        now = self._clock()
        if now - self._last_warn >= WARN_EVERY:
            log.warning("band dispatch backlog full (%d running, %d queued); "
                        "shed %d inbound request(s)", len(self._running),
                        len(self._backlog), self._dropped_since_warn)
            self._last_warn = now
            self._dropped_since_warn = 0
        if self.on_shed is not None:
            try:
                self.on_shed(payload, peer)
            except Exception:
                log.debug("on_shed raised", exc_info=True)
        return False

    def _spawn(self, payload: bytes, peer: tuple, pool: set) -> None:
        task = asyncio.get_running_loop().create_task(self._run(payload, peer))
        pool.add(task)
        task.add_done_callback(self._done_urgent if pool is self._urgent else self._done)

    def _done_urgent(self, task: asyncio.Task) -> None:
        self._urgent.discard(task)
        if len(self._urgent) <= URGENT_LOW:
            self._room.set()

    def _done(self, task: asyncio.Task) -> None:
        self._running.discard(task)
        if self._closed:
            return
        while self._backlog and len(self._running) < self.concurrency:
            self._spawn(*self._backlog.popleft(), self._running)

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
        self._room.set()
        tasks = list(self._running) + list(self._urgent)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._running.clear()
        self._urgent.clear()
