"""The console pump — drains worker ``proc`` sessions into console rooms.

One puller, on the ordinary request/reply path, doing ``proc.read(handle,
cursor)`` against each live session and appending what comes back to its room.
Consumers (agents over MCP, the dashboard) then read the room, so N readers
cost the band nothing — the alternative, workers pushing output as it appears,
would put a byte-per-packet firehose through a hub that broadcasts to every
peer on the band.

A session whose worker stops answering is closed out rather than left live
forever: the record says the worker went away, which is true and useful, and
the room freezes like any other.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("rook.band_mcp.console_pump")

POLL_IDLE = 2.0        # seconds between sweeps when nothing is producing
POLL_BUSY = 0.4        # …when at least one session is still pouring out data
# Per read. The band fragments at 1003 bytes with no retransmit, so a big reply
# is a long bet: 8 KB is ~9 fragments, 32 KB would be ~33. Poll often with small
# reads instead — the cursor only advances on a reply that actually arrived, so
# a dropped one costs a single cycle and never loses output.
READ_BYTES = 8192
CALL_TIMEOUT = 12.0
MAX_MISSES = 5         # consecutive failed reads before we give up on a session
SWEEP_EVERY = 60.0     # how often to freeze abandoned 'closing' rooms


class ConsolePump:
    def __init__(self, client, store) -> None:
        self.client = client
        self.store = store
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._misses: dict[str, int] = {}
        self._last_sweep = 0.0

    def start(self) -> None:
        if self._task is None and self.store.enabled:
            self._task = asyncio.create_task(self._loop())
            log.info("console pump started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            busy = False
            try:
                rooms = self.store.live_rooms()
                if rooms:
                    results = await asyncio.gather(
                        *(self._drain(r) for r in rooms), return_exceptions=True)
                    busy = any(r is True for r in results)
                now = time.time()
                if now - self._last_sweep > SWEEP_EVERY:
                    self._last_sweep = now
                    self.store.sweep_closing()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("console pump sweep failed")
            try:
                await asyncio.sleep(POLL_BUSY if busy else POLL_IDLE)
            except asyncio.CancelledError:
                break

    async def _drain(self, room: dict) -> bool:
        """Pull one session forward. Returns True if it produced output."""
        rid, worker, handle = room["room"], room["worker"], room["handle"]
        try:
            reply = await self.client.call(
                cap="proc.read",
                args={"handle": handle, "cursor": int(room["cursor"] or 0),
                      "max_bytes": READ_BYTES},
                target=worker, timeout=CALL_TIMEOUT, identity="console-pump")
        except asyncio.TimeoutError:
            return self._miss(rid, "worker did not answer")
        except Exception as e:
            return self._miss(rid, f"{type(e).__name__}: {e}")

        if not reply.get("ok"):
            return self._miss(rid, str(reply.get("error", "call failed")))
        result = reply.get("result") or {}
        if not result.get("ok"):
            # The worker answered but the handle is gone — it restarted, or the
            # session was reaped. Nothing more is coming.
            self._close(rid, None, f"session lost on worker: "
                                   f"{result.get('error', 'unknown')}")
            return False

        self._misses.pop(rid, None)
        chunk = result.get("chunk") or ""
        if result.get("dropped"):
            self.store.append(rid, f"— {result['dropped']} bytes dropped "
                                   f"(output outran the buffer) —", stream="sys")
        if chunk:
            self.store.append(rid, chunk, stream="out")
        self.store.set_cursor(rid, int(result.get("next_cursor") or 0))

        if result.get("eof"):
            self.store.mark_closing(rid, result.get("exit_code"))
            self._misses.pop(rid, None)
            return False
        return bool(chunk)

    def _miss(self, rid: str, why: str) -> bool:
        n = self._misses.get(rid, 0) + 1
        self._misses[rid] = n
        if n >= MAX_MISSES:
            self._close(rid, None, f"worker unreachable after {n} tries ({why})")
        return False

    def _close(self, rid: str, exit_code: int | None, note: str) -> None:
        self.store.append(rid, f"— {note} —", stream="sys")
        self.store.mark_closing(rid, exit_code)
        self._misses.pop(rid, None)
        log.info("console room %s closed out: %s", rid, note)
