"""Streams (S1 to S5): what stays open on a worker, passed on to everyone attached.

A call on a stream cap answers `stream_open` (S1). The hub then records the
stream as a hub object with a GUID and a moniker (G1), with the worker's title
and description (S3) and who opened it. Agents name streams by moniker or GUID,
never by the worker's own handle.

The worker owns the stream (S2). Everything that passes through it, output from
the worker and input from clients (with who sent it), is kept in order in the
stream's recording, numbered from 0. Clients pull: a client reads from any
position, and may wait a while for more. Any number of clients read at once,
each from its own position, so a late or returning client misses nothing.

A stream outlives whoever opened it (S2) and never times out (S4). It closes
only when the worker closes it or a client closes it on purpose. A closed
stream can still be read. Closing is journaled with who closed it (L1), and the
hub emits `stream.open` and `stream.close` (J2).

A call that is still running when its client stops waiting (Bake 13 Nov; see
`Hub.call_or_follow`) gets a stream of its own on the called worker, titled with
the cap and the worker, described with the call's why (S3). It stays empty while
the call runs. When the call ends, its reply, as the journal keeps it, is the
last entry, and the hub closes the stream on purpose (S4): the call is over. It
takes no input and no client closes it; it is read like any other.
"""

import asyncio
import base64
import json
import uuid

from rook_protocol import encode

from . import scrub
from .errors import HubError
from .journal import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    guid        TEXT PRIMARY KEY,
    moniker     TEXT NOT NULL UNIQUE,
    worker      TEXT NOT NULL REFERENCES workers(guid),
    handle      TEXT NOT NULL,          -- the worker's own name for it; never shown to agents
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    cap         TEXT NOT NULL,
    user        TEXT,                   -- who opened it
    token       TEXT,
    opened_at   REAL NOT NULL,
    closed_at   REAL,
    closed_by   TEXT                    -- "worker", or the user who closed it
);
CREATE INDEX IF NOT EXISTS streams_worker_handle ON streams (worker, handle);
CREATE TABLE IF NOT EXISTS stream_data (    -- the recording
    stream    TEXT NOT NULL REFERENCES streams(guid),
    position  INTEGER NOT NULL,             -- 0, 1, 2 ... in order, per stream
    at        REAL NOT NULL,
    direction TEXT NOT NULL,                -- out: from the worker; in: to the worker
    user      TEXT,                         -- who sent input
    token     TEXT,
    data      BLOB NOT NULL,
    PRIMARY KEY (stream, position)
);
CREATE TABLE IF NOT EXISTS call_streams (   -- streams that carry the reply of a long call (Bake 13 Nov)
    stream TEXT PRIMARY KEY REFERENCES streams(guid),
    call   TEXT NOT NULL                    -- the call's id
);
"""

MAX_WAIT = 40    # seconds a read may wait for more; well under the minute MCP clients give a call


def _text(data):
    """Bytes as text for an agent: UTF-8, with any byte that is not shown as \\xNN."""
    return data.decode("utf-8", "backslashreplace")


class Streams:
    def __init__(self, hub):
        self.hub = hub
        self._changed = {}   # stream GUID -> an asyncio.Event set when the stream has more, or closes

    # ------------------------------------------------- from the worker's link

    def opened(self, worker, message, actor, cap):
        """A call on `cap` answered stream_open: record the stream. Returns its record."""
        hub, guid = self.hub, str(uuid.uuid4())
        with hub._lock, hub._db:
            hub._db.execute(
                "INSERT INTO streams (guid, moniker, worker, handle, title, description, cap, user, token, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guid, hub._new_moniker(guid), worker, message["stream"], message["title"], message["description"],
                 cap, actor[0], actor[1], hub.clock()))
            record = self._record(self._row(guid))
        hub.events.emit("stream.open", self._event(record, record["opened_by"]), user=actor[0], token=actor[1],
                        worker=worker)
        return record

    def received(self, worker, message):
        """stream_data from a worker: add it to the recording and wake the readers."""
        row = self._open_by_handle(worker, message["stream"])
        if row is not None:
            self._append(row["guid"], "out", base64.b64decode(message["data"]), (None, None))

    def closed_by_worker(self, worker, message):
        row = self._open_by_handle(worker, message["stream"])
        if row is not None:
            self._close(row, "worker", (None, None), None)

    # ------------------------------------------------ a long call's stream

    def for_call(self, call_id, worker, cap, why, actor):
        """A stream to follow call `call_id` on `worker` (a worker's or device's GUID), which is still
        running. Its handle is never sent to any worker, so no worker can write to it or close it."""
        hub, guid = self.hub, str(uuid.uuid4())
        with hub._lock, hub._db:
            hub._db.execute(
                "INSERT INTO streams (guid, moniker, worker, handle, title, description, cap, user, token, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guid, hub._new_moniker(guid), worker, uuid.uuid4().hex, f"{cap} on {hub._moniker(worker)}",
                 scrub.scrub(why, hub.vault), cap, actor[0], actor[1], hub.clock()))
            hub._db.execute("INSERT INTO call_streams (stream, call) VALUES (?, ?)", (guid, call_id))
            record = self._record(self._row(guid))
        hub.events.emit("stream.open", self._event(record, record["opened_by"]), user=actor[0], token=actor[1],
                        worker=worker)
        return record

    def call_ended(self, stream, reply):
        """The call ended: `reply`, already as the journal keeps it, becomes the stream's last entry,
        and the hub closes the stream (S4). A stream already closed is left as it is: nothing is
        ever added after the hub closed it."""
        with self.hub._lock:
            row = self._row(stream)
        if row["closed_at"] is not None:
            return
        self._append(stream, "out", json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                     (None, None))
        self._close(row, "hub", (None, None), "The call ended; its reply is the stream's last entry.")

    def cut_short(self, reply_for):
        """At start: end each long call's stream still open, with `reply_for(call id)` as its last entry."""
        with self.hub._lock:
            rows = self.hub._db.execute("SELECT c.stream, c.call FROM call_streams c JOIN streams s ON s.guid = c.stream "
                                        "WHERE s.closed_at IS NULL").fetchall()
        for row in rows:
            self.call_ended(row["stream"], reply_for(row["call"]))

    def _for_call(self, row):
        """Refuse input and closing on a long call's stream: the hub ends it when the call ends."""
        with self.hub._lock:
            follows = self.hub._db.execute("SELECT 1 FROM call_streams WHERE stream = ?", (row["guid"],)).fetchone()
        if follows:
            raise HubError("call_stream", f"Stream {row['moniker']} carries the reply of a call; it takes no input "
                                          "and closes itself when the call ends.",
                           f"Read stream {row['moniker']} with a wait until it is no longer open; its last entry "
                           "is the call's reply.")

    def _open_by_handle(self, worker, handle):
        with self.hub._lock:
            return self.hub._db.execute("SELECT * FROM streams WHERE worker = ? AND handle = ? AND closed_at IS NULL",
                                        (worker, handle)).fetchone()

    def _append(self, guid, direction, data, actor):
        hub = self.hub
        with hub._lock, hub._db:
            position = hub._db.execute("SELECT coalesce(max(position) + 1, 0) FROM stream_data WHERE stream = ?",
                                       (guid,)).fetchone()[0]
            hub._db.execute("INSERT INTO stream_data (stream, position, at, direction, user, token, data) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (guid, position, hub.clock(), direction, *actor, data))
        self._wake(guid)

    def _wake(self, guid):
        event = self._changed.pop(guid, None)
        if event is not None:
            event.set()

    def _close(self, row, closed_by, actor, why):
        hub = self.hub
        with hub._lock, hub._db:
            hub._db.execute("UPDATE streams SET closed_at = ?, closed_by = ? WHERE guid = ?",
                            (hub.clock(), closed_by, row["guid"]))
            hub._journal("stream", actor, row["worker"], {"closed": row["guid"], "title": row["title"],
                                                          "closed_by": closed_by}, why=why)
            record = self._record(self._row(row["guid"]))
        hub.events.emit("stream.close", self._event(record, record["closed_by"]), user=actor[0], token=actor[1],
                        worker=row["worker"])
        self._wake(row["guid"])

    # ---------------------------------------------------------- records

    def _row(self, stream):
        """A stream's row by GUID or moniker (G1). The lock is held."""
        row = self.hub._db.execute("SELECT * FROM streams WHERE guid = ? OR moniker = ?", (stream, stream)).fetchone()
        if row is None:
            raise HubError("unknown_stream", f"No stream is called {stream!r}.",
                           "List the streams and use a moniker or GUID from there.")
        return row

    def _who(self, user):
        return None if user is None else self.hub._moniker(user)

    def _record(self, row):
        hub = self.hub
        return {"guid": row["guid"], "moniker": row["moniker"], "title": row["title"],
                "description": row["description"], "worker": hub._moniker(row["worker"]), "cap": row["cap"],
                "opened_by": self._who(row["user"]), "opened_at": iso(row["opened_at"]),
                "open": row["closed_at"] is None,
                "closed_by": row["closed_by"] if row["closed_by"] in (None, "worker", "hub") else self._who(row["closed_by"]),
                "closed_at": iso(row["closed_at"])}

    def _event(self, record, by):
        return {"stream": record["guid"], "stream_moniker": record["moniker"], "title": record["title"],
                "worker_moniker": record["worker"], "by": by}

    def list(self, open=None):
        """Every stream, newest first, with its title and description (S5); `open` narrows."""
        with self.hub._lock:
            rows = self.hub._db.execute("SELECT * FROM streams ORDER BY opened_at DESC, rowid DESC").fetchall()
            records = [self._record(row) for row in rows]
        return [r for r in records if open is None or r["open"] == open]

    def get(self, stream):
        with self.hub._lock:
            return self._record(self._row(stream))

    # ------------------------------------------------------ for clients

    async def read(self, stream, position=0, wait=0, limit=200, raw=False):
        """Entries from `position` on (at most `limit`), waiting up to `wait` seconds
        for one if there is none yet and the stream is open. Says where to read next.
        Agents get each entry as text (`out` or `in`). With `raw`, for a terminal, each
        entry's bytes come exactly as recorded, base64 in `data`, with its `direction`."""
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise HubError("bad_message", f"wait {wait!r} is not a number of seconds.",
                           f"Give wait as a number from 0 to {MAX_WAIT}, or leave it out.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise HubError("bad_message", f"limit {limit!r} is not a whole number of entries.",
                           "Give limit as a whole number of 1 or more, or leave it out.")
        wait = min(wait, MAX_WAIT)
        while True:
            with self.hub._lock:
                row = self._row(stream)
                end = self.hub._db.execute("SELECT coalesce(max(position) + 1, 0) FROM stream_data WHERE stream = ?",
                                           (row["guid"],)).fetchone()[0]
                if not isinstance(position, int) or position < 0 or position > end:
                    raise HubError("bad_position", f"Stream {row['moniker']} has positions 0 to {end}; "
                                   f"{position!r} is not one of them.", f"Read from a position between 0 and {end}.")
                rows = self.hub._db.execute(
                    "SELECT * FROM stream_data WHERE stream = ? AND position >= ? ORDER BY position LIMIT ?",
                    (row["guid"], position, limit)).fetchall()
            if rows or row["closed_at"] is not None or wait <= 0:
                break
            changed = self._changed.setdefault(row["guid"], asyncio.Event())
            try:
                await asyncio.wait_for(changed.wait(), wait)
            except TimeoutError:
                break
            wait = 0   # one wait per read
        if raw:
            entries = [{"position": r["position"], "direction": r["direction"],
                        "data": base64.b64encode(r["data"]).decode("ascii"),
                        **({"from": self._who(r["user"])} if r["direction"] == "in" else {})} for r in rows]
        else:
            entries = [{"position": r["position"], r["direction"]: _text(r["data"]),
                        **({"from": self._who(r["user"])} if r["direction"] == "in" else {})} for r in rows]
        return {"stream": row["moniker"], "open": row["closed_at"] is None, "entries": entries,
                "next": rows[-1]["position"] + 1 if rows else position}

    async def write(self, stream, text, *, user=None, token=None):
        """Send input to the worker, recorded with who sent it."""
        with self.hub._lock, self.hub._db:
            actor = self.hub.auth.actor(user, token)
            row = self._row(stream)
        if row["closed_at"] is not None:
            raise HubError("stream_closed", f"Stream {row['moniker']} is closed.",
                           "Read it to see what it did; open a new stream to go on.")
        self._for_call(row)
        link = self.hub._links.get(row["worker"])
        if link is None:
            raise HubError("worker_offline", f"The worker of stream {row['moniker']} is not connected.",
                           "Wait for the worker to come back online, then write again.")
        data = text.encode("utf-8")
        await link.send(encode({"type": "stream_data", "stream": row["handle"],
                                "data": base64.b64encode(data).decode("ascii")}))
        self._append(row["guid"], "in", data, actor)
        return {"written": len(data)}

    async def close(self, stream, why, *, user=None, token=None):
        """Close a stream on purpose: tell its worker, record who closed it."""
        with self.hub._lock, self.hub._db:
            actor = self.hub.auth.actor(user, token)
            row = self._row(stream)
        if row["closed_at"] is not None:
            raise HubError("stream_closed", f"Stream {row['moniker']} is already closed.", "Nothing to do.")
        self._for_call(row)
        link = self.hub._links.get(row["worker"])
        if link is None:
            raise HubError("worker_offline", f"The worker of stream {row['moniker']} is not connected.",
                           "Wait for the worker to come back online, then close it.")
        await link.send(encode({"type": "stream_close", "stream": row["handle"]}))
        self._close(row, actor[0], actor, why)
        return {"closed": row["moniker"]}
