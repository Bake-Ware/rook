"""The hub's stream index (castling-streams, N28) and its caps.

    hub.streams = HubStreams(hub)      # at start, after the builtin caps
    hub.streams.take(known, event, payload)       # a worker's stream.* event
    hub.streams.watch("agents", streams)          # a hub plugin's own Streams

Rows come from two places. Workers send ``stream.changed`` /
``stream.deleted`` / ``stream.synced`` events (builtin/stream.yaml, spec 13
``core.event``), which :meth:`HubStreams.take` files under the worker's id;
hub plugins hand their own :class:`~rook.streams.Streams` over with
``ctx.index_streams(streams)``, filed under ``hub:<plugin>``. Either way a
host changes only its own rows (:class:`~rook.streams.StreamIndex`).

``stream.list`` / ``stream.get`` / ``stream.search`` (builtin/stream) read
the index under the caller's rights: owners see every stream, a person the
ones they take part in, an agent acting alone the ones it takes part in that
have no person (the rule a worker applies to reading the stream itself).
"""
from __future__ import annotations

import logging
from pathlib import Path

from rook.kit.ctx import KitError
from rook.streams import Reporter, StreamIndex

log = logging.getLogger("rook.hub.streams")

BUILTIN = Path(__file__).with_name("builtin") / "stream"
EVENTS = ("stream.changed", "stream.deleted", "stream.synced")


class HubStreams:
    def __init__(self, hub):
        self.hub = hub
        self.index = StreamIndex(str(Path(hub.data) / "streams.db"))
        self._reporters: dict[str, Reporter] = {}
        hub.plugins.add_builtin(BUILTIN, {"stream.list": self.list, "stream.get": self.get,
                                          "stream.search": self.search})

    def close(self) -> None:
        for r in self._reporters.values():
            r.close()
        self._reporters.clear()
        self.index.close()

    # -- feeding it ------------------------------------------------------------------

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

    def watch(self, plugin: str, streams) -> None:
        """``ctx.index_streams``: list ``plugin``'s own streams (on the hub)."""
        old = self._reporters.pop(plugin, None)
        if old is not None:
            old.close()
        host = f"hub:{plugin}"

        def report(change: str, row: dict) -> None:
            if change == "deleted":
                self.index.drop(host, row["id"])
            elif not self.index.put(host, "rook", row, plugin=plugin):
                log.warning("stream row refused", extra={"fields": {"plugin": plugin, "stream": row["id"]}})

        r = self._reporters[plugin] = Reporter(streams, report)
        rows = r.rows()
        for row in rows:
            report("changed", row)
        self.index.sync(host, [row["id"] for row in rows])

    # -- the caps --------------------------------------------------------------------

    def _viewer(self, ctx) -> tuple | None:
        c = ctx.caller
        if c.owner:
            return None
        if c.person:
            return ("person", c.person)
        if c.agent:
            return ("agent", c.agent)
        raise KitError("streams are listed for a person or an agent")

    def _page(self, ctx, text=None, kind=None, worker=None, participant=None, open=False, limit=50,
              offset=0) -> dict:
        rows, nxt = self.index.query(self._viewer(ctx), kind=kind, worker=worker, participant=participant,
                                     open_only=open, text=text, limit=limit, offset=offset)
        return {"streams": [self._online(r) for r in rows], "next": nxt}

    def _online(self, row: dict) -> dict:
        if row.get("plugin"):
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
