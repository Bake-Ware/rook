"""The stream API: open, append, read by cursor, follow, attach, close.

One :class:`Streams` per host. A worker holds the bounded streams of the
terminals and processes it runs; the hub holds the durable rooms, huddles
and sessions. The ``stream.*`` caps (open/read/write/attach/list/close,
search/delete) are thin wrappers over these methods.

A **producer** is what writes a stream's output: a PTY, a process, an agent
harness, a voice link. It attaches with :meth:`Streams.attach`, appends
output through its handle and takes ``input`` events from
:meth:`Producer.inputs`. Input written while no producer is attached waits
in the stream; the next producer gets it (that is how a message wakes a
sleeping agent). One producer at a time.

Appends and waits run on one event loop thread; a producer reading in
another thread hands its chunks over with ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import AsyncIterator

from .events import KINDS, Event, Meta, StreamError, StreamSpec, check_body, check_kind
from .store import DEFAULT_BUDGET, MemoryStore, SqliteStore

READ_LIMIT = 1000


class Streams:
    def __init__(self, durable: SqliteStore | None = None, bounded: MemoryStore | None = None,
                 host: str = "", clock=time.time) -> None:
        self.durable = durable
        self.bounded = bounded if bounded is not None else MemoryStore()
        self.host = host
        self.clock = clock
        self._changed: dict[str, asyncio.Event] = {}
        self._producers: dict[str, "Producer"] = {}

    # -- streams -----------------------------------------------------------

    def open(self, kind: str, *, spec: StreamSpec | None = None, title: str = "",
             host: str | None = None, producer: str = "", participants=(), attrs=None,
             budget: int = DEFAULT_BUDGET) -> Meta:
        """Make a stream. ``spec`` defaults to the kind's (``KINDS``); a kind
        not listed there must pass one. ``budget`` sizes a bounded buffer."""
        check_kind(kind)
        spec = spec or KINDS.get(kind)
        if spec is None:
            raise StreamError(f"kind {kind} has no default; pass a spec")
        store = self._store_for(spec)
        meta = Meta(id="s_" + secrets.token_hex(8), kind=kind, spec=spec, title=str(title)[:200],
                    host=self.host if host is None else host, producer=producer,
                    participants=list(dict.fromkeys(participants)), attrs=dict(attrs or {}),
                    created=self.clock())
        store.create(meta, budget)
        return meta

    def get(self, sid: str) -> Meta:
        return self._store(sid).get(sid)

    def list(self, kind: str | None = None, participant: str | None = None,
             open_only: bool = False) -> list[Meta]:
        metas = self.bounded.list() + (self.durable.list() if self.durable else [])
        return sorted((m for m in metas
                       if (kind is None or m.kind == kind)
                       and (participant is None or participant in m.participants)
                       and not (open_only and m.closed)), key=lambda m: m.created)

    def update(self, sid: str, *, title: str | None = None, participants=None,
               attrs: dict | None = None) -> Meta:
        """Change metadata: title, the participant list, or merge ``attrs``."""
        store = self._store(sid)
        meta = store.get(sid)
        if title is not None:
            meta.title = str(title)[:200]
        if participants is not None:
            meta.participants = list(dict.fromkeys(participants))
        if attrs:
            meta.attrs.update(attrs)
        store.save(meta)
        return meta

    def close(self, sid: str, by: str = "", **status) -> Meta:
        """Finish a stream: it stays readable but takes no more events.
        Writes a final ``status`` event (``state: closed`` plus ``status``)."""
        store = self._store(sid)
        meta = store.get(sid)
        if meta.closed:
            return meta
        store.append(sid, "status", by, {**status, "state": "closed"}, self.clock())
        meta = store.get(sid)
        meta.closed = self.clock()
        store.save(meta)
        self._producers.pop(sid, None)
        self._notify(sid)
        return meta

    def delete(self, sid: str) -> None:
        """Erase a stream and every event for good (its own cap: a hard stop)."""
        self._store(sid).delete(sid)
        self._producers.pop(sid, None)
        self._notify(sid)

    # -- events ------------------------------------------------------------

    def append(self, sid: str, type_: str, body, by: str = "") -> Event:
        """Add one event. ``input`` needs an interactive stream; other types
        must be in the stream's spec (``status`` always is)."""
        store = self._store(sid)
        meta = store.get(sid)
        if meta.closed:
            raise StreamError(f"stream {sid} is closed")
        if not meta.spec.allows(type_):
            raise StreamError(f"stream {sid} ({meta.kind}) does not take {type_} events")
        ev = store.append(sid, type_, by, check_body(type_, body), self.clock())
        self._notify(sid)
        return ev

    async def read(self, sid: str, after: int = 0, limit: int = 200, types=None,
                   wait: float = 0.0) -> dict:
        """Events after cursor ``after``: ``{events, next, dropped, closed}``.
        Pass ``next`` back as ``after``. ``dropped`` counts events a bounded
        stream discarded before you got to them. With ``wait`` > 0 an empty
        read holds up to that many seconds for new events (long-poll)."""
        limit = max(1, min(int(limit), READ_LIMIT))
        types = tuple(types) if types else None
        changed = self._watch(sid) if wait > 0 else None
        out = self._read_now(sid, after, limit, types)
        if not out["events"] and not out["closed"] and changed is not None:
            try:
                await asyncio.wait_for(changed.wait(), wait)
            except asyncio.TimeoutError:
                return out
            first = out
            out = self._read_now(sid, first["next"], limit, types)
            out["dropped"] += first["dropped"]
        return out

    async def follow(self, sid: str, after: int = 0, types=None) -> AsyncIterator[Event]:
        """Every event after ``after`` as it arrives; ends once the stream is
        closed and read to the end. A jump in ``seq`` means a bounded stream
        dropped events before this reader got to them."""
        types = tuple(types) if types else None
        while True:
            changed = self._watch(sid)
            out = self._read_now(sid, after, READ_LIMIT, types)
            for ev in out["events"]:
                yield ev
            after = out["next"]
            if out["events"]:
                continue
            if out["closed"]:
                return
            await changed.wait()

    def search(self, query: str, limit: int = 20, kind: str | None = None) -> list[dict]:
        """Streams whose events contain ``query`` (case-insensitive), newest first."""
        if not query:
            raise StreamError("search needs a query")
        hits = self.bounded.search(query, limit, kind) + (
            self.durable.search(query, limit, kind) if self.durable else [])
        hits.sort(key=lambda h: h[1].at, reverse=True)
        return [{"stream": m.id, "kind": m.kind, "title": m.title, "seq": ev.seq,
                 "type": ev.type, "by": ev.by, "at": ev.at, "text": ev.plain()[:300]}
                for m, ev in hits[:limit]]

    # -- producers ---------------------------------------------------------

    def attach(self, sid: str, producer: str) -> "Producer":
        """Become the stream's producer (a harness waking on a session, a
        PTY on a terminal). Refused while another producer is attached."""
        meta = self.get(sid)
        if meta.closed:
            raise StreamError(f"stream {sid} is closed")
        if sid in self._producers:
            raise StreamError(f"stream {sid} already has a producer ({self._producers[sid].name})")
        if producer and producer != meta.producer:
            store = self._store(sid)
            meta.producer = producer
            store.save(meta)
        handle = Producer(self, sid, producer or meta.producer)
        self._producers[sid] = handle
        return handle

    def attached(self, sid: str) -> bool:
        return sid in self._producers

    def export(self, sid: str, title: str | None = None) -> Meta:
        """Copy a bounded stream's buffer into a new durable stream (keep a
        terminal's output past its ring)."""
        src = self.get(sid)
        if src.spec.durable:
            raise StreamError(f"stream {sid} is already durable")
        spec = StreamSpec(src.spec.types, src.spec.input, durable=True)
        dst = self.open(src.kind, spec=spec, title=title or src.title, host=self.host,
                        producer=src.producer, participants=src.participants,
                        attrs={**src.attrs, "exported_from": sid})
        after = 0
        while True:
            events, after, _ = self.bounded.read(sid, after, READ_LIMIT)
            for ev in events:
                self.durable.append(dst.id, ev.type, ev.by, ev.body, ev.at)
            if len(events) < READ_LIMIT:
                return self.durable.get(dst.id)

    # -- plumbing ----------------------------------------------------------

    def _store_for(self, spec: StreamSpec):
        if not spec.durable:
            return self.bounded
        if self.durable is None:
            raise StreamError("durable streams live on the hub; this host has no durable store")
        return self.durable

    def _store(self, sid: str):
        if sid in self.bounded:
            return self.bounded
        if self.durable is not None and sid in self.durable:
            return self.durable
        raise StreamError(f"no stream {sid}")

    def _read_now(self, sid: str, after: int, limit: int, types) -> dict:
        store = self._store(sid)
        events, nxt, dropped = store.read(sid, after, limit, types)
        return {"events": events, "next": nxt, "dropped": dropped,
                "closed": store.get(sid).closed is not None}

    def _watch(self, sid: str) -> asyncio.Event:
        """Get the wake-up for ``sid`` *before* reading, so an append between
        the read and the wait is never missed."""
        ev = self._changed.get(sid)
        if ev is None:
            ev = self._changed[sid] = asyncio.Event()
        return ev

    def _notify(self, sid: str) -> None:
        ev = self._changed.pop(sid, None)
        if ev is not None:
            ev.set()


class Producer:
    """An attached producer's handle: write output, take input, let go."""

    def __init__(self, streams: Streams, sid: str, name: str) -> None:
        self.streams = streams
        self.id = sid
        self.name = name

    @property
    def live(self) -> bool:
        return self.streams._producers.get(self.id) is self

    def result(self) -> dict:
        """What a cap that opened this stream returns."""
        return {"stream": self.id}

    def append(self, type_: str, body) -> Event:
        if not self.live:
            raise StreamError(f"producer {self.name} is no longer attached to {self.id}")
        return self.streams.append(self.id, type_, body, by=self.name)

    async def inputs(self) -> AsyncIterator[Event]:
        """Input events not yet taken, then new ones as they arrive. An event
        counts as taken once the loop asks for the next one, so input is
        never lost if the producer stops mid-event. Ends on detach or close."""
        s = self.streams
        while self.live:
            changed = s._watch(self.id)
            meta = s.get(self.id)
            out = s._read_now(self.id, meta.input_seq, 50, ("input",))
            for ev in out["events"]:
                yield ev
                store = s._store(self.id)
                meta = store.get(self.id)
                meta.input_seq = ev.seq
                store.save(meta)
            if out["events"]:
                continue
            if out["closed"]:
                return
            await changed.wait()

    def detach(self) -> None:
        """Let go; the stream stays open and keeps collecting input."""
        if self.live:
            del self.streams._producers[self.id]
            self.streams._notify(self.id)

    def close(self, **status) -> Meta:
        return self.streams.close(self.id, by=self.name, **status)


def cap_stream(streams: Streams, block: dict, *, producer: str, kind: str = "process",
               title: str = "", **meta) -> Producer:
    """Open the stream a cap's manifest declares (``stream: {types, input,
    durable}``) and attach the cap's code as its producer. The cap returns
    ``handle.result()`` (``{"stream": id}``) and keeps writing through the
    handle. ``ctx.stream(...)`` (rook.kit.ctx) calls this with the cap's
    block and the host's :class:`Streams`."""
    spec = StreamSpec.from_manifest(block)
    m = streams.open(kind, spec=spec, title=title, producer=producer, **meta)
    return streams.attach(m.id, producer)
