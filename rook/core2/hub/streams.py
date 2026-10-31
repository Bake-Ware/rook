"""The hub's streams (castling-streams): one durable store, the index, the caps.

    hub.streams = HubStreams(hub)      # before the plugins: they share .streams
    hub.streams.install()              # the builtin stream caps, with the others
    hub.streams.take(known, event, payload)       # a worker's stream.* event

**One store.** The hub keeps every stream that lives on it (chat rooms,
huddles, agent and voice sessions) in one durable
:class:`~rook.streams.Streams` (``<hub data>/stream-events.db``). Hub plugins
get it as ``ctx.streams`` and open their streams there with
``attrs.plugin`` naming themselves; nobody keeps a private stream store.

**One index.** Rows come from two places. Workers send ``stream.changed`` /
``stream.deleted`` / ``stream.synced`` events (builtin/stream.yaml, spec 13
``core.event``), which :meth:`HubStreams.take` files under the worker's id;
the hub's own store reports straight in, under ``hub``. Either way a host
changes only its own rows (:class:`~rook.streams.StreamIndex`).

**The caps** (builtin/stream, on worker ``rook``): ``stream.list`` /
``stream.get`` / ``stream.search`` read the index; ``stream.open`` (a chat
room), ``stream.read`` (by cursor, long-poll: that is how you follow a
stream), ``stream.write`` and ``stream.close`` act on the hub's own streams.
A terminal or process lives on its worker: read it there. Rights are the
index's (and a worker's) rule: owners see every stream, a person the ones
they take part in, an agent acting alone the ones it takes part in that have
no person.
"""
from __future__ import annotations

import logging
from pathlib import Path

from rook.kit.ctx import KitError
from rook.streams import Reporter, SqliteStore, StreamError, StreamIndex, Streams
from rook.streams.index import visible

log = logging.getLogger("rook.hub.streams")

BUILTIN = Path(__file__).with_name("builtin") / "stream"
EVENTS = ("stream.changed", "stream.deleted", "stream.synced")
HOST = "hub"                       # the index's host for the hub's own streams


class HubStreams:
    def __init__(self, hub):
        self.hub = hub
        data = Path(hub.data)
        self.index = StreamIndex(str(data / "streams.db"))
        self.streams = Streams(SqliteStore(str(data / "stream-events.db")), host="hub")
        self._reporter = Reporter(self.streams, self._report)
        rows = self._reporter.rows()
        for row in rows:
            self._report("changed", row)
        self.index.sync(HOST, [row["id"] for row in rows])

    def install(self) -> None:
        self.hub.plugins.add_builtin(BUILTIN, {
            "stream.list": self.list, "stream.get": self.get, "stream.search": self.search,
            "stream.open": self.open, "stream.read": self.read, "stream.write": self.write,
            "stream.close": self.close_stream})

    def close(self) -> None:
        self._reporter.close()
        self.index.close()
        self.streams.durable.close()

    # -- feeding the index -------------------------------------------------------------

    def take(self, k, event: str, payload: dict) -> None:
        """One of worker ``k``'s stream events (already checked against its
        manifest by ``core.event``). A stream must be of the band ``k`` is on."""
        if event == "stream.changed":
            if (payload.get("attrs") or {}).get("band") != k.band:
                log.warning("stream of another band refused", extra={"fields": {"worker": k.name}})
            elif not self.index.put(k.id, k.name, payload):
                log.warning("stream row refused", extra={"fields": {"worker": k.name,
                                                                    "stream": str(payload.get("id"))[:20]}})
        elif event == "stream.deleted":
            self.index.drop(k.id, payload["id"])
        elif event == "stream.synced" and payload.get("complete"):
            self.index.sync(k.id, payload.get("ids") or ())

    def _report(self, change: str, row: dict) -> None:
        if change == "deleted":
            self.index.drop(HOST, row["id"])
            return
        try:
            plugin = self.streams.get(row["id"]).attrs.get("plugin")
        except StreamError:
            plugin = None
        if not self.index.put(HOST, "rook", row, plugin=plugin if isinstance(plugin, str) else None):
            log.warning("stream row refused", extra={"fields": {"stream": row["id"]}})

    # -- rights ------------------------------------------------------------------------

    def _viewer(self, ctx) -> tuple | None:
        c = ctx.caller
        if c.owner:
            return None
        if c.person:
            return ("person", c.person)
        if c.agent:
            return ("agent", c.agent)
        raise KitError("streams are for a person or an agent")

    def _mine(self, ctx, sid: str):
        """The hub stream ``sid`` if the caller may see it."""
        try:
            meta = self.streams.get(sid)
        except StreamError:
            meta = None
        if meta is None:
            row = self.index.get(sid, self._viewer(ctx))
            if row is not None:
                raise KitError(f"stream {sid} is on worker {row['worker']}: call stream.* there")
            raise KitError(f"no stream {sid} you can see")
        if not visible(self._viewer(ctx), meta.participants):
            raise KitError(f"no stream {sid} you can see")
        return meta

    @staticmethod
    def _plugin_owned(meta, verb: str) -> None:
        plugin = meta.attrs.get("plugin")
        if plugin:
            raise KitError(f"stream {meta.id} is {plugin}'s ({meta.kind}): {verb} it through {plugin}'s caps")

    # -- the caps: the index -----------------------------------------------------------

    def _page(self, ctx, text=None, kind=None, worker=None, participant=None, open=False, limit=50,
              offset=0) -> dict:
        rows, nxt = self.index.query(self._viewer(ctx), kind=kind, worker=worker, participant=participant,
                                     open_only=open, text=text, limit=limit, offset=offset)
        return {"streams": [self._online(r) for r in rows], "next": nxt}

    def _online(self, row: dict) -> dict:
        if row["worker"] == "rook":
            row["online"] = True
        else:
            k = next((k for k in self.hub.known.values() if k.name == row["worker"]), None)
            row["online"] = bool(k and k.online)
        return row

    async def list(self, ctx, kind=None, worker=None, participant=None, open=False, limit=50, offset=0) -> dict:
        return self._page(ctx, None, kind, worker, participant, open, limit, offset)

    async def search(self, ctx, query, kind=None, worker=None, open=False, limit=50, offset=0) -> dict:
        return self._page(ctx, query, kind, worker, None, open, limit, offset)

    async def get(self, ctx, stream) -> dict:
        row = self.index.get(stream, self._viewer(ctx))
        if row is None:
            raise KitError(f"no stream {stream} you can see")
        return self._online(row)

    # -- the caps: the hub's own streams -----------------------------------------------

    async def open(self, ctx, title, participants=(), kind="room") -> dict:
        """A chat room: ``text`` events from its participants (castling-streams)."""
        c = ctx.caller
        me = [p for p in (c.person, c.agent) if p]
        if not me:
            raise KitError("a room is opened by a person or an agent")
        title = " ".join(str(title).split())[:200]
        if not title:
            raise KitError("bad args: title names the room")
        meta = self.streams.open(kind, title=title, producer="", participants=[*me, *participants],
                                 attrs={"by": c.actor, **({"band": c.band} if c.band else {})})
        return {"stream": meta.id, "kind": meta.kind, "title": meta.title, "participants": meta.participants}

    async def read(self, ctx, stream, after=0, limit=200, wait=0.0, types=None) -> dict:
        self._mine(ctx, stream)
        try:
            out = await self.streams.read(stream, after, limit, types, wait)
        except StreamError as e:
            raise KitError(str(e)) from None
        return {**out, "events": [ev.wire() for ev in out["events"]]}

    async def write(self, ctx, stream, body, type="text") -> dict:
        meta = self._mine(ctx, stream)
        self._plugin_owned(meta, "write to")
        body = {"text": body["text"], **({"mentions": list(body["mentions"])} if body.get("mentions") else {})}
        try:
            ev = self.streams.append(stream, type, body, by=ctx.caller.actor)
        except StreamError as e:
            raise KitError(str(e)) from None
        return {"stream": stream, "seq": ev.seq}

    async def close_stream(self, ctx, stream) -> dict:
        meta = self._mine(ctx, stream)
        self._plugin_owned(meta, "close")
        if not (ctx.caller.owner or meta.attrs.get("by") == ctx.caller.actor):
            raise KitError("only whoever opened a room (or an owner) closes it")
        meta = self.streams.close(stream, by=ctx.caller.actor)
        return {"stream": stream, "closed": meta.closed}
