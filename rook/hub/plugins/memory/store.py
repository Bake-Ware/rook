"""SQLite store for home agent memory (``memory.db`` in the plugin's data
directory, opened with :func:`rook.core.db.open_db` and the plugin's
migrations). No network, no model.

One row per memory with its kind, scope, state, confidence, provenance and
supersede edges; an FTS5 mirror for keyword search; float32 unit vectors per
embedding model; an append-only history; and the conversation turns waiting
for the consolidation job. Text changes are new rows that supersede old ones
(the page's "edit" does the same), so the only in-place updates are
bookkeeping: states, counters, the pinned flag.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ....core.db import open_db
from .embedder import pack, unpack
from .rules import STOPWORDS

NAMESPACE = "memory"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"
STATES = ("active", "superseded", "archived", "forgotten")
COLUMNS = ("id", "kind", "scope", "text", "hash", "slot", "state", "confidence", "pinned",
           "uses", "reinforced", "last_used", "created", "updated", "source", "author", "room",
           "ref", "supersedes", "superseded_by", "tags", "reason", "approved")
_JSON = ("supersedes", "tags")


def new_id() -> str:
    return "m_" + uuid.uuid4().hex[:14]


def fts_query(query: str) -> str:
    """The words of a free-text query as an FTS5 OR query (no operator
    injection: every word is quoted)."""
    words = [w for w in re.findall(r"\w+", str(query or "").lower())
             if len(w) > 1 and w not in STOPWORDS][:24]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        # 0600: memories are personal data, like the vault and the journal.
        self._conn = open_db(self.path, mode=0o600, migrations=MIGRATIONS, ns=NAMESPACE)
        # Forgotten text is overwritten on disk, not just unlinked from pages.
        self._conn.execute("PRAGMA secure_delete=ON")

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

    # -- rows ----------------------------------------------------------------------
    @staticmethod
    def row(r) -> dict:
        d = dict(r)
        for k in _JSON:
            if k in d:
                try:
                    d[k] = json.loads(d[k] or "[]")
                except ValueError:
                    d[k] = []
        for k in ("pinned", "approved"):
            if k in d:
                d[k] = bool(d[k])
        return d

    def insert(self, rec: dict, actor: str) -> dict:
        now = time.time()
        rec = {**rec}
        rec.setdefault("id", new_id())
        rec.setdefault("created", now)
        rec.setdefault("updated", now)
        rec["pinned"] = int(bool(rec.get("pinned")))
        rec["approved"] = int(bool(rec.get("approved")))
        for k in _JSON:
            rec[k] = json.dumps(rec.get(k) or [], separators=(",", ":"))
        cols = [c for c in COLUMNS if c in rec]
        with self.db() as db:
            db.execute(f"INSERT INTO memories({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       [rec[c] for c in cols])
            self._history(db, rec["id"], "create", actor,
                          {"state": rec["state"], "kind": rec["kind"], "source": rec.get("source")})
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
        for k in ("pinned", "approved"):
            if k in fields:
                fields[k] = int(bool(fields[k]))
        with self.db() as db:
            db.execute(f"UPDATE memories SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       [*fields.values(), mid])
            data = {k: v for k, v in fields.items()
                    if k in ("state", "reason", "confidence", "superseded_by", "pinned", "kind",
                             "approved")}
            self._history(db, mid, action, actor, data)

    def bump(self, mid: str, **counters) -> None:
        """Bookkeeping without a history row (reinforcement, recall counts)."""
        sets = ", ".join(f"{k}={k}+?" for k in counters)
        with self.db() as db:
            db.execute(f"UPDATE memories SET {sets}, last_used=? WHERE id=?",
                       [*counters.values(), time.time(), mid])

    @staticmethod
    def _history(db, mid: str, action: str, actor: str, data: dict) -> None:
        db.execute("INSERT INTO memory_history(id, action, actor, ts, data) VALUES(?,?,?,?,?)",
                   (mid, action, actor or "unknown", time.time(),
                    json.dumps(data, separators=(",", ":"), default=str)))

    def history(self, mid: str, limit: int = 30) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute("SELECT action, actor, ts, data FROM memory_history WHERE id=? "
                              "ORDER BY seq DESC LIMIT ?", (mid, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def chain(self, mid: str) -> list[str]:
        """``mid`` and every older version it replaced, transitively."""
        seen: list[str] = []
        todo = [mid]
        with self.db(False) as db:
            while todo:
                cur = todo.pop()
                if cur in seen:
                    continue
                seen.append(cur)
                r = db.execute("SELECT supersedes FROM memories WHERE id=?", (cur,)).fetchone()
                if r:
                    try:
                        todo += [x for x in json.loads(r[0] or "[]") if isinstance(x, str)]
                    except ValueError:
                        pass
                todo += [x[0] for x in db.execute(
                    "SELECT id FROM memories WHERE superseded_by=? AND state='superseded'", (cur,))]
        return seen

    def purge(self, mid: str, actor: str) -> list[str]:
        """Erase the text of ``mid`` and of every older version it replaced
        (a corrected memory's earlier wording is the same personal data),
        their vectors and their history. Each row stays as a tombstone with
        one ``purge`` history entry, so the page still says that something
        was forgotten, and when. Returns the ids erased."""
        ids = self.chain(mid)
        now = time.time()
        with self.db() as db:
            for i in ids:
                db.execute("UPDATE memories SET text='[forgotten]', hash='purged', slot=NULL, "
                           "state='forgotten', reason='erased', tags='[]', updated=? WHERE id=?",
                           (now, i))
                db.execute("DELETE FROM memory_vectors WHERE id=?", (i,))
                db.execute("DELETE FROM memory_history WHERE id=?", (i,))
                self._history(db, i, "purge", actor, {"state": "forgotten",
                                                      "with": mid if i != mid else None})
        return ids

    def expire(self, before: float) -> int:
        """Delete superseded, archived and forgotten rows last changed before
        ``before``, with their vectors and history (retention)."""
        with self.db() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM memories WHERE state IN ('superseded','archived','forgotten') "
                "AND updated < ?", (before,))]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                db.execute(f"DELETE FROM memory_vectors WHERE id IN ({marks})", chunk)
                db.execute(f"DELETE FROM memory_history WHERE id IN ({marks})", chunk)
                db.execute(f"DELETE FROM memories WHERE id IN ({marks})", chunk)
        return len(ids)

    def drop_all_turns(self, older_than: float | None = None) -> int:
        with self.db() as db:
            if older_than is None:
                return db.execute("DELETE FROM memory_turns").rowcount
            return db.execute("DELETE FROM memory_turns WHERE ts < ?", (older_than,)).rowcount

    # -- queries -----------------------------------------------------------------
    @staticmethod
    def _in(col: str, values) -> tuple[str, list]:
        values = list(values)
        return f"{col} IN ({','.join('?' * len(values))})", values

    def select(self, scopes=None, kinds=None, states=("active",), limit: int = 200,
               offset: int = 0, order: str = "updated DESC", query: str = "") -> list[dict]:
        where, args = [], []
        for col, vals in (("scope", scopes), ("kind", kinds), ("state", states)):
            if vals:
                sql, a = self._in(col, vals)
                where.append(sql)
                args += a
        if query:
            where.append("text LIKE ? ESCAPE '\\'")
            args.append("%" + re.sub(r"([%_\\])", r"\\\1", query) + "%")
        sql = "SELECT * FROM memories" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        with self.db(False) as db:
            rows = db.execute(sql, [*args, int(limit), int(offset)]).fetchall()
        return [self.row(r) for r in rows]

    def count(self, scopes=None, kinds=None, states=("active",), query: str = "") -> int:
        where, args = [], []
        for col, vals in (("scope", scopes), ("kind", kinds), ("state", states)):
            if vals:
                sql, a = self._in(col, vals)
                where.append(sql)
                args += a
        if query:
            where.append("text LIKE ? ESCAPE '\\'")
            args.append("%" + re.sub(r"([%_\\])", r"\\\1", query) + "%")
        sql = "SELECT count(*) FROM memories" + (" WHERE " + " AND ".join(where) if where else "")
        with self.db(False) as db:
            return int(db.execute(sql, args).fetchone()[0])

    def by_hash(self, scope: str, kind: str, h: str) -> dict | None:
        with self.db(False) as db:
            r = db.execute("SELECT * FROM memories WHERE scope=? AND kind=? AND hash=? "
                           "AND state='active' ORDER BY updated DESC LIMIT 1",
                           (scope, kind, h)).fetchone()
        return self.row(r) if r else None

    def by_slot(self, scope: str, kind: str, slot: str) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute("SELECT * FROM memories WHERE scope=? AND kind=? AND slot=? "
                              "AND state='active'", (scope, kind, slot)).fetchall()
        return [self.row(r) for r in rows]

    def lexical(self, query: str, scopes, kinds=None, states=("active",),
                limit: int = 50) -> list[dict]:
        q = fts_query(query)
        if not q:
            return []
        where = ["memories_fts MATCH ?"]
        args: list = [q]
        for col, vals in (("m.scope", scopes), ("m.kind", kinds), ("m.state", states)):
            if vals:
                sql, a = self._in(col, vals)
                where.append(sql)
                args += a
        with self.db(False) as db:
            rows = db.execute(
                "SELECT m.* FROM memories m JOIN memories_fts f ON m.rowid=f.rowid WHERE "
                + " AND ".join(where) + " ORDER BY bm25(memories_fts) LIMIT ?",
                [*args, int(limit)]).fetchall()
        return [self.row(r) for r in rows]

    def touch(self, ids) -> None:
        ids = list(ids)
        if not ids:
            return
        now = time.time()
        with self.db() as db:
            db.executemany("UPDATE memories SET uses=uses+1, last_used=? WHERE id=?",
                           [(now, i) for i in ids])

    def counts(self) -> dict:
        with self.db(False) as db:
            rows = db.execute("SELECT state, kind, count(*) n, sum(length(text)) chars "
                              "FROM memories GROUP BY state, kind").fetchall()
            vec = db.execute("SELECT model, count(*) n FROM memory_vectors GROUP BY model").fetchall()
            turns = db.execute("SELECT count(*) FROM memory_turns").fetchone()[0]
            people = db.execute("SELECT DISTINCT scope FROM memories WHERE state='active' "
                                "ORDER BY scope").fetchall()
        out: dict = {"by_state": {}, "active": {}, "active_chars": 0,
                     "vectors": {r["model"]: r["n"] for r in vec}, "waiting_turns": int(turns),
                     "scopes": [r[0] for r in people]}
        for r in rows:
            out["by_state"][r["state"]] = out["by_state"].get(r["state"], 0) + r["n"]
            if r["state"] == "active":
                out["active"][r["kind"]] = r["n"]
                out["active_chars"] += r["chars"] or 0
        return out

    # -- vectors -------------------------------------------------------------------
    def put_vector(self, mid: str, model: str, vector: list[float]) -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO memory_vectors(id, model, dim, vector) VALUES(?,?,?,?)",
                       (mid, model, len(vector), pack(vector)))

    def vectors(self, ids, model: str) -> dict:
        ids = list(ids)
        out: dict = {}
        with self.db(False) as db:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in db.execute(f"SELECT id, vector FROM memory_vectors WHERE model=? AND id IN "
                                    f"({','.join('?' * len(chunk))})", [model, *chunk]):
                    out[r["id"]] = unpack(r["vector"])
        return out

    def iter_vectors(self, scopes, model: str, kinds=None, states=("active",)):
        where, args = ["v.model=?"], [model]
        for col, vals in (("m.scope", scopes), ("m.kind", kinds), ("m.state", states)):
            if vals:
                sql, a = self._in(col, vals)
                where.append(sql)
                args += a
        with self.db(False) as db:
            rows = db.execute("SELECT m.*, v.vector AS _vec FROM memories m JOIN memory_vectors v "
                              "ON v.id=m.id WHERE " + " AND ".join(where), args).fetchall()
        for r in rows:
            d = self.row(r)
            yield d, unpack(d.pop("_vec"))

    def missing_vectors(self, model: str | None, limit: int = 32) -> list[dict]:
        with self.db(False) as db:
            if model:
                rows = db.execute(
                    "SELECT m.id, m.text FROM memories m WHERE m.state='active' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_vectors v WHERE v.id=m.id AND v.model=?) "
                    "ORDER BY m.updated DESC LIMIT ?", (model, limit)).fetchall()
            else:
                rows = db.execute(
                    "SELECT m.id, m.text FROM memories m WHERE m.state='active' AND NOT EXISTS "
                    "(SELECT 1 FROM memory_vectors v WHERE v.id=m.id) "
                    "ORDER BY m.updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- conversation turns --------------------------------------------------------
    def add_turn(self, room: str, scope: str, role: str, sender: str, text: str) -> None:
        with self.db() as db:
            db.execute("INSERT INTO memory_turns(room, scope, role, sender, text, ts) "
                       "VALUES(?,?,?,?,?,?)", (room, scope, role, sender, text, time.time()))

    def turn_rooms(self) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute("SELECT room, scope, count(*) n, max(ts) last, min(ts) first, "
                              "max(seq) seq FROM memory_turns GROUP BY room, scope").fetchall()
        return [dict(r) for r in rows]

    def turns(self, room: str, scope: str, upto: int) -> list[dict]:
        with self.db(False) as db:
            rows = db.execute("SELECT * FROM memory_turns WHERE room=? AND scope=? AND seq<=? "
                              "ORDER BY seq", (room, scope, upto)).fetchall()
        return [dict(r) for r in rows]

    def drop_turns(self, room: str, scope: str, upto: int) -> None:
        with self.db() as db:
            db.execute("DELETE FROM memory_turns WHERE room=? AND scope=? AND seq<=?",
                       (room, scope, upto))

    # -- meta ----------------------------------------------------------------------
    def meta(self, key: str, default=None):
        with self.db(False) as db:
            r = db.execute("SELECT value FROM memory_meta WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default

    def set_meta(self, key: str, value) -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES(?,?)",
                       (key, json.dumps(value, default=str)))
