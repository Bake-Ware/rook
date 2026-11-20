"""One sqlite file: the record of what was said (the scratch pad is its part no dream has covered yet),
episodes, beliefs, the links between them that carry trueness, pins, and the dreams and calls.

Two clocks on everything: `happened_at` (or `made_at`) never changes; `last_used` moves only when a
real turn with a person pulls the memory up (see `use`). Nothing here is ever deleted except by `reset`.
"""

import datetime
import json
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,          -- a file taken in, or 'live'
    source_id TEXT,                -- the conversation's id in that file
    made_up INTEGER NOT NULL DEFAULT 0,
    cc_session TEXT,               -- the Claude Code session of a live one (what a dream forks)
    started_at TEXT, last_at TEXT,
    dreamed_to INTEGER NOT NULL DEFAULT 0,   -- the last entry id a dream has covered
    UNIQUE (source, source_id)
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id),
    at TEXT NOT NULL,              -- when it was said (never changes)
    speaker TEXT NOT NULL,         -- who said it: bake, steward, agent, other (stamped on the way in)
    channel TEXT NOT NULL,         -- chat or voice
    text TEXT NOT NULL,
    last_used TEXT,
    condensed_at TEXT              -- set when a dream covered it: no longer in the scratch pad
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id),
    text TEXT NOT NULL,
    said_by TEXT NOT NULL,         -- from the record, not from the model
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,            -- correction, statement, mention, unclear
    entries TEXT NOT NULL,         -- JSON list of entry ids it comes from
    happened_at TEXT NOT NULL,
    last_used TEXT,
    made_at TEXT NOT NULL,
    dream INTEGER                  -- null when a pin made it
);
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    about TEXT NOT NULL,           -- bake (a "should": his wishes, preferences, goals), how, world
    wordings TEXT,                 -- JSON: other ways of saying it, written by the dream (keyword search finds by them)
    made_at TEXT NOT NULL,
    last_used TEXT,
    dream INTEGER                  -- null when a pin made it
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    belief INTEGER NOT NULL REFERENCES beliefs(id),
    episode INTEGER NOT NULL REFERENCES episodes(id),
    stance INTEGER NOT NULL,       -- +1 supports, -1 counts against
    strength REAL NOT NULL,        -- kind x who x channel; 0 when it may not count (see note)
    at TEXT NOT NULL,              -- when the evidence happened (the episode's happened_at)
    occasion TEXT NOT NULL,
    made_at TEXT NOT NULL,
    dream INTEGER,
    why TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY,
    belief INTEGER NOT NULL REFERENCES beliefs(id),
    action TEXT NOT NULL,          -- pin or unpin
    at TEXT NOT NULL,
    entry INTEGER,                 -- his message it came from
    words TEXT
);
CREATE TABLE IF NOT EXISTS opposites (
    a INTEGER NOT NULL, b INTEGER NOT NULL,          -- a < b
    at TEXT NOT NULL, by TEXT NOT NULL,              -- 'dream N' or 'same check, dream N'
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS belief_texts (            -- earlier wordings of a belief (a pin rewords it)
    id INTEGER PRIMARY KEY, belief INTEGER NOT NULL, text TEXT NOT NULL, until TEXT NOT NULL, why TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY, session INTEGER NOT NULL, entry INTEGER NOT NULL, reply INTEGER,
    at TEXT NOT NULL, recalled TEXT       -- JSON list of the tags the turn was given (null: no memory)
);
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY, session INTEGER NOT NULL, at TEXT NOT NULL, summary TEXT
);
CREATE TABLE IF NOT EXISTS vectors (
    kind TEXT NOT NULL, ref INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
    PRIMARY KEY (kind, ref, model)
);
CREATE TABLE IF NOT EXISTS uses (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, session INTEGER, entry INTEGER,
    kind TEXT NOT NULL, ref INTEGER NOT NULL, how TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_log (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, role TEXT NOT NULL, session INTEGER, entry INTEGER,
    tool TEXT NOT NULL, args TEXT, answer TEXT
);
CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL,
    mode TEXT NOT NULL, trigger TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT,
    entries_from INTEGER, entries_to INTEGER,
    facts INTEGER, links INTEGER, new_beliefs INTEGER,
    held_back TEXT,                -- JSON: what the record would not let become a belief, and why
    answer TEXT                    -- the dream's JSON answer, as given
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT, cc_session TEXT,
    seconds REAL, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, turns INTEGER,
    ok INTEGER NOT NULL, note TEXT,
    reported_total REAL            -- Claude Code's total_cost_usd: for a resumed or forked session it is
                                   -- the session's running total, so cost_usd is this call's share of it
);
CREATE TABLE IF NOT EXISTS cc_totals (cc_session TEXT PRIMARY KEY, total REAL NOT NULL);
"""


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_time(text):
    t = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def stamp(text):
    """Any ISO time, as this store writes them (UTC, seconds)."""
    return parse_time(text).astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "memory.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:   # stores from before round three's keyword search
            self.db.execute("ALTER TABLE beliefs ADD COLUMN wordings TEXT")
        except sqlite3.OperationalError:
            pass

    def close(self):
        self.db.close()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def run(self, sql, *args):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.lastrowid

    # --- sessions and the record ---
    def session(self, name, source, source_id=None, made_up=False):
        row = self.one("SELECT * FROM sessions WHERE source = ? AND source_id IS ?", source, source_id)
        if row:
            return row
        self.run("INSERT INTO sessions (name, source, source_id, made_up) VALUES (?, ?, ?, ?)",
                 name, source, source_id, int(made_up))
        return self.one("SELECT * FROM sessions WHERE source = ? AND source_id IS ?", source, source_id)

    def add_entry(self, session, at, speaker, channel, text):
        at = stamp(at)
        eid = self.run("INSERT INTO entries (session, at, speaker, channel, text) VALUES (?, ?, ?, ?, ?)",
                       session, at, speaker, channel, text)
        self.run("UPDATE sessions SET started_at = COALESCE(started_at, ?), last_at = ? WHERE id = ?",
                 at, at, session)
        return eid

    def undreamed(self, session):
        s = self.one("SELECT dreamed_to FROM sessions WHERE id = ?", session)
        return self.all("SELECT * FROM entries WHERE session = ? AND id > ? ORDER BY id", session, s["dreamed_to"])

    def scratch(self):
        return self.all("SELECT * FROM entries WHERE condensed_at IS NULL ORDER BY id")

    # --- episodes, beliefs, links ---
    def add_episode(self, session, text, said_by, channel, kind, entries, happened_at, dream=None, at=None):
        return self.run("INSERT INTO episodes (session, text, said_by, channel, kind, entries, happened_at, made_at, dream)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", session, text, said_by, channel, kind,
                        json.dumps(list(entries)), stamp(happened_at), at or now(), dream)

    def add_belief(self, text, about, dream=None, at=None, wordings=None):
        return self.run("INSERT INTO beliefs (text, about, made_at, dream, wordings) VALUES (?, ?, ?, ?, ?)",
                        text, about, at or now(), dream, json.dumps(wordings) if wordings else None)

    def add_link(self, belief, episode, stance, strength, occasion, dream=None, why=None, note=None, at=None):
        ep = self.one("SELECT happened_at FROM episodes WHERE id = ?", episode)
        return self.run("INSERT INTO links (belief, episode, stance, strength, at, occasion, made_at, dream, why, note)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", belief, episode, 1 if stance > 0 else -1,
                        strength, ep["happened_at"], occasion, at or now(), dream, why, note)

    def reword(self, belief, text, at, why):
        old = self.one("SELECT text FROM beliefs WHERE id = ?", belief)["text"]
        if old == text:
            return
        self.run("INSERT INTO belief_texts (belief, text, until, why) VALUES (?, ?, ?, ?)", belief, old, at, why)
        self.run("UPDATE beliefs SET text = ? WHERE id = ?", text, belief)
        self.run("DELETE FROM vectors WHERE kind = 'belief' AND ref = ?", belief)

    def mark_opposites(self, a, b, at, by):
        if a == b:
            return
        a, b = min(a, b), max(a, b)
        self.run("INSERT OR IGNORE INTO opposites (a, b, at, by) VALUES (?, ?, ?, ?)", a, b, at, by)

    def opposites(self, belief):
        return [r["b"] if r["a"] == belief else r["a"]
                for r in self.all("SELECT * FROM opposites WHERE a = ? OR b = ?", belief, belief)]

    def links(self, belief):
        return self.all("SELECT l.*, e.text AS episode_text, e.said_by, e.kind, e.channel, e.session"
                        " FROM links l JOIN episodes e ON e.id = l.episode WHERE l.belief = ? ORDER BY l.at, l.id", belief)

    def pins(self, belief):
        return self.all("SELECT * FROM pins WHERE belief = ? ORDER BY id", belief)

    def pinned(self, belief):
        last = self.one("SELECT action FROM pins WHERE belief = ? ORDER BY id DESC LIMIT 1", belief)
        return bool(last and last["action"] == "pin")

    def add_pin(self, belief, action, at, entry=None, words=None):
        return self.run("INSERT INTO pins (belief, action, at, entry, words) VALUES (?, ?, ?, ?, ?)",
                        belief, action, stamp(at), entry, words)

    # --- the second clock: use by a real turn ---
    def use(self, at, items, session=None, entry=None, how="recall"):
        """A real turn with a person pulled these up: [(kind, id)] with kind entry/episode/belief."""
        at = stamp(at)
        for kind, ref in items:
            table = {"entry": "entries", "episode": "episodes", "belief": "beliefs"}[kind]
            self.db.execute(f"UPDATE {table} SET last_used = ? WHERE id = ? AND (last_used IS NULL OR last_used < ?)",
                            (at, ref, at))
            self.db.execute("INSERT INTO uses (at, session, entry, kind, ref, how) VALUES (?, ?, ?, ?, ?, ?)",
                            (at, session, entry, kind, ref, how))
        self.db.commit()

    # --- vectors ---
    def vectors(self, kind, model):
        return {r["ref"]: r["vec"] for r in self.all("SELECT ref, vec FROM vectors WHERE kind = ? AND model = ?", kind, model)}

    def put_vectors(self, kind, model, pairs):
        self.db.executemany("INSERT OR REPLACE INTO vectors (kind, ref, model, vec) VALUES (?, ?, ?, ?)",
                            [(kind, ref, model, vec) for ref, vec in pairs])
        self.db.commit()

    def log_tool(self, role, session, entry, tool, args, answer):
        self.run("INSERT INTO tool_log (at, role, session, entry, tool, args, answer) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 now(), role, session, entry, tool, json.dumps(args), answer)

    # --- dreams and calls ---
    def start_dream(self, session, mode, trigger, entries_from, entries_to):
        return self.run("INSERT INTO dreams (session, mode, trigger, started_at, entries_from, entries_to)"
                        " VALUES (?, ?, ?, ?, ?, ?)", session, mode, trigger, now(), entries_from, entries_to)

    def finish_dream(self, dream, facts, links, new_beliefs, held_back, answer):
        self.run("UPDATE dreams SET finished_at = ?, facts = ?, links = ?, new_beliefs = ?, held_back = ?, answer = ?"
                 " WHERE id = ?", now(), facts, links, new_beliefs, json.dumps(held_back), json.dumps(answer), dream)

    def add_call(self, purpose, model, seconds, cost, tokens_in, tokens_out, turns, ok, note=None, cc_session=None,
                 reported_total=None):
        self.run("INSERT INTO calls (at, purpose, model, cc_session, seconds, cost_usd, input_tokens, output_tokens,"
                 " turns, ok, note, reported_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", now(), purpose, model,
                 cc_session, seconds, cost, tokens_in, tokens_out, turns, int(ok), note, reported_total)

    def cc_total(self, cc_session):
        row = self.one("SELECT total FROM cc_totals WHERE cc_session = ?", cc_session)
        return row["total"] if row else 0.0

    def set_cc_total(self, cc_session, total):
        self.run("INSERT OR REPLACE INTO cc_totals (cc_session, total) VALUES (?, ?)", cc_session, total)
