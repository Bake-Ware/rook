"""SQLite store for agent memory (``memory.db``). No network, no MCP.

One row per memory with its scope, kind, state, confidence, provenance and
supersede edges; an FTS5 mirror for keyword search; float32 unit vectors
per embedding model; the idempotency table for transcript ingest; and an
append-only history of every state change.

Memories are never edited in place: a correction is a new row that
supersedes the old one, and forgetting marks a row ``retracted`` (``purge``
also blanks its text). The only in-place changes are bookkeeping:
confidence reinforcement, recall counters, state transitions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ....core import migrations
from .embedder import pack, unpack

NAMESPACE = "memory"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"
STATES = ("pending", "active", "superseded", "archived", "retracted", "rejected")
LIVE = ("active",)
COLUMNS = ("id", "scope_kind", "scope_id", "kind", "text", "hash", "state", "confidence",
           "reinforced", "recalls", "last_used", "created", "updated", "author", "actor",
           "session", "journal", "task", "source", "supersedes", "superseded_by", "tags",
           "reason", "warnings")
_JSON = ("supersedes", "tags", "warnings")


def new_id() -> str:
    return "m_" + uuid.uuid4().hex[:14]


def fts_query(query: str) -> str:
    """Words of a free-text query as an FTS5 OR query (no operator injection)."""
    words = [w for w in re.findall(r"[\w]+", query.lower()) if len(w) > 1][:24]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        migrations.apply(self._conn, MIGRATIONS, NAMESPACE)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def db(self, write: bool = True):
        with self._lock:
            if write:
                with self._conn:
                    yield self._conn
            else:
                yield self._conn

    # -- rows ------------------------------------------------------------
    @staticmethod
    def row(r) -> dict:
        d = dict(r)
        for k in _JSON:
            if k in d:
                try:
                    d[k] = json.loads(d[k] or "[]")
                except ValueError:
                    d[k] = []
        return d

    def insert(self, rec: dict, actor: str) -> dict:
        now = time.time()
        rec = {**rec}
        rec.setdefault("id", new_id())
        rec.setdefault("created", now)
        rec.setdefault("updated", now)
        rec.setdefault("last_used", None)
        for k in _JSON:
            rec[k] = json.dumps(rec.get(k) or [], separators=(",", ":"))
        cols = [c for c in COLUMNS if c in rec]
        with self.db() as db:
            db.execute(f"INSERT INTO memories({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       [rec[c] for c in cols])
            self._history(db, rec["id"], "create", actor,
                          {"state": rec["state"], "confidence": rec["confidence"]})
        return self.get(rec["id"])

    def get(self, mid: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return self.row(r) if r else None

    def update(self, mid: str, actor: str, action: str, **fields) -> None:
        fields["updated"] = time.time()
        for k in _JSON:
            if k in fields:
                fields[k] = json.dumps(fields[k] or [], separators=(",", ":"))
        with self.db() as db:
            db.execute(f"UPDATE memories SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       [*fields.values(), mid])
            data = {k: v for k, v in fields.items() if k in ("state", "reason", "confidence",
                                                             "superseded_by")}
            self._history(db, mid, action, actor, data)

    @staticmethod
    def _history(db, mid: str, action: str, actor: str, data: dict) -> None:
        db.execute("INSERT INTO history(id, action, actor, ts, data) VALUES(?,?,?,?,?)",
                   (mid, action, actor or "unknown", time.time(),
                    json.dumps(data, separators=(",", ":"), default=str)))

    def history(self, mid: str, limit: int = 20) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute("SELECT action, actor, ts, data FROM history WHERE id=? "
                              "ORDER BY seq DESC LIMIT ?", (mid, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    # -- queries ---------------------------------------------------------
    @staticmethod
    def _scope_sql(scopes) -> tuple[str, list]:
        if not scopes:
            return "0", []
        return ("(" + " OR ".join("(scope_kind=? AND scope_id=?)" for _ in scopes) + ")",
                [x for s in scopes for x in s])

    def select(self, scopes=None, kinds=None, states=LIVE, limit: int = 200, offset: int = 0,
               order: str = "updated DESC") -> list[dict]:
        where, args = [], []
        if scopes is not None:
            sql, a = self._scope_sql(scopes)
            where.append(sql)
            args += a
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        sql = "SELECT * FROM memories" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        with self.db(False) as db:
            rows = db.execute(sql, [*args, limit, offset]).fetchall()
        return [self.row(r) for r in rows]

    def by_hash(self, scope, kind: str, h: str, states=("active", "pending")) -> dict | None:
        with self.db(False) as db:
            r = db.execute(f"SELECT * FROM memories WHERE scope_kind=? AND scope_id=? AND kind=? "
                           f"AND hash=? AND state IN ({','.join('?' * len(states))}) "
                           f"ORDER BY state='active' DESC, updated DESC LIMIT 1",
                           (*scope, kind, h, *states)).fetchone()
        return self.row(r) if r else None

    def by_source(self, source: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM memories WHERE source=? ORDER BY created LIMIT 1",
                           (source,)).fetchone()
        return self.row(r) if r else None

    def lexical(self, query: str, scopes, states=LIVE, limit: int = 50) -> list[dict]:
        q = fts_query(query)
        if not q:
            return []
        sql, args = self._scope_sql(scopes)
        with self.db(False) as db:
            rows = db.execute(
                f"SELECT m.* FROM memories m JOIN memories_fts f ON m.rowid=f.rowid "
                f"WHERE memories_fts MATCH ? AND {sql.replace('scope_', 'm.scope_')} "
                f"AND m.state IN ({','.join('?' * len(states))}) ORDER BY bm25(memories_fts) LIMIT ?",
                [q, *args, *states, limit]).fetchall()
        return [self.row(r) for r in rows]

    def touch(self, ids) -> None:
        if not ids:
            return
        now = time.time()
        with self.db() as db:
            db.executemany("UPDATE memories SET recalls=recalls+1, last_used=? WHERE id=?",
                           [(now, i) for i in ids])

    def counts(self) -> dict:
        with self.db(False) as db:
            rows = db.execute("SELECT state, kind, count(*) n, sum(length(text)) chars FROM memories "
                              "GROUP BY state, kind").fetchall()
            vec = db.execute("SELECT model, count(*) n FROM vectors GROUP BY model").fetchall()
        out: dict = {"by_state": {}, "active_by_kind": {}, "active_chars": 0,
                     "vectors": {r["model"]: r["n"] for r in vec}}
        for r in rows:
            out["by_state"][r["state"]] = out["by_state"].get(r["state"], 0) + r["n"]
            if r["state"] == "active":
                out["active_by_kind"][r["kind"]] = r["n"]
                out["active_chars"] += r["chars"] or 0
        return out

    # -- vectors ---------------------------------------------------------
    def put_vector(self, mid: str, model: str, vector: list[float]) -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO vectors(id, model, dim, vector) VALUES(?,?,?,?)",
                       (mid, model, len(vector), pack(vector)))

    def drop_vector(self, mid: str) -> None:
        with self.db() as db:
            db.execute("DELETE FROM vectors WHERE id=?", (mid,))

    def vectors(self, ids, model: str) -> dict:
        ids = list(ids)
        out: dict = {}
        with self.db(False) as db:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in db.execute(f"SELECT id, vector FROM vectors WHERE model=? AND id IN "
                                    f"({','.join('?' * len(chunk))})", [model, *chunk]):
                    out[r["id"]] = unpack(r["vector"])
        return out

    def iter_vectors(self, scopes, model: str, states=LIVE):
        """Stream ``(row, vector)`` for live memories in scope with a vector
        of ``model`` (bounded memory as the store grows)."""
        sql, args = self._scope_sql(scopes)
        with self.db(False) as db:
            rows = db.execute(
                f"SELECT m.*, v.vector AS _vec FROM memories m JOIN vectors v ON v.id=m.id "
                f"WHERE v.model=? AND {sql.replace('scope_', 'm.scope_')} "
                f"AND m.state IN ({','.join('?' * len(states))})",
                [model, *args, *states]).fetchall()
        for r in rows:
            d = self.row(r)
            yield d, unpack(d.pop("_vec"))

    def missing_vectors(self, model: str | None, limit: int = 32) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute(
                "SELECT m.id, m.text FROM memories m LEFT JOIN vectors v ON v.id=m.id "
                "WHERE m.state IN ('active','pending') AND (v.id IS NULL OR (? IS NOT NULL AND v.model<>?)) "
                "ORDER BY m.updated DESC LIMIT ?", (model, model, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- ingest bookkeeping ---------------------------------------------
    def ingested(self, key: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM ingested WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def mark_ingested(self, key: str, last_index: int, episode: str | None) -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO ingested(key, last_index, episode, updated) "
                       "VALUES(?,?,?,?)", (key, last_index, episode, time.time()))
