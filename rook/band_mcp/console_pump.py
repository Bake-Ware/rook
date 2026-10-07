"""The console pump — drains worker sessions into console rooms.

One puller, on the ordinary request/reply path, reading each live room's
process by cursor and appending what comes back to its room. A room's process
is a **Rook terminal** (``work.stream.read(id, cursor)``) on workers whose
terminals take commands, so the same process is streamable on the Sessions
page; on older workers, and for rooms opened before that, it is a ``proc``
session (``proc.read(handle, cursor)``). Consumers (agents over MCP, the
dashboard) then read the room, so N readers cost the band nothing — the
alternative, workers pushing output as it appears, would put a byte-per-packet
firehose through a hub that broadcasts to every peer on the band.

A session whose worker stops answering is closed out rather than left live
forever: the record says the worker went away, which is true and useful, and
the room freezes like any other.

The pump also watches Rook terminals an agent started for a task
(``work.stream.open(task=…)`` through ``rook_call``): one ``work.stream.list``
per worker every ``WATCH_EVERY`` seconds, and ``on_end`` when one is gone.
"""

from __future__ import annotations

import asyncio
import codecs
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
WATCH_EVERY = 30.0     # how often to check watched terminals
WATCH_MAX_SECS = 7 * 86400   # stop watching a terminal whose worker never comes back


def _decode(enc: str, data: str) -> bytes:
    from ..worker.termwire import decode
    return decode(enc, data, limit=4 * 1024 * 1024)


class ConsolePump:
    def __init__(self, client, store, on_end=None) -> None:
        """``on_end(kind, ref, what, exit_code)`` (optional) is called when a
        room's process ends on its own (``kind`` ``console``, ``ref`` the room
        id) or a watched terminal ends (``kind`` ``session``, ``ref`` its link)."""
        self.client = client
        self.store = store
        self.on_end = on_end
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._misses: dict[str, int] = {}
        self._decoders: dict[str, codecs.IncrementalDecoder] = {}
        self._last_sweep = 0.0
        self._last_watch = 0.0

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
                if now - self._last_watch > WATCH_EVERY:
                    self._last_watch = now
                    await self.check_watched()
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
        if room.get("transport") == "term":
            return await self._drain_term(room)
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
            self._ended(rid, result.get("exit_code"))
            return False
        return bool(chunk)

    async def _drain_term(self, room: dict) -> bool:
        """The same for a room whose process is a Rook terminal: the cursor is
        the terminal's byte offset, and output arrives as encoded bytes."""
        rid, worker, tid = room["room"], room["worker"], room["handle"]
        try:
            reply = await self.client.call(
                cap="work.stream.read",
                args={"id": tid, "cursor": int(room["cursor"] or 0),
                      "max_bytes": READ_BYTES, "accept": "tbz"},
                target=worker, timeout=CALL_TIMEOUT, identity="console-pump")
        except asyncio.TimeoutError:
            return self._miss(rid, "worker did not answer")
        except Exception as e:
            return self._miss(rid, f"{type(e).__name__}: {e}")

        result = reply.get("result") or {}
        error = str(reply.get("error") or result.get("error") or "")
        if "no such terminal" in error:
            # The worker restarted, or the terminal was closed and reaped.
            self._close(rid, None, f"session lost on worker: {error}")
            return False
        if not reply.get("ok") or not result.get("ok"):
            return self._miss(rid, error or "call failed")

        self._misses.pop(rid, None)
        try:
            raw = _decode(result.get("enc") or "t", result.get("data") or "")
        except Exception as e:
            return self._miss(rid, f"undecodable output: {e}")
        if result.get("dropped"):
            self.store.append(rid, f"— {result['dropped']} bytes dropped "
                                   f"(output outran the buffer) —", stream="sys")
        # A multi-byte character can straddle two reads: decode incrementally.
        dec = self._decoders.get(rid)
        if dec is None:
            dec = self._decoders[rid] = codecs.getincrementaldecoder("utf-8")("replace")
        text = dec.decode(raw, final=bool(result.get("eof")))
        if text:
            self.store.append(rid, text, stream="out")
        self.store.set_cursor(rid, int(result.get("next") or 0))

        if result.get("eof"):
            self._decoders.pop(rid, None)
            self._ended(rid, result.get("exit_code"))
            return False
        return bool(raw)

    def _miss(self, rid: str, why: str) -> bool:
        n = self._misses.get(rid, 0) + 1
        self._misses[rid] = n
        if n >= MAX_MISSES:
            self._close(rid, None, f"worker unreachable after {n} tries ({why})")
        return False

    def _ended(self, rid: str, exit_code) -> None:
        self.store.mark_closing(rid, exit_code)
        self._misses.pop(rid, None)
        self._notify("console", rid, f"process exited (code {exit_code})", exit_code)

    def _close(self, rid: str, exit_code: int | None, note: str) -> None:
        self.store.append(rid, f"— {note} —", stream="sys")
        self.store.mark_closing(rid, exit_code)
        self._misses.pop(rid, None)
        self._decoders.pop(rid, None)
        log.info("console room %s closed out: %s", rid, note)
        self._notify("console", rid, note, exit_code)

    def _notify(self, kind: str, ref: str, what: str, exit_code) -> None:
        if self.on_end is None:
            return
        try:
            self.on_end(kind, ref, what, exit_code)
        except Exception:
            log.exception("console pump: end hook failed for %s %s", kind, ref)

    async def check_watched(self, now: float | None = None) -> int:
        """One ``work.stream.list`` per worker with watched terminals; report
        the ones that ended or are gone. A worker that does not answer keeps
        its watches (it may come back) for up to WATCH_MAX_SECS. Returns how
        many ended."""
        now = now or time.time()
        by_worker: dict[str, list[dict]] = {}
        for w in self.store.watched():
            by_worker.setdefault(w["worker"], []).append(w)
        ended = 0
        for worker, watches in by_worker.items():
            try:
                reply = await self.client.call(cap="work.stream.list", args={}, target=worker,
                                               timeout=CALL_TIMEOUT, identity="console-pump")
                result = reply.get("result") or {}
                if not reply.get("ok") or not result.get("ok"):
                    raise ValueError(reply.get("error") or result.get("error") or "call failed")
            except Exception:
                for w in watches:
                    if now - (w["created"] or now) > WATCH_MAX_SECS:
                        self.store.unwatch(worker, w["term"])
                continue
            terms = {t.get("id"): t for t in result.get("terminals") or [] if isinstance(t, dict)}
            for w in watches:
                t = terms.get(w["term"])
                if t is not None and t.get("running"):
                    continue
                code = t.get("exit_code") if t else None
                what = (f"Rook terminal {w['term']} exited (code {code})" if t
                        else f"Rook terminal {w['term']} is gone from its worker")
                self.store.unwatch(worker, w["term"])
                ended += 1
                self._notify("session", w["ref"], what, code)
        return ended
