"""Events a worker's plugins emit, on their way to the hub (``core.event``,
docs/spec/core-v2.md section 13).

One :class:`Outbox` per (hub, band) the worker is a member of. ``put`` never
blocks the plugin: the queue is bounded, and when it is full the oldest
event goes (counted, and the count travels with the next batch so the hub
can say so). One batch is in flight at a time and the next goes only when
the hub has answered, so a slow hub slows the sender instead of piling up
requests (backpressure). While the hub is away, sends fail and back off;
:meth:`Outbox.kick` (hub contact again) sends at once. A batch the hub
never answered goes again with the same event ids, which the hub uses to
drop repeats.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import secrets
from typing import Awaitable, Callable

from rook.core.backoff import Backoff

log = logging.getLogger("rook.worker.events")

MAX_QUEUED = 1000          # events kept per hub while it is away
MAX_EVENT = 16 * 1024      # bytes of one event's JSON; bigger ones are not sent
BATCH = 50                 # events per core.event request
BATCH_BYTES = 24 * 1024    # stays under the transport's inline limit with room for the envelope

Send = Callable[[list, int], Awaitable[None]]


def make(event: str, plugin: str, payload: dict, size: int) -> dict:
    """The wire form of one event (``size`` is its payload's JSON length)."""
    return {"id": secrets.token_hex(8), "event": event, "plugin": plugin, "payload": payload, "_size": size}


class Outbox:
    """``send(events, dropped)`` delivers one batch or raises."""

    def __init__(self, label: str, send: Send, *, max_queued: int = MAX_QUEUED, retry_cap: float = 30.0):
        self.label, self.send, self.max = label, send, max_queued
        self.queue: collections.deque = collections.deque()
        self.dropped = 0
        self._seq = 0
        self._sending = 0                  # the last seq of the batch in flight
        self._evicted = 0                  # of those, how many the overflow took from the queue
        self._wake = asyncio.Event()       # something queued
        self._kicked = asyncio.Event()     # hub contact: retry now
        self._backoff = Backoff(base=1.0, cap=retry_cap)
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def put(self, ev: dict) -> None:
        """Queue one event (from the loop's thread)."""
        if len(self.queue) >= self.max:
            seq, _ = self.queue.popleft()
            if seq <= self._sending:           # in flight: dropped only if this send fails
                self._evicted += 1
                return self._append(ev)
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning("event queue full: oldest dropped", extra={"fields": {
                    "hub": self.label, "dropped": self.dropped}})
        self._append(ev)

    def _append(self, ev: dict) -> None:
        self._seq += 1
        self.queue.append((self._seq, ev))
        self._wake.set()

    def kick(self) -> None:
        """The hub is back: try now, not after the backoff."""
        self._backoff.reset()
        self._kicked.set()

    def _batch(self) -> list:
        out, size = [], 0
        for seq, ev in self.queue:
            if out and (len(out) >= BATCH or size + ev["_size"] > BATCH_BYTES):
                break
            out.append((seq, ev))
            size += ev["_size"] + 100
        return out

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.queue:
                batch = self._batch()
                dropped = self.dropped
                self._sending, self._evicted = batch[-1][0], 0
                self._kicked.clear()
                try:
                    await self.send([{k: v for k, v in ev.items() if k != "_size"} for _, ev in batch], dropped)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.dropped += self._evicted
                    self._sending = self._evicted = 0
                    delay = self._backoff.next()
                    log.debug("events not delivered %s", str(e) or type(e).__name__, extra={"fields": {
                        "hub": self.label, "queued": len(self.queue), "error": str(e) or type(e).__name__,
                        "retry_in": round(delay, 1)}})
                    try:        # hub contact ends the wait early; new events don't
                        await asyncio.wait_for(self._kicked.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._backoff.reset()
                self.dropped -= dropped
                while self.queue and self.queue[0][0] <= self._sending:   # overflow may have taken some
                    self.queue.popleft()
