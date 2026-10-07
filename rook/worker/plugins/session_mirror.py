"""sessions.mirror: the live event stream of a Claude Code session started anywhere.

The Rook Claude Code mod writes each session's events to a spool on its host
(:mod:`rook.worker.session_mirror`, docs/design/sessions.md section 3.4).
``sessions.mirror`` serves that spool from a cursor and, like
``work.stream.read``, holds the call (up to 25 s) until there is something
past the cursor or the session ends, so a viewer costs one outstanding
request. Spools of sessions closed for a week are deleted.

This plugin shares the ``sessions`` namespace with the session catalog
plugin (``sessions.list/follow/send/stop``); the loader registers caps by
their full name, so two plugins may share a namespace as long as their cap
names differ.
"""

from __future__ import annotations

import asyncio
import logging

from ..plugin import Plugin, capability
from .. import session_mirror as spool

log = logging.getLogger("rook.worker.plugins.session_mirror")

MAX_WAIT = 25.0             # long-poll ceiling, seconds (as work.stream.read)
POLL_SECS = 0.25            # how often a held call looks at the spool
COALESCE_SECS = 0.05        # gather a burst before answering a long poll
CLEANUP_SECS = 6 * 3600.0   # how often closed spools are swept


class SessionMirrorPlugin(Plugin):
    NAMESPACE = "sessions"
    NAME = "session_mirror"
    SKILL = ("Live view of a Claude Code session started in any terminal (needs the Rook "
             "Claude Code mod on that host): `sessions.mirror(agent, native_id, cursor, wait=20)` "
             "returns its events (prompt, assistant.delta/done, tool.call/result, turn.end, "
             "state, session.start/end) past `cursor`; pass the returned `cursor` back. "
             "`done` is true once the session has ended.")

    def __init__(self) -> None:
        super().__init__()
        self._readers: dict[tuple[str, str], spool.SpoolReader] = {}
        self._sweeper: asyncio.Task | None = None

    async def start(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    async def _sweep(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(spool.cleanup)
                if removed:
                    log.info("removed %d closed session mirror spool(s)", len(removed))
            except Exception:
                log.exception("session mirror cleanup failed")
            await asyncio.sleep(CLEANUP_SECS)

    def _reader(self, agent: str, native_id: str) -> spool.SpoolReader:
        key = spool.check(agent, native_id)
        reader = self._readers.get(key)
        if reader is None:
            if len(self._readers) > 256:
                self._readers.clear()
            reader = self._readers[key] = spool.SpoolReader(*key)
        return reader

    @capability("mirror", risk="read", tags=("sensitive",))
    async def mirror(self, agent: str, native_id: str, cursor: int = 0, wait: float = 0,
                     max_events: int = spool.MAX_EVENTS) -> dict:
        """Live events of a session the Rook Claude Code mod mirrors on this host,
        those after ``cursor`` (the last ``seq`` seen; 0 for all). With ``wait`` > 0
        the call holds (up to 25 s) until there are new events or the session
        ends. Returns ``{ok, events, cursor, done}``; pass ``cursor`` back."""
        reader = self._reader(agent, native_id)
        cursor = max(0, int(cursor))
        max_events = max(1, min(int(max_events), spool.MAX_EVENTS))
        wait = max(0.0, min(float(wait), MAX_WAIT))
        out = await asyncio.to_thread(reader.read, cursor, max_events)
        if wait and not out["events"] and not out["done"]:
            loop = asyncio.get_running_loop()
            end = loop.time() + wait
            stamp = await asyncio.to_thread(reader.stamp)
            while loop.time() < end:
                await asyncio.sleep(min(POLL_SECS, max(0.0, end - loop.time())))
                now = await asyncio.to_thread(reader.stamp)
                if now == stamp:
                    continue
                stamp = now
                await asyncio.sleep(COALESCE_SECS)
                out = await asyncio.to_thread(reader.read, cursor, max_events)
                if out["events"] or out["done"]:
                    break
        return out


PLUGIN = SessionMirrorPlugin
