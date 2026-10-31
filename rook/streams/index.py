"""The hub's index of every stream on the band (castling-streams, N28).

Streams live where their producer runs: a worker keeps its terminals and
processes, hub plugins (agents, speech) their sessions. The hub keeps one
index of them all, so the sessions list, the desktop's Sessions tab, history
importers and the dashboard can find a stream without asking every host.

* :class:`Reporter` turns one :class:`~rook.streams.Streams`' changes into
  index rows: opening, closing, retitling and deleting at once, activity
  (appends) at most every ``every`` seconds per stream.
* :class:`StreamIndex` keeps the rows in SQLite, one per stream id, each
  under the host that reported it (a worker id, or ``hub``). A host
  only ever changes its own rows.

On a worker the reporter's rows travel to the hub as ``stream.*`` events
(builtin/stream.yaml ``emits``, spec 13 ``core.event``); the hub's own
store (``ctx.streams``, every hub plugin's) reports straight to the index.
Who may see a row is :func:`visible`'s rule, the same as a worker's for
reading the stream: owners see every stream; a person the ones they take
part in; an agent alone the ones it takes part in that have no person.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from typing import Callable

from .events import StreamError, check_kind

log = logging.getLogger("rook.streams.index")

ACTIVITY_EVERY = 5.0          # seconds between activity reports for one stream
MAX_PAGE = 200
SID = re.compile(r"^s_[0-9a-f]{16}$")

Report = Callable[[str, dict], None]      # report("changed" | "deleted", row)


class Reporter:
    """Watch ``streams`` (it takes its ``on_change``) and ``report`` rows."""

    def __init__(self, streams, report: Report, every: float = ACTIVITY_EVERY) -> None:
        self.streams, self.report, self.every = streams, report, every
        self._dirty: set[str] = set()
        self._timer: asyncio.TimerHandle | None = None
        streams.on_change = self._changed

    def rows(self) -> list[dict]:
        """Every stream it watches, as rows (a full resync)."""
        return [self.streams.summary(m.id) for m in self.streams.list()]

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.streams.on_change == self._changed:
            self.streams.on_change = None

    def _changed(self, sid: str, change: str) -> None:
        if change != "append":
            self._dirty.discard(sid)
            self.report("deleted" if change == "delete" else "changed", self.streams.summary(sid))
            return
        self._dirty.add(sid)
        if self._timer is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:           # no loop (a sync caller): report now
                return self.flush()
            self._timer = loop.call_later(self.every, self.flush)

    def flush(self) -> None:
        self._timer = None
        dirty, self._dirty = self._dirty, set()
        for sid in dirty:
            try:
                self.report("changed", self.streams.summary(sid))
            except StreamError:            # deleted since (that was reported)
                continue
            except Exception:              # the index is a view: it never breaks a stream
                log.exception("stream activity not reported", extra={"fields": {"stream": sid}})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    worker TEXT NOT NULL,
    plugin TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    attrs TEXT NOT NULL DEFAULT '{}',
    types TEXT NOT NULL DEFAULT '[]',
    input INTEGER NOT NULL DEFAULT 0,
    durable INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    closed REAL,
    last_at REAL NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS streams_host ON streams(host);
CREATE INDEX IF NOT EXISTS streams_last ON streams(last_at DESC, id);
"""
_COLS = ("id, host, worker, plugin, kind, title, producer, participants, attrs, types, input, durable,"
         " created, closed, last_at, last_seq, size")


def visible(viewer: tuple | None, participants: list) -> bool:
    """``viewer`` is None (an owner: everything), ``("person", p)`` or
    ``("agent", a)`` (acting alone)."""
    if viewer is None:
        return True
    kind, who = viewer
    if kind == "person":
        return who in participants
    return who in participants and not any(str(p).startswith("person:") for p in participants)


def _viewer_sql(viewer: tuple | None) -> tuple[str, list]:
    if viewer is None:
        return "1", []
    has = "EXISTS (SELECT 1 FROM json_each(participants) WHERE value = ?)"
    if viewer[0] == "person":
        return has, [viewer[1]]
    return has + " AND NOT EXISTS (SELECT 1 FROM json_each(participants) WHERE value LIKE 'person:%')", [viewer[1]]


def _num(v, default=0.0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


class StreamIndex:
    """The rows, in one SQLite file."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def put(self, host: str, worker: str, row: dict, plugin: str | None = None) -> bool:
        """Add or replace ``row`` (a :meth:`Streams.summary`) under ``host``.
        False when it is malformed or the id is another host's."""
        sid = row.get("id")
        try:
            if not (isinstance(sid, str) and SID.match(sid)):
                return False
            check_kind(row.get("kind"))
        except StreamError:
            return False
        participants = [p[:200] for p in row.get("participants") or () if isinstance(p, str)][:100]
        attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
        types = [t for t in row.get("types") or () if isinstance(t, str)][:8]
        created = _num(row.get("created"))
        closed = row.get("closed")
        values = (sid, host, worker, plugin, row["kind"], str(row.get("title") or "")[:200],
                  str(row.get("producer") or "")[:200], json.dumps(participants), json.dumps(attrs),
                  json.dumps(types), int(bool(row.get("input"))), int(bool(row.get("durable"))), created,
                  _num(closed) if closed is not None else None, _num(row.get("last_at"), created),
                  int(_num(row.get("last_seq"), 0)), int(_num(row.get("size"), 0)))
        with self._lock:
            have = self._db.execute("SELECT host FROM streams WHERE id=?", (sid,)).fetchone()
            if have is not None and have[0] != host:
                return False
            self._db.execute(f"INSERT OR REPLACE INTO streams ({_COLS}) VALUES ({','.join('?' * 17)})", values)
        return True

    def drop(self, host: str, sid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM streams WHERE id=? AND host=?", (sid, host))

    def sync(self, host: str, ids) -> int:
        """``host`` holds exactly ``ids`` now: forget its other rows. How many went."""
        keep = set(ids)
        with self._lock:
            have = [r[0] for r in self._db.execute("SELECT id FROM streams WHERE host=?", (host,))]
            gone = [sid for sid in have if sid not in keep]
            self._db.executemany("DELETE FROM streams WHERE id=? AND host=?", [(sid, host) for sid in gone])
        return len(gone)

    def get(self, sid: str, viewer: tuple | None = None) -> dict | None:
        with self._lock:
            r = self._db.execute(f"SELECT {_COLS} FROM streams WHERE id=?", (sid,)).fetchone()
        row = _row(r) if r else None
        return row if row and visible(viewer, row["participants"]) else None

    def query(self, viewer: tuple | None = None, *, kind: str | None = None, worker: str | None = None,
              participant: str | None = None, open_only: bool = False, text: str | None = None,
              limit: int = 50, offset: int = 0) -> tuple[list[dict], int | None]:
        """Rows ``viewer`` may see, latest activity first: ``(rows, next)``;
        pass ``next`` back as ``offset`` (None: no more). ``text`` matches
        title, producer, kind, participants and attrs (case-insensitive)."""
        where, args = _viewer_sql(viewer)
        where = [where]
        for col, v in (("kind", kind), ("worker", worker)):
            if v:
                where.append(f"{col} = ?")
                args.append(v)
        if participant:
            where.append("EXISTS (SELECT 1 FROM json_each(participants) WHERE value = ?)")
            args.append(participant)
        if open_only:
            where.append("closed IS NULL")
        if text:
            like = "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("LOWER(title || ' ' || producer || ' ' || kind || ' ' || participants || ' ' || attrs)"
                         " LIKE ? ESCAPE '\\'")
            args.append(like)
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM streams WHERE {' AND '.join(where)}"
                                    " ORDER BY last_at DESC, id LIMIT ? OFFSET ?",
                                    (*args, limit + 1, offset)).fetchall()
        more = len(rows) > limit
        return [_row(r) for r in rows[:limit]], (offset + limit if more else None)


def _row(r) -> dict:
    participants = json.loads(r[7])
    out = {"id": r[0], "worker": r[2], "kind": r[4], "title": r[5], "producer": r[6],
           "participants": participants,
           "person": next((p for p in participants if p.startswith("person:")), None),
           "agent": next((p for p in participants if p.startswith(("agent:", "system:"))), None),
           "attrs": json.loads(r[8]), "types": json.loads(r[9]), "input": bool(r[10]), "durable": bool(r[11]),
           "created": r[12], "closed": r[13], "last_at": r[14], "last_seq": r[15], "size": r[16]}
    if r[3]:
        out["plugin"] = r[3]
    return out
