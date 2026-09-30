"""Hub-side fan-out for live worker terminals (``work.stream.*``).

One :class:`TermStream` per (worker, terminal) follows the worker's PTY with a
long-poll on ``work.stream.read`` and keeps a bounded replay ring. Any number
of browser viewers attach to it; each gets the ring on attach (or just the
tail past its cursor on reconnect) and then every new chunk as it lands.

Memory is bounded on every axis, for a hub host with ~1 GB of RAM:

* per stream: ``RING_BYTES`` of replay, ``INPUT_MAX`` of pending input;
* per viewer: ``VIEWER_MAX`` of queued output. A viewer that can't keep up
  has its queue dropped and is resynced from the ring (reset + replay)
  instead of stalling the stream or growing without bound;
* streams: at most ``MAX_STREAMS``; idle ones (no viewers) stop pulling
  after ``IDLE_SECS`` and are dropped after ``KEEP_SECS``.

Exactly one viewer holds input at a time. The first viewer to type while
nobody holds takes it; ``take`` steals it, ``release`` gives it up and
``handoff`` passes it to another viewer. Only the holder's resizes reach the
PTY; every viewer is told the PTY size so all render the same grid.

Wire to the browser: binary frames are output, an 8-byte big-endian stream
offset followed by raw bytes; JSON text frames carry state and control.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid

from ..worker import termwire

log = logging.getLogger(__name__)

RING_BYTES = 256 * 1024
VIEWER_MAX = 512 * 1024
INPUT_MAX = 64 * 1024
WRITE_CHUNK = 4096
READ_BYTES = 16 * 1024
LONG_POLL = 10.0
CALL_SLACK = 6.0
MAX_MISSES = 4
MAX_STREAMS = 32
MAX_VIEWERS = 16
IDLE_SECS = 60.0        # keep following this long after the last viewer leaves
KEEP_SECS = 300.0       # then keep the ring this long for late viewers


class Viewer:
    """One attached browser. Output is queued here and drained by its socket."""

    def __init__(self, label: str) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.label = label
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queued = 0
        self.resync = False
        self.closed = False
        self.sent: int | None = None   # stream offset this viewer has up to

    def push(self, item, size: int = 0) -> None:
        if self.closed:
            return
        if size and self.queued + size > VIEWER_MAX:
            # Too slow: drop what's pending and replay from the ring instead.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queued = 0
            self.resync = True
            self.queue.put_nowait(("resync", None))
            return
        self.queued += size
        self.queue.put_nowait(("frame", item) if size else ("json", item))

    async def next(self):
        """The next item to send, as ``(kind, item)``. Frames are trimmed so a
        viewer never receives bytes it already has (a resync replays the ring,
        and frames queued before it may overlap)."""
        while True:
            kind, item = await self.queue.get()
            if kind == "frame":
                self.queued -= len(item) - 8
                start = struct.unpack(">Q", item[:8])[0]
                end = start + len(item) - 8
                if self.sent is not None and end <= self.sent:
                    continue
                if self.sent is not None and start < self.sent:
                    item = frame(self.sent, item[8 + self.sent - start:])
                self.sent = end
            elif kind == "json" and item.get("type") == "reset":
                self.sent = None
            return kind, item


def frame(start: int, data: bytes) -> bytes:
    return struct.pack(">Q", start) + data


class TermStream:
    def __init__(self, hub: "TermHub", worker_id: str, term_id: str) -> None:
        self.hub = hub
        self.worker_id = worker_id
        self.term_id = term_id
        self.ring = bytearray()
        self.start = 0            # stream offset of ring[0]
        self.end = 0              # stream offset one past the last byte
        self.primed = False       # first read done (start/end are real)
        self.running = True
        self.exit_code: int | None = None
        self.lost = ""            # why we stopped hearing from the worker
        self.cols = 0
        self.rows = 0
        self.viewers: dict[str, Viewer] = {}
        self.holder: str | None = None
        self.pull: asyncio.Task | None = None
        self.input = bytearray()
        self.writer: asyncio.Task | None = None
        self.resizer: asyncio.Task | None = None
        self.want_size: tuple[int, int] | None = None
        self.idle_since = time.monotonic()
        self.touched = time.monotonic()

    # -- viewer side ---------------------------------------------------------

    def state(self) -> dict:
        return {"type": "state", "holder": self.holder, "cols": self.cols, "rows": self.rows,
                "running": self.running, "exit_code": self.exit_code, "lost": self.lost,
                "viewers": [{"id": v.id, "label": v.label} for v in self.viewers.values()]}

    def broadcast_state(self) -> None:
        msg = self.state()
        for v in self.viewers.values():
            v.push(msg)

    def attach(self, viewer: Viewer, since: int | None = None) -> None:
        if len(self.viewers) >= MAX_VIEWERS:
            raise ValueError("Too many viewers on this terminal.")
        self.viewers[viewer.id] = viewer
        self.touched = time.monotonic()
        viewer.push({**self.state(), "type": "hello", "viewer": viewer.id})
        self.replay(viewer, since)
        self.broadcast_state()
        self.ensure_pull()

    def replay(self, viewer: Viewer, since: int | None = None) -> None:
        if since is not None and self.primed and self.start <= since <= self.end:
            if since < self.end:
                viewer.push(frame(since, bytes(self.ring[since - self.start:])),
                            self.end - since)
            return
        viewer.push({"type": "reset", "start": self.start})
        if self.ring:
            viewer.push(frame(self.start, bytes(self.ring)), len(self.ring))

    def detach(self, viewer: Viewer) -> None:
        viewer.closed = True
        if self.viewers.pop(viewer.id, None) is None:
            return
        if self.holder == viewer.id:
            self.holder = None
        if not self.viewers:
            self.idle_since = time.monotonic()
        self.broadcast_state()

    def control(self, viewer: Viewer, data: dict) -> None:
        op = data.get("op")
        if op == "take":
            self.holder = viewer.id
        elif op == "release":
            if self.holder == viewer.id:
                self.holder = None
        elif op == "handoff":
            if self.holder != viewer.id:
                raise ValueError("Only the input holder can hand off.")
            to = str(data.get("to", ""))
            if to not in self.viewers:
                raise ValueError("That viewer has left.")
            self.holder = to
        elif op == "input":
            self.send_input(viewer, termwire.decode(str(data.get("enc", "t")),
                                                    str(data.get("data", "")), limit=INPUT_MAX))
            return
        elif op == "resize":
            if self.holder != viewer.id:
                return  # only the holder sizes the PTY
            cols, rows = int(data.get("cols", 0)), int(data.get("rows", 0))
            if 20 <= cols <= 500 and 5 <= rows <= 200:
                self.request_resize(cols, rows)
            return
        elif op == "signal":
            if self.holder not in (None, viewer.id):
                raise ValueError("Take control before signalling.")
            sig = str(data.get("sig", "INT")).upper()
            self.hub.spawn(self.hub.call(self.worker_id, "work.stream.signal",
                                         {"id": self.term_id, "sig": sig}))
            return
        else:
            raise ValueError("Unknown terminal operation.")
        self.broadcast_state()

    # -- input / resize --------------------------------------------------------

    def send_input(self, viewer: Viewer, raw: bytes) -> None:
        if not self.running:
            raise ValueError("The terminal has exited.")
        if self.holder is None:
            self.holder = viewer.id
            self.broadcast_state()
        if self.holder != viewer.id:
            raise ValueError("Another viewer has control. Take control to type.")
        if len(self.input) + len(raw) > INPUT_MAX:
            raise ValueError("Input is arriving faster than the host accepts it.")
        self.input.extend(raw)
        if self.writer is None or self.writer.done():
            self.writer = self.hub.spawn(self._flush_input())

    async def _flush_input(self) -> None:
        # One write in flight at a time keeps keystrokes in order; whatever
        # arrives meanwhile is coalesced into the next call.
        while self.input and self.running:
            chunk = bytes(self.input[:WRITE_CHUNK])
            del self.input[:WRITE_CHUNK]
            enc, data = termwire.encode(chunk, ("t", "b"))
            try:
                await self.hub.call(self.worker_id, "work.stream.write",
                                    {"id": self.term_id, "data": data, "enc": enc})
            except Exception as e:
                self.input.clear()
                for v in self.viewers.values():
                    v.push({"type": "error", "error": f"Input was not delivered: {e}"})
                return

    def request_resize(self, cols: int, rows: int) -> None:
        self.want_size = (cols, rows)
        if self.resizer is None or self.resizer.done():
            self.resizer = self.hub.spawn(self._apply_resize())

    async def _apply_resize(self) -> None:
        while self.want_size and self.running:
            cols, rows = self.want_size
            self.want_size = None
            if (cols, rows) == (self.cols, self.rows):
                continue
            try:
                r = await self.hub.call(self.worker_id, "work.stream.resize",
                                        {"id": self.term_id, "cols": cols, "rows": rows})
                self.cols, self.rows = r.get("cols", cols), r.get("rows", rows)
                self.broadcast_state()
            except Exception:
                log.debug("resize failed for %s", self.term_id, exc_info=True)
            await asyncio.sleep(0.1)  # debounce drag-resizes

    # -- output ----------------------------------------------------------------

    def ensure_pull(self) -> None:
        if self.running and (self.pull is None or self.pull.done()):
            self.pull = self.hub.spawn(self._pull())

    def _append(self, at: int, raw: bytes) -> None:
        if not self.primed or at != self.end:
            # First read, or the worker's ring rolled past us: restart the
            # replay at the worker's cursor and resync every viewer.
            self.ring = bytearray(raw)
            self.start, self.end = at, at + len(raw)
            first = not self.primed
            self.primed = True
            for v in self.viewers.values():
                if first:
                    if raw:
                        v.push(frame(at, raw), len(raw))
                else:
                    self.replay(v)
            return
        self.ring.extend(raw)
        self.end += len(raw)
        if len(self.ring) > RING_BYTES:
            drop = len(self.ring) - RING_BYTES
            del self.ring[:drop]
            self.start += drop
        if raw:
            f = frame(at, raw)
            for v in self.viewers.values():
                v.push(f, len(raw))

    async def _pull(self) -> None:
        misses = 0
        while self.running:
            if not self.viewers and time.monotonic() - self.idle_since > IDLE_SECS:
                return
            try:
                r = await self.hub.call(
                    self.worker_id, "work.stream.read",
                    {"id": self.term_id, "cursor": self.end if self.primed else 0,
                     "max_bytes": READ_BYTES, "wait": LONG_POLL, "accept": "tbz"},
                    timeout=LONG_POLL + CALL_SLACK)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                gone = "no such terminal" in str(e)
                misses += 1
                if gone or misses >= MAX_MISSES:
                    self.finish(None, "Terminal is gone from its host." if gone
                                else f"Host stopped answering ({e or 'timeout'}).")
                    return
                await asyncio.sleep(min(2.0 * misses, 5.0))
                continue
            misses = 0
            raw = termwire.decode(r.get("enc", "t"), r.get("data", ""))
            self._append(int(r.get("cursor", self.end)), raw)
            if (r.get("cols"), r.get("rows")) != (self.cols, self.rows) and r.get("cols"):
                self.cols, self.rows = r["cols"], r["rows"]
                self.broadcast_state()
            if r.get("eof"):
                self.finish(r.get("exit_code"))
                return

    def finish(self, exit_code, lost: str = "") -> None:
        if not self.running:
            return
        self.running = False
        self.exit_code = exit_code
        self.lost = lost
        self.holder = None
        self.input.clear()
        self.idle_since = time.monotonic()
        self.broadcast_state()
        self.hub.ended(self)

    def cancel(self) -> None:
        for t in (self.pull, self.writer, self.resizer):
            if t is not None and not t.done():
                t.cancel()
        for v in list(self.viewers.values()):
            v.push({"type": "error", "error": "Terminal stream closed."})
            v.closed = True
        self.viewers.clear()


class TermHub:
    """All live terminal streams this hub process is following."""

    def __init__(self, band_getter, on_end=None, identity: str = "system:work-term") -> None:
        self._band = band_getter
        self.on_end = on_end
        self.identity = identity
        self.streams: dict[tuple[str, str], TermStream] = {}
        self.tasks: set[asyncio.Task] = set()
        self._sweeper: asyncio.Task | None = None

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def call(self, worker_id: str, cap: str, args: dict, timeout: float = 12.0) -> dict:
        band = self._band()
        if band is None:
            raise ValueError("The band is not connected.")
        reply = await band.call(cap=cap, args=args, target=worker_id, timeout=timeout,
                                identity=self.identity)
        if not reply.get("ok") or reply.get("from") != worker_id:
            raise ValueError(reply.get("error") or "Worker did not acknowledge the request.")
        result = reply.get("result") or {}
        if isinstance(result, dict) and result.get("ok") is False:
            raise ValueError(result.get("error") or "Worker operation failed.")
        return result

    def stream(self, worker_id: str, term_id: str) -> TermStream:
        key = (worker_id, term_id)
        s = self.streams.get(key)
        if s is None:
            self.sweep()
            if len(self.streams) >= MAX_STREAMS:
                idle = sorted((x for x in self.streams.values() if not x.viewers),
                              key=lambda x: x.touched)
                if not idle:
                    raise ValueError("Too many live terminals are open on this hub.")
                self.drop(idle[0])
            s = self.streams[key] = TermStream(self, worker_id, term_id)
        if self._sweeper is None or self._sweeper.done():
            try:
                self._sweeper = self.spawn(self._sweep_loop())
            except RuntimeError:
                pass
        return s

    def get(self, worker_id: str, term_id: str) -> TermStream | None:
        return self.streams.get((worker_id, term_id))

    def ended(self, stream: TermStream) -> None:
        if self.on_end is not None:
            try:
                self.on_end(stream)
            except Exception:
                log.exception("terminal end hook failed")

    def drop(self, stream: TermStream) -> None:
        stream.cancel()
        self.streams.pop((stream.worker_id, stream.term_id), None)

    def sweep(self) -> None:
        now = time.monotonic()
        for s in list(self.streams.values()):
            if s.viewers:
                continue
            idle = now - s.idle_since
            if idle > IDLE_SECS + KEEP_SECS or (not s.running and idle > KEEP_SECS):
                self.drop(s)

    async def _sweep_loop(self) -> None:
        while self.streams:
            await asyncio.sleep(30)
            self.sweep()

    def memory(self) -> int:
        """Bytes held in rings, pending input and viewer queues."""
        return sum(len(s.ring) + len(s.input) + sum(v.queued for v in s.viewers.values())
                   for s in self.streams.values())

    async def stop(self) -> None:
        for s in list(self.streams.values()):
            s.cancel()
        self.streams.clear()
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
