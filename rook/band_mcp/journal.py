"""Call journal — a persistent record of every band call fired through the MCP.

The problem it solves: an agent fires ``rook_call``, the cap runs long or the
call times out, and the output — which may still be landing — falls into the
ether the moment the tool result is discarded. The journal captures every
call's full reply server-side into a size-capped sqlite ring, keyed by call id,
worker, cap, caller identity, and (once chat rooms exist) thread id. An agent
that lost the thread of what it was doing can query it back.

sqlite (WAL) rather than JSONL because the useful queries are "what did worker
X do", "what happened since time T", "show me call <id>" — indexable lookups,
not a full-file scan. The ring is pruned by row count on write.

This is server-side (band_mcp), so it captures every MCP-originated call
without any worker-side change or OTA. Workers keep their own local audit trail
(:mod:`rook.worker.audit`) of who called them; the journal is the caller-side
record of what came back.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("rook.band_mcp.journal")

_MAX_ROWS = 20000          # prune oldest beyond this on write
_PRUNE_EVERY = 200         # only run the prune sweep every N inserts (cheap amortization)


class Journal:
    """A size-capped, queryable log of band calls. Thread-safe; all access is
    serialized through one lock over a single connection (sqlite from asyncio's
    default executor threads, so we can't rely on one-thread affinity)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._writes_since_prune = 0
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            log.warning("journal dir %s not writable; journaling disabled", d)
            self._db = None
            return
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id    TEXT,
                    ts         REAL,
                    identity   TEXT,
                    cap        TEXT,
                    worker     TEXT,
                    thread_id  TEXT,
                    ok         INTEGER,
                    error      TEXT,
                    reply      TEXT
                )
            """)
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_calls_worker ON calls(worker)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_calls_thread ON calls(thread_id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_calls_callid ON calls(call_id)")
            self._db.commit()
        except Exception:
            log.exception("journal init failed; journaling disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

    def record(self, *, cap: str, worker: str | None, identity: str | None,
               args: dict | None, reply: dict | None, thread_id: str | None = None,
               call_id: str | None = None) -> str:
        """Store one call + its reply. Returns the journal call id (generated if
        not supplied). Best-effort — never raises into the call path."""
        cid = call_id or (reply or {}).get("id") or uuid.uuid4().hex
        if self._db is None:
            return cid
        ok = 1 if (reply or {}).get("ok") else 0
        error = None if ok else str((reply or {}).get("error", ""))[:1000]
        # Store the full reply (it's the whole point — the output that would
        # otherwise be lost), but bound it so a giant base64 blob can't bloat
        # the ring without limit.
        try:
            blob = json.dumps(reply, separators=(",", ":")) if reply is not None else None
            if blob is not None and len(blob) > 200_000:
                blob = blob[:200_000] + "…<truncated>"
        except Exception:
            blob = "<unserializable reply>"
        row = (cid, round(time.time(), 3), identity or "anonymous", cap,
               worker, thread_id, ok, error, blob)
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO calls (call_id, ts, identity, cap, worker, "
                    "thread_id, ok, error, reply) VALUES (?,?,?,?,?,?,?,?,?)", row)
                self._writes_since_prune += 1
                if self._writes_since_prune >= _PRUNE_EVERY:
                    self._prune_locked()
                    self._writes_since_prune = 0
                self._db.commit()
        except Exception:
            log.debug("journal record failed", exc_info=True)
        return cid

    def _prune_locked(self) -> None:
        """Drop rows beyond the newest _MAX_ROWS. Called under _lock."""
        try:
            cur = self._db.execute("SELECT COUNT(*) FROM calls")
            (n,) = cur.fetchone()
            if n > _MAX_ROWS:
                self._db.execute(
                    "DELETE FROM calls WHERE seq <= "
                    "(SELECT seq FROM calls ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                    (_MAX_ROWS,))
        except Exception:
            log.debug("journal prune failed", exc_info=True)

    def query(self, *, worker: str | None = None, cap_prefix: str | None = None,
              identity: str | None = None, thread_id: str | None = None,
              call_id: str | None = None, since: float | None = None,
              ok: bool | None = None, limit: int = 50,
              include_reply: bool = False) -> list[dict]:
        """Return matching journal entries, newest first. ``include_reply``
        attaches the full stored reply (omitted by default to keep listings
        light — fetch a single call_id with include_reply=True to see output)."""
        if self._db is None:
            return []
        clauses, params = [], []
        if worker:
            clauses.append("worker = ?"); params.append(worker)
        if cap_prefix:
            clauses.append("cap LIKE ?"); params.append(cap_prefix + "%")
        if identity:
            clauses.append("identity = ?"); params.append(identity)
        if thread_id:
            clauses.append("thread_id = ?"); params.append(thread_id)
        if call_id:
            clauses.append("call_id = ?"); params.append(call_id)
        if since is not None:
            clauses.append("ts >= ?"); params.append(float(since))
        if ok is not None:
            clauses.append("ok = ?"); params.append(1 if ok else 0)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT call_id, ts, identity, cap, worker, thread_id, ok, "
               f"error, reply FROM calls {where} ORDER BY seq DESC LIMIT ?")
        params.append(max(1, min(int(limit), 500)))
        try:
            with self._lock:
                rows = self._db.execute(sql, params).fetchall()
        except Exception:
            log.debug("journal query failed", exc_info=True)
            return []
        out = []
        for (cid, ts, ident, cap, worker_, thread, ok_, error, reply) in rows:
            e = {"call_id": cid, "ts": ts, "identity": ident, "cap": cap,
                 "worker": worker_, "thread_id": thread, "ok": bool(ok_)}
            if error:
                e["error"] = error
            if include_reply and reply is not None:
                try:
                    e["reply"] = json.loads(reply)
                except Exception:
                    e["reply"] = reply
            out.append(e)
        return out

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
