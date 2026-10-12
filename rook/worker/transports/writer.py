"""The band writer: one task that owns frame emission for a transport.

Before this, every ``send()`` took a transport-wide lock for all fragments of
its message, so a multi-MB reply held the link for seconds and every
keepalive, announce and small reply queued behind it (head-of-line blocking).
Now each message is queued and a single writer task emits frames:

* **control** — single-fragment messages (keepalives, announces, most
  replies) — FIFO, served first;
* **bulk** — multi-fragment messages — served round-robin, one fragment at
  a time, across up to ``BULK_ACTIVE`` messages (the rest wait FIFO), so two
  large replies share the link and a small one slips in between fragments.

Interleaving fragments of different messages is wire-compatible: every peer
reassembles by the 16-byte ``fragment_id`` in the §6.6 envelope, not by
arrival order, and sequences are assigned at emission time so they stay
monotonic per sender. A single writer also means ``ws.send_bytes`` is never
called concurrently.

:meth:`FrameWriter.submit_frame` queues one frame that is NOT a fragmented
CHANNEL message (a ROUTE declaration, a native DROP frame): the caller gives
the plaintext and a sealer, and the writer seals it at emission time like
everything else, so its sequence comes from the same source in wire order.

``submit()`` still waits until its message is fully written (or raises), so
callers keep the old ``await transport.send(...)`` semantics. A caller that
is cancelled (``asyncio.timeout`` around a call) before its message started
emitting withdraws it; once the first fragment is out the message is finished
anyway, because a half-sent message only leaves a dead partial buffer on
every peer until their reassembly timeout.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("rook.worker.transports.writer")

# After this many consecutive control frames while bulk is waiting, one bulk
# fragment goes out: control has priority but cannot starve bulk forever.
CONTROL_BURST = 16
# At most this many multi-fragment messages are interleaved at once; the rest
# wait FIFO. Every interleaved message is an open partial buffer on every
# receiver, and the reference Reassembler evicts the oldest partial beyond 256
# — so a burst of hundreds of small bulk messages (OTA Drop chunks are two
# fragments each) must not all be opened at the same time.
BULK_ACTIVE = 8
# Yield to the event loop at least every this many frames. A UDP sendto that
# succeeds immediately never suspends, so without this a large unpaced
# message would run to completion without letting anyone else enqueue.
YIELD_EVERY = 8


class TokenBucket:
    """Frame pacing: ``rate`` frames/s sustained, bursts up to ``burst``.

    Used for bulk frames on UDP so a large reply doesn't land on the hub as
    hundreds of back-to-back datagrams (its UDP egress queue is shared by
    every UDP peer and drops on overflow)."""

    def __init__(self, rate: float, burst: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate = float(rate)
        self.burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._at = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now

    def delay(self) -> float:
        """Seconds until one token is available (0 if one is now)."""
        self._refill()
        return 0.0 if self._tokens >= 1.0 else (1.0 - self._tokens) / self.rate

    def consume(self) -> None:
        """Spend one token (call only when :meth:`delay` returned 0)."""
        self._tokens -= 1.0

    async def take(self) -> None:
        """Wait for a token and spend it."""
        while True:
            wait = self.delay()
            if wait <= 0:
                self.consume()
                return
            await asyncio.sleep(wait)


class _Out:
    """One queued message."""

    __slots__ = ("chunks", "channel_id", "index", "future", "cancelled", "sealer")

    def __init__(self, chunks: list[bytes], channel_id: int,
                 future: asyncio.Future,
                 sealer: Optional[Callable[[bytes], bytes]] = None) -> None:
        self.chunks = chunks
        self.channel_id = channel_id
        self.index = 0          # next chunk to emit; > 0 means "started"
        self.future = future
        self.cancelled = False
        self.sealer = sealer    # set for submit_frame(): seals instead of seal()

    @property
    def started(self) -> bool:
        return self.index > 0


def _consume(fut: asyncio.Future) -> None:
    # Nobody may be awaiting a message whose sender was cancelled mid-send;
    # retrieve the exception so asyncio doesn't log "never retrieved".
    if not fut.cancelled():
        fut.exception()


class FrameWriter:
    """Single-writer frame scheduler for one link.

    ``split(payload)`` fragments a message; ``seal(chunk, channel_id)``
    encrypts one chunk into a wire frame (assigning its sequence);
    ``write(frame)`` puts it on the link. :class:`BandCodec` provides the
    first two. ``pacer`` (optional) throttles bulk frames only.
    """

    def __init__(self,
                 split: Callable[[bytes], list[bytes]],
                 seal: Callable[[bytes, int], bytes],
                 write: Callable[[bytes], Awaitable[None]],
                 pacer: Optional[TokenBucket] = None) -> None:
        self._split = split
        self._seal = seal
        self._write = write
        self.pacer = pacer
        self._control: collections.deque[_Out] = collections.deque()
        self._bulk: collections.deque[_Out] = collections.deque()      # interleaving
        self._waiting: collections.deque[_Out] = collections.deque()   # bulk, not yet admitted
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._current: Optional[_Out] = None   # message whose frame is being written
        self._closed = False
        # Monotonic time of the last frame actually written (any kind); the
        # transport uses it to skip keepalives on a busy link.
        self.last_tx: float = float("-inf")
        self.frames_sent = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._closed = False
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.fail_all(RuntimeError("transport stopped"))

    def fail_all(self, exc: BaseException) -> None:
        """Fail every queued message (e.g. the WS link dropped), including the
        one mid-write; the rest of its fragments are abandoned."""
        cur = self._current
        if cur is not None and not cur.future.done():
            cur.future.set_exception(exc)
        for q in (self._control, self._bulk, self._waiting):
            while q:
                m = q.popleft()
                if not m.future.done():
                    m.future.set_exception(exc)

    @property
    def pending(self) -> int:
        return len(self._control) + len(self._bulk) + len(self._waiting)

    # -- submit --------------------------------------------------------------

    async def submit(self, payload: bytes, channel_id: int = 0) -> None:
        """Queue ``payload`` and wait until all its frames are written."""
        if self._closed or self._task is None:
            raise RuntimeError("transport not started")
        chunks = self._split(payload)
        await self._enqueue(_Out(chunks, channel_id, self._future()), len(chunks) > 1)

    async def submit_frame(self, plaintext: bytes, sealer: Callable[[bytes], bytes],
                           bulk: bool = False) -> None:
        """Queue one frame built by ``sealer(plaintext)`` at emission time and
        wait until it is written. ``bulk`` puts it in the paced, round-robin
        class (native DROP chunks); otherwise it goes with control traffic."""
        if self._closed or self._task is None:
            raise RuntimeError("transport not started")
        await self._enqueue(_Out([plaintext], 0, self._future(), sealer), bulk)

    @staticmethod
    def _future() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(_consume)
        return fut

    async def _enqueue(self, msg: _Out, bulk: bool) -> None:
        fut = msg.future
        (self._waiting if bulk else self._control).append(msg)
        self._wake.set()
        try:
            # shield: our cancellation must not cancel the shared future; the
            # writer decides (below) whether the message still goes out.
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            if not msg.started and not fut.done():
                msg.cancelled = True
                self._discard(msg)
                fut.cancel()
            raise

    def _discard(self, msg: _Out) -> None:
        for q in (self._control, self._bulk, self._waiting):
            try:
                q.remove(msg)
            except ValueError:
                pass

    # -- writer --------------------------------------------------------------

    def _pick(self, streak: int) -> tuple[Optional[_Out], bool]:
        while self._control and self._control[0].cancelled:
            self._control.popleft()
        while self._waiting and len(self._bulk) < BULK_ACTIVE:
            m = self._waiting.popleft()
            if not m.cancelled:
                self._bulk.append(m)
        while self._bulk and self._bulk[0].cancelled:
            self._bulk.popleft()
        if self._control and (not self._bulk or streak < CONTROL_BURST):
            return self._control.popleft(), True
        if self._bulk:
            return self._bulk.popleft(), False
        return None, False

    async def _emit(self, msg: _Out) -> None:
        chunk = msg.chunks[msg.index]
        frame = (msg.sealer(chunk) if msg.sealer is not None
                 else self._seal(chunk, msg.channel_id))
        msg.index += 1
        self._current = msg
        try:
            await self._write(frame)
        finally:
            self._current = None
        self.last_tx = time.monotonic()
        self.frames_sent += 1

    async def _run(self) -> None:
        streak = 0       # consecutive control frames while bulk waited
        since_yield = 0
        while True:
            msg, is_control = self._pick(streak)
            if msg is None:
                streak = 0
                self._wake.clear()
                await self._wake.wait()
                continue
            if not is_control and self.pacer is not None:
                wait = self.pacer.delay()
                if wait > 0:
                    # Out of bulk budget: put the message back (it stays
                    # withdrawable and visible to stop()) and wait for either
                    # the next token or new work — a control message that
                    # arrives meanwhile goes out without waiting for the pacer.
                    self._bulk.appendleft(msg)
                    streak = 0  # bulk can't go anyway; don't hold control back
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self.pacer.consume()
            try:
                await self._emit(msg)
            except asyncio.CancelledError:
                # Stopping: the frame may or may not be out; fail the message.
                if not msg.future.done():
                    msg.future.set_exception(RuntimeError("transport stopped"))
                raise
            except Exception as e:
                # Drop the rest of this message; the caller sees the error.
                if not msg.future.done():
                    msg.future.set_exception(e)
                continue
            if msg.future.done() and msg.index < len(msg.chunks):
                continue  # failed meanwhile (fail_all): abandon the rest
            if msg.index >= len(msg.chunks):
                if not msg.future.done():
                    msg.future.set_result(None)
                if len(msg.chunks) > 1:
                    log.debug("sent %d-fragment message", len(msg.chunks))
            else:
                self._bulk.append(msg)  # round-robin: back of the line
            streak = streak + 1 if (is_control and (self._bulk or self._waiting)) else 0
            since_yield += 1
            if since_yield >= YIELD_EVERY:
                since_yield = 0
                await asyncio.sleep(0)
