"""The hub's journal (spec 11 of docs/spec/core-v2.md): one SQLite table,
write-once rows, a hash chain per project plus the general chain.

Only the hub writes. ``append`` stamps the session's project and chain,
links the entry after that chain's head and inserts it. Triggers refuse
UPDATE always and DELETE of any row past its chain's checkpoint; the only
way to move a checkpoint is ``prune``, which journals itself. The hash chain
catches anyone who goes around the triggers.

The hub keeps every chain's head (and checkpoint) in memory; ``check``
compares them with the table and walks the chains. A mismatch means history
was rewritten under the running hub.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator

from .. import journal, rights
from ..wire import canonical

SCHEMA = 1

# Columns copied out of the entry for queries; the hashed entry is ``body``.
COLUMNS = ("id", "kind", "chain", "ts", "call", "cap", "worker", "person", "agent", "session", "project",
           "risk", "decision", "prev", "hash")

_DDL = f"""
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(f"{c} TEXT" if c != "ts" else "ts REAL NOT NULL" for c in COLUMNS)},
    body TEXT NOT NULL,
    UNIQUE (id));
CREATE INDEX IF NOT EXISTS journal_chain ON journal (chain, seq);
CREATE INDEX IF NOT EXISTS journal_call ON journal (call);
CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts);
-- (column, seq): a filtered read is newest first by seq, so these serve its
-- order too; (column, ts) made SQLite sort every matching row (soak: 130 ms
-- for an agent's 40,000 entries, under the lock appends wait on).
DROP INDEX IF EXISTS journal_cap;
DROP INDEX IF EXISTS journal_worker;
DROP INDEX IF EXISTS journal_person;
DROP INDEX IF EXISTS journal_agent;
DROP INDEX IF EXISTS journal_project;
CREATE INDEX IF NOT EXISTS journal_cap_seq ON journal (cap, seq);
CREATE INDEX IF NOT EXISTS journal_worker_seq ON journal (worker, seq);
CREATE INDEX IF NOT EXISTS journal_person_seq ON journal (person, seq);
CREATE INDEX IF NOT EXISTS journal_agent_seq ON journal (agent, seq);
CREATE INDEX IF NOT EXISTS journal_project_seq ON journal (project, seq);

-- Where a pruned chain now starts: the last pruned entry (seq, hash). It
-- only moves forward, must name a real entry when it moves, and is never
-- deleted, so a chain verifies from here instead of from GENESIS.
CREATE TABLE IF NOT EXISTS journal_checkpoints (
    chain TEXT PRIMARY KEY, seq INTEGER NOT NULL, hash TEXT NOT NULL, ts REAL NOT NULL);

CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is write-once'); END;
CREATE TRIGGER IF NOT EXISTS journal_prune_only BEFORE DELETE ON journal
WHEN NOT EXISTS (SELECT 1 FROM journal_checkpoints c WHERE c.chain = OLD.chain AND c.seq >= OLD.seq)
BEGIN SELECT RAISE(ABORT, 'journal is write-once: only retention pruning deletes'); END;

CREATE TRIGGER IF NOT EXISTS checkpoint_real BEFORE INSERT ON journal_checkpoints
WHEN NOT EXISTS (SELECT 1 FROM journal j WHERE j.seq = NEW.seq AND j.chain = NEW.chain AND j.hash = NEW.hash)
BEGIN SELECT RAISE(ABORT, 'checkpoint must name a journal entry'); END;
CREATE TRIGGER IF NOT EXISTS checkpoint_forward BEFORE UPDATE ON journal_checkpoints
WHEN NEW.chain != OLD.chain OR NEW.seq <= OLD.seq
  OR NOT EXISTS (SELECT 1 FROM journal j WHERE j.seq = NEW.seq AND j.chain = NEW.chain AND j.hash = NEW.hash)
BEGIN SELECT RAISE(ABORT, 'checkpoints only move forward to a journal entry'); END;
CREATE TRIGGER IF NOT EXISTS checkpoint_kept BEFORE DELETE ON journal_checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are never deleted'); END;
"""

# Riskiest entry of a segment, as an index into rights.RISKS (see journal.tier).
_RANK = ("CASE WHEN risk IS NULL OR risk = 'read' THEN 0 WHEN risk = 'write' THEN 1 "
         "WHEN risk = 'exec' THEN 2 ELSE 3 END")

PAGE = 2000  # rows per read when walking a chain


class JournalError(Exception):
    pass


class JournalStore:
    def __init__(self, path: str = ":memory:", *, clock: Callable[[], float] = time.time,
                 retention: dict | None = None):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()   # the connection and the in-memory chains
        self._maint = threading.Lock()   # verify and prune never overlap
        self.clock = clock
        self.retention = {**journal.RETENTION_DAYS, **(retention or {})}
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA):
            raise JournalError(f"journal schema {version} is newer than this hub ({SCHEMA})")
        self._db.executescript(_DDL)
        self._db.execute(f"PRAGMA user_version = {SCHEMA}")
        # chain -> [checkpoint hash, head hash] as this hub last wrote them.
        self._chains: dict[str, list[str]] = {}
        for chain, cp in self._db.execute("SELECT chain, hash FROM journal_checkpoints"):
            self._chains[chain] = [cp, cp]
        for chain, head in self._db.execute(
                "SELECT chain, hash FROM journal WHERE seq IN (SELECT MAX(seq) FROM journal GROUP BY chain)"):
            self._chains.setdefault(chain, [journal.GENESIS, journal.GENESIS])[1] = head

    def close(self) -> None:
        self._db.close()

    def heads(self) -> dict[str, str]:
        with self._lock:
            return {c: h for c, (_, h) in self._chains.items()}

    @contextmanager
    def _tx(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # -- writing --------------------------------------------------------------

    def append(self, entry: dict, session: dict | None = None) -> dict:
        """Stamp ``entry`` with the session's project and chain, link it after
        that chain's head and store it. Any ``chain``, ``project``, ``prev`` or
        ``hash`` the caller set is replaced. Returns the stored entry."""
        e = journal.stamp({k: v for k, v in entry.items() if k not in ("prev", "hash")}, session)
        e.setdefault("id", uuid.uuid4().hex)
        e.setdefault("ts", self.clock())
        if not isinstance(e.get("kind"), str) or not e["kind"]:
            raise JournalError("entry needs a kind")
        if not isinstance(e["id"], str) or not e["id"]:
            raise JournalError("entry id must be text")
        if isinstance(e["ts"], bool) or not isinstance(e["ts"], (int, float)):
            raise JournalError("entry ts must be a number")
        with self._lock:
            with self._tx():
                e = self._insert(e)
            self._chains.setdefault(e["chain"], [journal.GENESIS, journal.GENESIS])[1] = e["hash"]
        return e

    def _insert(self, e: dict) -> dict:
        """Link and insert inside the caller's transaction; the caller moves
        the in-memory head once it commits."""
        e = journal.append(self._chains.get(e["chain"], [journal.GENESIS, journal.GENESIS])[1], e)
        body = canonical(e).decode("utf-8")
        try:
            self._db.execute(
                f"INSERT INTO journal ({', '.join(COLUMNS)}, body) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                [_col(c, e.get(c)) for c in COLUMNS] + [body])
        except sqlite3.IntegrityError as exc:
            raise JournalError(f"duplicate journal id {e['id']}") from exc
        return e

    # -- reading --------------------------------------------------------------

    def get(self, entry_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM journal WHERE id = ?", (entry_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def query(self, *, call: str | None = None, cap: str | None = None, worker: str | None = None,
              person: str | None = None, agent: str | None = None, actor: str | None = None,
              project: str | None = None, chain: str | None = None, kind: str | None = None,
              session: str | None = None, since: float | None = None, until: float | None = None,
              viewer: dict | None = None, limit: int = 100) -> list[dict]:
        """Entries matching every filter given, newest first. ``cap`` ending in
        ``.*`` matches that prefix; ``actor`` is person or agent; ``since`` is
        inclusive, ``until`` exclusive. ``viewer`` limits to what they may
        read (11.4)."""
        where, params = [], []
        for col, val in (("call", call), ("worker", worker), ("person", person), ("agent", agent),
                         ("project", project), ("chain", chain), ("kind", kind), ("session", session)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if cap is not None and cap.endswith(".*"):
            where.append("substr(cap, 1, ?) = ?")
            params += [len(cap) - 1, cap[:-1]]
        elif cap is not None:
            where.append("cap = ?")
            params.append(cap)
        if actor is not None:
            where.append("(person = ? OR agent = ?)")
            params += [actor, actor]
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts < ?")
            params.append(until)
        if viewer is not None:
            clause, vparams = _viewer_clause(viewer)
            if clause:
                where.append(clause)
                params += vparams
        sql = "SELECT body FROM journal" + (" WHERE " + " AND ".join(where) if where else "")
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seq DESC LIMIT ?", params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def entries(self, chain: str) -> Iterator[dict]:
        """A chain's entries, oldest first (from its checkpoint once pruned)."""
        for row in self._rows(chain):
            yield json.loads(row[-1])

    def _rows(self, chain: str, upto: int | None = None) -> Iterator[tuple]:
        """Rows of a chain in pages, so a long walk never holds the lock."""
        after = 0
        while True:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT seq, {', '.join(COLUMNS)}, body FROM journal WHERE chain = ? AND seq > ? AND seq <= ? "
                    "ORDER BY seq LIMIT ?", (chain, after, upto if upto is not None else 2**62, PAGE)).fetchall()
            for row in rows:
                yield row[1:]
            if len(rows) < PAGE:
                return
            after = rows[-1][0]

    # -- verification ---------------------------------------------------------

    def verify(self, chain: str) -> dict | None:
        """Walk one chain from its stored checkpoint. None if it holds, else
        ``{chain, id, problem}`` for the first entry that doesn't follow
        (``id`` is None when the checkpoint or head is the problem)."""
        with self._maint:
            with self._lock:
                cp = self._db.execute("SELECT hash FROM journal_checkpoints WHERE chain = ?", (chain,)).fetchone()
                upto = self._db.execute("SELECT MAX(seq) FROM journal WHERE chain = ?", (chain,)).fetchone()[0]
                mem = list(self._chains.get(chain, ()))
            if not mem:
                return {"chain": chain, "id": None, "problem": "chain this hub never wrote"}
            start = cp[0] if cp else journal.GENESIS
            if mem[0] != start:
                return {"chain": chain, "id": None, "problem": "checkpoint differs"}
            prev = start
            for row in self._rows(chain, upto or 0):
                cols, body = row[:-1], row[-1]
                try:
                    e = json.loads(body)
                    linked = isinstance(e, dict) and e.get("prev") == prev and e.get("hash") == journal.entry_hash(e)
                except ValueError:
                    linked = False
                if not linked:
                    return {"chain": chain, "id": cols[0], "problem": "broken link"}
                if list(cols) != [_col(c, e.get(c)) for c in COLUMNS]:
                    return {"chain": chain, "id": cols[0], "problem": "columns differ from entry"}
                prev = e["hash"]
            # The snapshot head is the hash of row ``upto``, whatever was
            # appended since.
            if mem[1] != prev:
                return {"chain": chain, "id": None, "problem": "head differs"}
            return None

    def check(self) -> list[dict]:
        """Verify every chain the table or this hub knows; empty when all hold.
        The hub runs this periodically."""
        with self._lock:
            chains = {r[0] for r in self._db.execute("SELECT DISTINCT chain FROM journal")}
            chains |= {r[0] for r in self._db.execute("SELECT chain FROM journal_checkpoints")}
            chains |= set(self._chains)
        return [p for c in sorted(chains) if (p := self.verify(c))]

    # -- retention ------------------------------------------------------------

    def prune(self, now: float | None = None, closed: dict | None = None) -> list[dict]:
        """Retention (11.4). ``closed`` maps closed projects to when they
        closed. A closed project's chain goes whole once its last activity is
        past the tier of its riskiest entry; the general chain goes by UTC
        day, oldest first, stopping at the first day still kept. Each prune
        moves the chain's checkpoint to its last pruned entry, deletes up to
        it and journals a ``prune`` entry on the general chain; returns those."""
        now = self.clock() if now is None else now
        out = []
        with self._maint, self._lock:
            for project, closed_at in sorted((closed or {}).items()):
                chain = journal.chain_name(project)
                seq, last_ts, rank = self._db.execute(
                    f"SELECT MAX(seq), MAX(ts), MAX({_RANK}) FROM journal WHERE chain = ?", (chain,)).fetchone()
                if seq is not None and journal.expired(max(last_ts, closed_at), rights.RISKS[rank], now,
                                                       self.retention):
                    out.append(self._prune_through(chain, seq, "project closed", now))
            seq = self._general_cutoff(now)
            if seq is not None:
                out.append(self._prune_through(journal.GENERAL, seq, "age", now))
        return out

    def _general_cutoff(self, now: float) -> int | None:
        """The last general-chain seq such that it and every entry before it
        lie in expired days, or None if nothing is due."""
        day = f"CAST(ts / {journal.DAY} AS INTEGER)"
        days = self._db.execute(f"SELECT {day} AS d, MAX({_RANK}) FROM journal WHERE chain = ? GROUP BY d",
                                (journal.GENERAL,)).fetchall()
        kept = [d for d, rank in days
                if not journal.expired((d + 1) * journal.DAY, rights.RISKS[rank], now, self.retention)]
        if len(kept) == len(days):
            return None
        cond, params = "", [journal.GENERAL]
        if kept:
            first_kept = self._db.execute(
                f"SELECT MIN(seq) FROM journal WHERE chain = ? AND {day} IN ({','.join('?' * len(kept))})",
                [journal.GENERAL, *kept]).fetchone()[0]
            cond, params = " AND seq < ?", [journal.GENERAL, first_kept]
        return self._db.execute(f"SELECT MAX(seq) FROM journal WHERE chain = ?{cond}", params).fetchone()[0]

    def _prune_through(self, chain: str, seq: int, reason: str, now: float) -> dict:
        last_id, last_hash = self._db.execute("SELECT id, hash FROM journal WHERE seq = ?", (seq,)).fetchone()
        first_ts = self._db.execute("SELECT MIN(ts) FROM journal WHERE chain = ? AND seq <= ?",
                                    (chain, seq)).fetchone()[0]
        with self._tx():
            self._db.execute(
                "INSERT INTO journal_checkpoints (chain, seq, hash, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (chain) DO UPDATE SET seq = excluded.seq, hash = excluded.hash, ts = excluded.ts",
                (chain, seq, last_hash, now))
            count = self._db.execute("DELETE FROM journal WHERE chain = ? AND seq <= ?", (chain, seq)).rowcount
            e = self._insert(journal.stamp({
                "id": uuid.uuid4().hex, "kind": "prune", "ts": now, "pruned": chain, "reason": reason,
                "through": last_id, "through_hash": last_hash, "count": count, "from_ts": first_ts}, None))
        self._chains.setdefault(chain, [journal.GENESIS, last_hash])[0] = last_hash
        self._chains.setdefault(e["chain"], [journal.GENESIS, journal.GENESIS])[1] = e["hash"]
        return e


def _col(name: str, v):
    """A column's value: ts as a number, other fields as text (non-text as
    canonical JSON)."""
    if name == "ts" or v is None or isinstance(v, str):
        return v
    return canonical(v).decode("utf-8")


def _viewer_clause(viewer: dict) -> tuple[str, list]:
    """11.4: owners read everything; an agent reads its own actions and its
    projects' history; a person reads their own actions."""
    if viewer.get("owner"):
        return "", []
    if viewer.get("agent"):
        projects = list(viewer.get("projects") or [])
        clause = "agent = ?" + (f" OR project IN ({','.join('?' * len(projects))})" if projects else "")
        return f"({clause})", [viewer["agent"], *projects]
    if viewer.get("person"):
        return "person = ?", [viewer["person"]]
    return "0", []
