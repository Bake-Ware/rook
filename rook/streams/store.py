"""Where stream events live.

* :class:`MemoryStore`: bounded. Each stream keeps its newest events up to a
  byte budget; a reader that fell behind is told how many events it missed.
  Terminals and processes, on the worker that runs them.
* :class:`SqliteStore`: durable. Every event is kept until the stream is
  deleted. Rooms, huddles and agent sessions, on the hub.

Both answer the same calls; :class:`rook.streams.Streams` picks one per
stream from its spec and does the waiting, checking and lifecycle. Stores
are synchronous and called from the event loop thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import deque

from .events import Event, Meta, StreamError, StreamSpec

DEFAULT_BUDGET = 256 * 1024       # bytes per bounded stream (today's terminal scrollback)
MAX_BUDGET = 8 * 1024 * 1024
KEEP_CLOSED = 32                  # finished bounded streams kept for late readers


def _window(last_seq: int, after: int, first: int) -> tuple[int, int]:
    """Clamp a cursor to the oldest event still held: ``(after, dropped)``."""
    after = max(0, min(int(after), last_seq))
    if after < first - 1:
        return first - 1, first - 1 - after
    return after, 0


class MemoryStore:
    """Bounded streams in memory: a ring of events per stream."""

    def __init__(self, keep_closed: int = KEEP_CLOSED) -> None:
        self.keep_closed = keep_closed
        self._meta: dict[str, Meta] = {}
        self._events: dict[str, deque[Event]] = {}
        self._held: dict[str, int] = {}          # bytes held per stream
        self._budget: dict[str, int] = {}

    def __contains__(self, sid: str) -> bool:
        return sid in self._meta

    def create(self, meta: Meta, budget: int = DEFAULT_BUDGET) -> None:
        self._prune_closed()
        self._meta[meta.id] = meta
        self._events[meta.id] = deque()
        self._held[meta.id] = 0
        self._budget[meta.id] = max(4096, min(int(budget), MAX_BUDGET))

    def get(self, sid: str) -> Meta:
        try:
            return self._meta[sid]
        except KeyError:
            raise StreamError(f"no stream {sid}") from None

    def save(self, meta: Meta) -> None:
        self._meta[meta.id] = meta

    def append(self, sid: str, type_: str, by: str, body, at: float) -> Event:
        meta = self.get(sid)
        meta.last_seq += 1
        ev = Event(meta.last_seq, at, type_, by, body)
        ring = self._events[sid]
        ring.append(ev)
        self._held[sid] += ev.size
        while self._held[sid] > self._budget[sid] and len(ring) > 1:
            self._held[sid] -= ring.popleft().size
        return ev

    def read(self, sid: str, after: int, limit: int, types=None) -> tuple[list[Event], int, int]:
        meta = self.get(sid)
        ring = self._events[sid]
        first = ring[0].seq if ring else meta.last_seq + 1
        after, dropped = _window(meta.last_seq, after, first)
        out: list[Event] = []
        for ev in ring:                            # small rings; a scan is fine
            if ev.seq <= after or (types and ev.type not in types):
                continue
            out.append(ev)
            if len(out) >= limit:
                return out, ev.seq, dropped
        return out, meta.last_seq, dropped

    def list(self) -> list[Meta]:
        return list(self._meta.values())

    def delete(self, sid: str) -> None:
        self.get(sid)
        for d in (self._meta, self._events, self._held, self._budget):
            d.pop(sid, None)

    def search(self, query: str, limit: int) -> list[tuple[Meta, Event]]:
        q = query.lower()
        hits = []
        for sid, ring in self._events.items():
            hits += [(self._meta[sid], ev) for ev in ring if q in ev.plain().lower()]
        hits.sort(key=lambda h: h[1].at, reverse=True)
        return hits[:limit]

    def _prune_closed(self) -> None:
        closed = sorted((m for m in self._meta.values() if m.closed), key=lambda m: m.closed)
        for m in closed[:max(0, len(closed) - self.keep_closed)]:
            self.delete(m.id)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    attrs TEXT NOT NULL DEFAULT '{}',
    types TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    closed REAL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    input_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS streams_kind ON streams(kind);
CREATE TABLE IF NOT EXISTS events (
    stream TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at REAL NOT NULL,
    type TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    body BLOB NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (stream, seq)
) WITHOUT ROWID;
"""

_COLS = "id, kind, title, host, producer, participants, attrs, types, input, created, closed, last_seq, input_seq"


class SqliteStore:
    """Durable streams in one SQLite file: nothing is dropped until delete."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __contains__(self, sid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM streams WHERE id=?", (sid,)).fetchone() is not None

    def create(self, meta: Meta, budget: int = 0) -> None:
        with self._lock:
            self._db.execute(f"INSERT INTO streams ({_COLS}) VALUES ({','.join('?' * 13)})",
                             self._row(meta))

    def get(self, sid: str) -> Meta:
        with self._lock:
            row = self._db.execute(f"SELECT {_COLS} FROM streams WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise StreamError(f"no stream {sid}")
        return self._meta(row)

    def save(self, meta: Meta) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE streams SET title=?, producer=?, participants=?, attrs=?, closed=?,"
                " input_seq=? WHERE id=?",
                (meta.title, meta.producer, json.dumps(meta.participants), json.dumps(meta.attrs),
                 meta.closed, meta.input_seq, meta.id))

    def append(self, sid: str, type_: str, by: str, body, at: float) -> Event:
        raw = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT last_seq FROM streams WHERE id=?", (sid,)).fetchone()
                if row is None:
                    raise StreamError(f"no stream {sid}")
                ev = Event(row[0] + 1, at, type_, by, body)
                self._db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                                 (sid, ev.seq, at, type_, by, raw, ev.plain()))
                self._db.execute("UPDATE streams SET last_seq=? WHERE id=?", (ev.seq, sid))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return ev

    def read(self, sid: str, after: int, limit: int, types=None) -> tuple[list[Event], int, int]:
        with self._lock:
            row = self._db.execute("SELECT last_seq FROM streams WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise StreamError(f"no stream {sid}")
            last = row[0]
            after = max(0, min(int(after), last))
            sql = "SELECT seq, at, type, by, body FROM events WHERE stream=? AND seq>? AND seq<=?"
            args: list = [sid, after, last]
            if types:
                sql += f" AND type IN ({','.join('?' * len(types))})"
                args += list(types)
            rows = self._db.execute(sql + " ORDER BY seq LIMIT ?", (*args, limit)).fetchall()
        events = [self._event(r) for r in rows]
        nxt = events[-1].seq if len(events) >= limit else last
        return events, nxt, 0

    def list(self) -> list[Meta]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM streams").fetchall()
        return [self._meta(r) for r in rows]

    def delete(self, sid: str) -> None:
        with self._lock:
            if self._db.execute("DELETE FROM streams WHERE id=?", (sid,)).rowcount == 0:
                raise StreamError(f"no stream {sid}")
            self._db.execute("DELETE FROM events WHERE stream=?", (sid,))

    def search(self, query: str, limit: int) -> list[tuple[Meta, Event]]:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT stream, seq, at, type, by, body FROM events WHERE text LIKE ? ESCAPE '\\'"
                " ORDER BY at DESC LIMIT ?", (like, limit)).fetchall()
        metas: dict[str, Meta] = {}
        hits = []
        for r in rows:
            if r[0] not in metas:
                metas[r[0]] = self.get(r[0])
            hits.append((metas[r[0]], self._event(r[1:])))
        return hits

    @staticmethod
    def _row(m: Meta) -> tuple:
        return (m.id, m.kind, m.title, m.host, m.producer, json.dumps(m.participants),
                json.dumps(m.attrs), json.dumps(list(m.spec.types)), int(m.spec.input),
                m.created, m.closed, m.last_seq, m.input_seq)

    @staticmethod
    def _meta(r) -> Meta:
        spec = StreamSpec(tuple(json.loads(r[7])), bool(r[8]), durable=True)
        return Meta(id=r[0], kind=r[1], spec=spec, title=r[2], host=r[3], producer=r[4],
                    participants=json.loads(r[5]), attrs=json.loads(r[6]), created=r[9],
                    closed=r[10], last_seq=r[11], input_seq=r[12])

    @staticmethod
    def _event(r) -> Event:
        seq, at, type_, by, raw = r
        body = bytes(raw) if type_ == "bytes" else json.loads(raw)
        return Event(seq, at, type_, by, body)
