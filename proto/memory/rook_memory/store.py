"""The store: one sqlite file. Sessions taken in, the scratch pad, episodes, beliefs and what supports
them, dream passes, and every model call with what it cost.

Beliefs are never deleted. A belief that is superseded keeps its row and gets `superseded_by` (the
new belief), `how` (contradicts or refines) and `superseded_at`.
"""

import datetime
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lines_taken INTEGER NOT NULL DEFAULT 0,
  first_at TEXT, last_at TEXT, taken_at TEXT NOT NULL, scratch_condensed_at TEXT
);
CREATE TABLE IF NOT EXISTS scratch (
  id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id),
  line INTEGER NOT NULL, at TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id),
  first_line INTEGER NOT NULL, last_line INTEGER NOT NULL,
  happened_at TEXT NOT NULL, last_read TEXT NOT NULL, importance INTEGER NOT NULL,
  text TEXT NOT NULL, made_at TEXT NOT NULL, dreamed_in INTEGER REFERENCES dreams(id)
);
CREATE TABLE IF NOT EXISTS beliefs (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, confidence REAL NOT NULL, importance INTEGER NOT NULL,
  made_at TEXT NOT NULL, last_read TEXT NOT NULL, dream INTEGER REFERENCES dreams(id), question TEXT,
  superseded_by INTEGER REFERENCES beliefs(id), how TEXT, superseded_at TEXT
);
CREATE TABLE IF NOT EXISTS support (
  belief INTEGER NOT NULL REFERENCES beliefs(id), kind TEXT NOT NULL, ref INTEGER NOT NULL,
  PRIMARY KEY (belief, kind, ref)
);
CREATE TABLE IF NOT EXISTS dreams (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, trigger TEXT NOT NULL,
  questions TEXT, episodes_in INTEGER, beliefs_made INTEGER, superseded INTEGER, scratch_condensed INTEGER
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
  seconds REAL, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, ok INTEGER NOT NULL, note TEXT
);
"""


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


class Store:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "memory.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    # sessions and the scratch pad

    def session(self, path):
        path = str(pathlib.Path(path).expanduser().resolve())
        row = self.one("SELECT * FROM sessions WHERE path = ?", path)
        if row is None:
            self.db.execute("INSERT INTO sessions (path, taken_at) VALUES (?, ?)", (path, now()))
            self.db.commit()
            row = self.one("SELECT * FROM sessions WHERE path = ?", path)
        return row

    def add_scratch(self, session, entries):
        self.db.executemany(
            "INSERT INTO scratch (session, line, at, role, text) VALUES (?, ?, ?, ?, ?)",
            [(session, e.line, e.at, e.role, e.text) for e in entries])
        if entries:
            self.db.execute(
                "UPDATE sessions SET lines_taken = MAX(lines_taken, ?), first_at = COALESCE(first_at, ?),"
                " last_at = ?, taken_at = ?, scratch_condensed_at = NULL WHERE id = ?",
                (entries[-1].line, entries[0].at, entries[-1].at, now(), session))
        self.db.commit()

    def mark_taken(self, session, lines):
        self.db.execute("UPDATE sessions SET lines_taken = MAX(lines_taken, ?) WHERE id = ?", (lines, session))
        self.db.commit()

    def condense_scratch(self, keep_last):
        """Drop the scratch pad of every session but the `keep_last` most recent ones (by when they
        ended) that still have one. Answers how many entries were dropped."""
        sessions = self.q("SELECT DISTINCT s.id FROM sessions s JOIN scratch c ON c.session = s.id"
                          " ORDER BY s.last_at DESC")
        dropped = 0
        for row in sessions[keep_last:]:
            dropped += self.db.execute("DELETE FROM scratch WHERE session = ?", (row["id"],)).rowcount
            self.db.execute("UPDATE sessions SET scratch_condensed_at = ? WHERE id = ?", (now(), row["id"]))
        self.db.commit()
        return dropped

    # episodes

    def add_episode(self, session, first_line, last_line, happened_at, importance, text):
        cur = self.db.execute(
            "INSERT INTO episodes (session, first_line, last_line, happened_at, last_read, importance, text, made_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session, first_line, last_line, happened_at, happened_at, importance, text, now()))
        self.db.commit()
        return cur.lastrowid

    def undreamed(self):
        return self.q("SELECT * FROM episodes WHERE dreamed_in IS NULL ORDER BY happened_at, id")

    def undreamed_importance(self):
        return self.one("SELECT COALESCE(SUM(importance), 0) AS n FROM episodes WHERE dreamed_in IS NULL")["n"]

    # beliefs and supersedence

    def head(self, belief):
        """The belief that stands now in `belief`'s line: itself, or what superseded it, and so on."""
        seen = set()
        while True:
            row = self.one("SELECT id, superseded_by FROM beliefs WHERE id = ?", belief)
            if row is None or row["superseded_by"] is None or row["id"] in seen:
                return belief
            seen.add(row["id"])
            belief = row["superseded_by"]

    def add_belief(self, text, confidence, importance, dream=None, question=None, because=(),
                   supersedes=None, how=None, at=None):
        """Write a belief. `because` is [(kind, id)] with kind episode or belief. When `supersedes`
        names a belief, the belief standing now in its line is marked superseded by this one."""
        at = at or now()
        cur = self.db.execute(
            "INSERT INTO beliefs (text, confidence, importance, made_at, last_read, dream, question)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, max(0.0, min(1.0, float(confidence))), int(importance), at, at, dream, question))
        new = cur.lastrowid
        for kind, ref in because:
            self.db.execute("INSERT OR IGNORE INTO support (belief, kind, ref) VALUES (?, ?, ?)", (new, kind, ref))
        old = None
        if supersedes is not None and self.one("SELECT 1 FROM beliefs WHERE id = ?", supersedes):
            old = self.head(supersedes)
            if old != new:
                self.db.execute("UPDATE beliefs SET superseded_by = ?, how = ?, superseded_at = ? WHERE id = ?",
                                (new, how or "refines", at, old))
            else:
                old = None
        self.db.commit()
        return new, old

    def current_beliefs(self):
        return self.q("SELECT * FROM beliefs WHERE superseded_by IS NULL ORDER BY id")

    def support_of(self, belief):
        return self.q("SELECT kind, ref FROM support WHERE belief = ? ORDER BY kind DESC, ref", belief)

    def touch(self, kind, ids, at=None):
        table = {"episode": "episodes", "belief": "beliefs"}[kind]
        self.db.executemany(f"UPDATE {table} SET last_read = ? WHERE id = ?", [(at or now(), i) for i in ids])
        self.db.commit()

    # dreams and calls

    def start_dream(self, trigger):
        cur = self.db.execute("INSERT INTO dreams (started_at, trigger) VALUES (?, ?)", (now(), trigger))
        self.db.commit()
        return cur.lastrowid

    def finish_dream(self, dream, questions, episode_ids, made, superseded, condensed):
        self.db.executemany("UPDATE episodes SET dreamed_in = ? WHERE id = ?", [(dream, i) for i in episode_ids])
        self.db.execute("UPDATE dreams SET finished_at = ?, questions = ?, episodes_in = ?, beliefs_made = ?,"
                        " superseded = ?, scratch_condensed = ? WHERE id = ?",
                        (now(), questions, len(episode_ids), made, superseded, condensed, dream))
        self.db.commit()

    def add_call(self, purpose, model, seconds, cost, input_tokens, output_tokens, ok, note=None):
        self.db.execute("INSERT INTO calls (at, purpose, model, seconds, cost_usd, input_tokens, output_tokens, ok, note)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (now(), purpose, model, seconds, cost, input_tokens, output_tokens, int(ok), note))
        self.db.commit()
