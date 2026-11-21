"""The house agent's memory (brief MEM5; the design Bake settled through MEM1 to MEM4, ported from
proto/memory on ha/memory-proto, draft pull request 458).

**Layers.** The conversation as kept (each person's stream, hub.py) is the word-for-word record;
this module keeps an index of it (`entries`), each entry with who said it and how it arrived.
Episodes are facts of what was said or happened, each tied to its entries and to whose words they
are. Beliefs rest on episodes through links, for or against, with a strength (kind x who said it):
a belief has no score of its own. The newest strong link says which way it stands; how many
separate occasions (days) back that way says how sure (maturity); older links are its history.

**Who said it.** `person:<name>` is a person's own words (their own site session, or as themselves);
`agent:<token>` is an agent at the door with a person's token, `agent:house` the house agent
messaging itself, `job:<job>` a job's call, and `house` the house agent's replies. Only a person's
own words can found or move a belief about what that person wants (about `person:<name>`); what an
agent or a job said is theirs. Only strong evidence flips a belief, and an agent's word never does
(except its decision on how something works): the house agent asks the person once instead, and
their answer decides. Nothing is ever written about not knowing.

**Pins.** The agent decides what is definitive and pins it (agent.memory.pin) from the person's own
message; every pin, unpin and replacement is said in the reply (the line is added if the agent did
not say it). The person's own later statement replaces a pin; nothing else moves it.

**The graph.** Things (what beliefs are about) with names and aliases; edges: about, refines,
opposes, exception_to, for_now (and rests-on: the links). Written only by the dream (and pin).
Reading is SQLite full-text search (FTS5, Porter stemming) on names, aliases and belief text, a
walk out over the edges, and a ranking by closeness, recency and standing, all in SQL: no model.
Another search could later be added beside `recall` (its candidates join the same ranking).

**Dreams.** One forked turn of the person's session (agents-claude-code.turn with fork and schema),
run on the engine's worker with the record since the last dream in its prompt and the memory caps
at the door to look things up. Its answer is applied here, in one transaction. A dream runs when
the conversation has been idle `memory_idle_minutes` with undreamed words, before Claude Code
compacts the session (its PreCompact hook calls agent.dream), and when asked (agent.dream).

**Storage.** A SQLite file of the plugin's own, `memory.db` in its hub data folder (Plugin.data),
which backup.run carries. Not core's records: the walk and the ranking join edges, names and
beliefs in single SQL statements, which records (one type per search, every word of a text, one
call per record) cannot do; through records each recall would read rows one call at a time.
"""

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

from rook_hub.library import Error

log = logging.getLogger("rook_hub.plugins.agents.memory")
agent = globals().get("agent")      # the house agent's hub module (hub.py), put here before the module runs

# ------------------------------------------------------------------ every number in memory
# MEM5: the only knobs are the four settings in setup(). These are the rest, from round four
# (proto/memory/knobs.toml); none refuses, cuts or ends anything.
KIND = {"correction": 1.0, "decision": 1.0, "statement": 0.6, "mention": 0.3, "unclear": 0.1}
BY_PERSON, BY_OTHER, BY_HOUSE = 1.0, 0.4, 0.0   # a person's own words, an agent's or a job's, the house's replies
FLIP_AT = 0.4           # a link this strong (or stronger) can flip a belief
MATURITY = 1.5          # certainty = 1 - exp(-summed strength of the occasions behind it / this)
BEFORE_FLIP = 0.5       # occasions from before the latest strong contrary link count this much
TENTATIVE_BELOW, MATURE_AT = 0.5, 0.6
OWN_WORDS = 0.5         # a belief found by its own words holds this share of the message's content words
OWN_WORDS_PINNED = 0.34     # ... or this share, for a pinned one
WALK_DECAY = 0.6        # closeness kept per step out from a thing the message names
PIN_MATCH = 0.6         # pin: a held belief sharing this share of content words (Dice) is the one pinned
SAME_FROM = 0.5         # the dream: a new belief this close to a held one is checked (same, opposite, different)
RECENCY = 0.999         # recency = this to the power of the hours since a belief was last handed to a turn
W_CLOSE, W_RECENT, W_SURE = 1.0, 0.5, 0.3       # the ranking: closeness, recency, |standing|
STRETCH = 150_000       # characters of record per dream call; a longer record is dreamed in several forked calls
PERSON_ABOUT = "person"     # about values: person:<name>, how, world

STOP = set("""a an the and or but if then so of to in on at by for with from about into over as is are was were be been
being am do does did done have has had having i me my mine you your yours he him his she her it its we us our they them
their this that these those there here what which who whom whose when where why how can could will would shall should may
might must just still now again also any some all no not yes please tell let lets say said go goes going get gets got
days day way ways thing things very really""".split())
KINDS = tuple(KIND)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (      -- each person's conversation, by its agent-session record
    session    TEXT PRIMARY KEY,
    person     TEXT NOT NULL,              -- person:<name>
    dreamed_to INTEGER NOT NULL DEFAULT 0, -- the last seq a dream covered
    last_at    REAL
);
CREATE TABLE IF NOT EXISTS entries (       -- the record, as the conversation's stream keeps it
    id      INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    at      REAL NOT NULL,
    speaker TEXT NOT NULL,                 -- person:<name>, agent:<token>, agent:house, job:<job>, house
    channel TEXT NOT NULL,                 -- site, door, job, reply
    text    TEXT NOT NULL,
    UNIQUE (session, seq)
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY, session TEXT, text TEXT NOT NULL, said_by TEXT NOT NULL, channel TEXT NOT NULL,
    kind TEXT NOT NULL, entries TEXT NOT NULL, happened_at REAL NOT NULL, dream INTEGER, made_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, about TEXT NOT NULL, wordings TEXT NOT NULL DEFAULT '[]',
    made_at REAL NOT NULL, dream INTEGER, last_used REAL,
    pinned INTEGER NOT NULL DEFAULT 0,     -- the standing, kept as it changes, so recall ranks in SQL
    value REAL NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '', touched REAL NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS belief_fts USING fts5(body, tokenize = 'porter unicode61');
CREATE TABLE IF NOT EXISTS belief_texts (belief INTEGER NOT NULL, text TEXT NOT NULL, at REAL NOT NULL, why TEXT);
CREATE TABLE IF NOT EXISTS links (         -- rests on: an episode for or against a belief
    id INTEGER PRIMARY KEY, belief INTEGER NOT NULL, episode INTEGER NOT NULL, stance INTEGER NOT NULL,
    strength REAL NOT NULL, occasion TEXT NOT NULL, flips INTEGER NOT NULL, at REAL NOT NULL, dream INTEGER,
    why TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS links_belief ON links (belief, at);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY, belief INTEGER NOT NULL, action TEXT NOT NULL,   -- pin, unpin, replace
    at REAL NOT NULL, session TEXT, seq INTEGER, words TEXT NOT NULL, was TEXT
);
CREATE INDEX IF NOT EXISTS pins_belief ON pins (belief, id);
CREATE INDEX IF NOT EXISTS pins_turn ON pins (session, seq);
CREATE TABLE IF NOT EXISTS things (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, made_at REAL NOT NULL, dream INTEGER, entries TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS things_made ON things (made_at);
CREATE TABLE IF NOT EXISTS names (         -- every way a thing is said: its name (how 'name'), aliases, heals
    id INTEGER PRIMARY KEY, thing INTEGER NOT NULL, name TEXT NOT NULL, words INTEGER NOT NULL, how TEXT NOT NULL,
    at REAL NOT NULL, dream INTEGER, entries TEXT NOT NULL DEFAULT '[]', UNIQUE (thing, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS name_fts USING fts5(name, tokenize = 'porter unicode61');
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY, src_kind TEXT NOT NULL, src INTEGER NOT NULL, rel TEXT NOT NULL,
    dst_kind TEXT NOT NULL, dst INTEGER NOT NULL, at REAL NOT NULL, dream INTEGER, entries TEXT NOT NULL DEFAULT '[]',
    how TEXT NOT NULL DEFAULT 'dream', why TEXT, ended_at REAL, ended TEXT,
    UNIQUE (src_kind, src, rel, dst_kind, dst)
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges (dst_kind, dst, rel);
CREATE INDEX IF NOT EXISTS edges_src ON edges (src_kind, src, rel);
CREATE TABLE IF NOT EXISTS misses (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, seq INTEGER NOT NULL, at REAL NOT NULL, query TEXT NOT NULL,
    recalled TEXT NOT NULL DEFAULT '[]', found TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open',
    dream INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS heals (
    id INTEGER PRIMARY KEY, miss INTEGER NOT NULL, at REAL NOT NULL, dream INTEGER, kind TEXT NOT NULL,
    ref INTEGER, words TEXT, why TEXT
);
CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY, belief INTEGER NOT NULL, episode INTEGER NOT NULL, claim TEXT NOT NULL,
    person TEXT NOT NULL, made_at REAL NOT NULL, dream INTEGER, shown_at REAL, shown_seq INTEGER,
    answered_at REAL, answer TEXT
);
CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, trigger TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL, state TEXT NOT NULL DEFAULT 'running', seq_from INTEGER, seq_to INTEGER, calls INTEGER DEFAULT 0,
    counts TEXT, held_back TEXT, answer TEXT, error TEXT
);
"""


# ------------------------------------------------------------------------------- the store

class Store:
    """memory.db, one connection used from the hub's loop and its threads, under one lock."""

    def __init__(self, path):
        self.path = path
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))    # owner-only, as the hub's data is (its WAL follows it)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.create_function("recency", 2, _recency, deterministic=True)
        with self.lock:
            self.db.executescript(SCHEMA)

    def all(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def run(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    def write(self):
        """`with store.write():` one transaction, under the lock."""
        return _Transaction(self)

    # -- beliefs

    def add_belief(self, text, about, at, dream=None, wordings=()):
        bid = self.run("INSERT INTO beliefs (text, about, wordings, made_at, dream, touched) VALUES (?, ?, ?, ?, ?, ?)",
                       text, about, json.dumps(list(wordings)), at, dream, at)
        self._index(bid)
        return bid

    def _index(self, bid):
        b = self.one("SELECT text, wordings FROM beliefs WHERE id = ?", bid)
        self.run("DELETE FROM belief_fts WHERE rowid = ?", bid)
        self.run("INSERT INTO belief_fts (rowid, body) VALUES (?, ?)", bid,
                 " | ".join([b["text"], *json.loads(b["wordings"])]))

    def reword(self, bid, text, at, why):
        old = self.one("SELECT text FROM beliefs WHERE id = ?", bid)["text"]
        if old != text:
            self.run("INSERT INTO belief_texts (belief, text, at, why) VALUES (?, ?, ?, ?)", bid, old, at, why)
            self.run("UPDATE beliefs SET text = ? WHERE id = ?", text, bid)
            self._index(bid)

    def add_link(self, bid, episode, stance, strength, occasion, at, dream=None, why=None, note=None, flips=True):
        self.run("INSERT INTO links (belief, episode, stance, strength, occasion, flips, at, dream, why, note)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", bid, episode, stance, strength, occasion, int(flips), at,
                 dream, why, note)
        self.refresh(bid)

    def links(self, bid):
        return self.all("SELECT l.*, e.text AS episode_text, e.said_by, e.kind, e.channel FROM links l"
                        " JOIN episodes e ON e.id = l.episode WHERE l.belief = ? ORDER BY l.at, l.id", bid)

    def last_pin(self, bid):
        return self.one("SELECT * FROM pins WHERE belief = ? ORDER BY id DESC LIMIT 1", bid)

    def pinned(self, bid):
        p = self.last_pin(bid)
        return p is not None and p["action"] != "unpin"

    def refresh(self, bid):
        """Keep the belief's standing on its row, so recall ranks by it in SQL."""
        st = self.standing(bid)
        last = self.one("SELECT MAX(at) AS at FROM links WHERE belief = ?", bid)["at"] or 0
        self.run("UPDATE beliefs SET pinned = ?, value = ?, summary = ?, touched = MAX(COALESCE(last_used, 0), ?, made_at)"
                 " WHERE id = ?", int(st["pinned"]), st["value"], summary(st), last, bid)

    def standing(self, bid, upto=None):
        links = self.links(bid)
        pins = [p for p in self.all("SELECT * FROM pins WHERE belief = ? ORDER BY id", bid)
                if upto is None or p["at"] <= upto]
        pinned = bool(pins) and pins[-1]["action"] != "unpin"
        st = standing(links, pinned=pinned, upto=upto)
        if pinned:
            since = None
            for p in pins:          # the first pin of the run of pins since the last unpin
                since = None if p["action"] == "unpin" else since if since is not None else p["at"]
            st["pinned_at"], st["pinned_words"] = since, pins[-1]["words"]
            st["against_since_pin"] = sum(1 for l in links if l["stance"] < 0 and l["at"] >= since
                                          and (upto is None or l["at"] <= upto))
        return st

    # -- the graph

    def thing_named(self, name):
        name = " ".join(str(name or "").split())
        if not name:
            return None
        return self.one("SELECT t.* FROM names n JOIN things t ON t.id = n.thing WHERE lower(n.name) = lower(?)"
                        " ORDER BY n.how != 'name', n.id LIMIT 1", name)

    def add_thing(self, name, at, dream=None, entries=()):
        tid = self.run("INSERT INTO things (name, made_at, dream, entries) VALUES (?, ?, ?, ?)",
                       name, at, dream, json.dumps(list(entries)))
        self.add_name(tid, name, at, dream, entries, how="name")
        return tid

    def add_name(self, tid, name, at, dream=None, entries=(), how="alias"):
        name = " ".join(str(name or "").split())
        if not name or not words(name):
            return None
        if self.one("SELECT 1 FROM names WHERE thing = ? AND lower(name) = lower(?)", tid, name):
            return None
        nid = self.run("INSERT INTO names (thing, name, words, how, at, dream, entries) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       tid, name, len(words(name)), how, at, dream, json.dumps(list(entries)))
        self.run("INSERT INTO name_fts (rowid, name) VALUES (?, ?)", nid, name)
        return nid

    def add_edge(self, src_kind, src, rel, dst_kind, dst, at, dream=None, entries=(), how="dream", why=None):
        if self.one("SELECT 1 FROM edges WHERE src_kind = ? AND src = ? AND rel = ? AND dst_kind = ? AND dst = ?",
                    src_kind, src, rel, dst_kind, dst):
            return None
        return self.run("INSERT INTO edges (src_kind, src, rel, dst_kind, dst, at, dream, entries, how, why)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", src_kind, src, rel, dst_kind, dst, at, dream,
                        json.dumps(list(entries)), how, why)


class _Transaction:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.db.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, kind, *rest):
        try:
            self.store.db.execute("COMMIT" if kind is None else "ROLLBACK")
        finally:
            self.store.lock.release()


def _recency(touched, now):
    hours = max(0.0, ((now or 0) - (touched or 0)) / 3600)
    return RECENCY ** hours


def words(text):
    """The content words of a text, once each, in order."""
    return list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 1))


def _stem(w):
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[:-len(suffix)]
    return w


def dice(a, b):
    """The share of content words two texts have in common."""
    A, B = {_stem(w) for w in words(a)}, {_stem(w) for w in words(b)}
    return 2 * len(A & B) / (len(A) + len(B)) if A and B else 0.0


def _now(call):
    """The hub's clock (Hub(clock=...)), so a test or a made-up run can walk through days."""
    return call._hub.clock()


def _when(at):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at or 0))


def _occasion(at):
    return time.strftime("%Y-%m-%d", time.localtime(at))


# --------------------------------------------------------------------------------- standing

def strength(kind, said_by):
    """kind x who said it. A person's own words weigh 1, an agent's or a job's 0.4, the house's 0."""
    who = BY_HOUSE if said_by in ("house", "agent:house") else BY_PERSON if said_by.startswith("person:") else BY_OTHER
    return KIND.get(kind, KIND["mention"]) * who


def strong(value):
    return value >= FLIP_AT - 1e-9


def _cert(total):
    return 1.0 - math.exp(-total / MATURITY)


def standing(links, pinned=False, upto=None):
    """A belief's standing from its links (round two's rules): the latest strong link that may flip
    says which way; the occasions behind that way say how sure; a pin stands at +1."""
    links = sorted((l for l in links if upto is None or l["at"] <= upto), key=lambda l: (l["at"], l["id"]))
    counted = [l for l in links if l["strength"] > 0]
    firm = [l for l in counted if strong(l["strength"]) and l["flips"]]
    out = {"pinned": pinned, "links": len(links), "flips": sum(1 for a, b in zip(firm, firm[1:]) if a["stance"] != b["stance"]),
           "direction": 0, "certainty": 0.0, "contrary": 0.0, "occasions": 0, "contrary_occasions": 0,
           "conflict": False, "weak_against_since": 0}
    if counted:
        d = (firm or counted)[-1]["stance"]
        last_contrary = max((l["at"] for l in firm if l["stance"] != d), default=-1)
        mine, theirs = {}, {}
        for l in counted:
            pool = mine if l["stance"] == d else theirs
            weight = l["strength"] * (BEFORE_FLIP if pool is mine and l["at"] <= last_contrary else 1)
            pool[l["occasion"]] = max(pool.get(l["occasion"], 0.0), weight)
        last_firm = firm[-1]["at"] if firm else -1
        out.update(direction=d, certainty=_cert(sum(mine.values())), contrary=_cert(sum(theirs.values())),
                   occasions=len(mine), contrary_occasions=len(theirs),
                   weak_against_since=sum(1 for l in counted if l["stance"] != d and l["at"] >= last_firm
                                          and not (strong(l["strength"]) and l["flips"])))
        out["conflict"] = out["certainty"] < TENTATIVE_BELOW and out["contrary"] >= MATURE_AT
    if pinned:
        out.update(value=1.0, label="pinned", conflict=False)
    else:
        out["value"] = out["direction"] * out["certainty"]
        out["label"] = label(out)
    return out


def label(s):
    if not s["direction"]:
        return "no evidence"
    word = "holds" if s["direction"] > 0 else "does not hold"
    grade = ("tentative" if s["certainty"] < TENTATIVE_BELOW else "mature" if s["certainty"] >= MATURE_AT
             else "settling")
    text = f"{word}, {grade}"
    if s["conflict"]:
        text += "; ask before acting: a fresh one-off against a mature '" + \
                ("holds" if s["direction"] < 0 else "does not hold") + "'"
    return text


def _n(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def summary(s):
    """One line: the standing, the occasions behind it, its history."""
    if s["pinned"]:
        against = s.get("against_since_pin", 0)
        return "PINNED" + (f" ({_n(against, 'link')} against since, not counted)" if against else "")
    parts = [f"{s['label']} ({s['value']:+.2f}; {_n(s['occasions'], 'occasion')})"]
    if s["contrary_occasions"]:
        parts.append(f"{_n(s['contrary_occasions'], 'contrary occasion')} ({s['contrary']:.2f})")
    if s["weak_against_since"]:
        parts.append(f"{_n(s['weak_against_since'], 'weak link')} against since (too weak to flip it)")
    if s["flips"]:
        parts.append(f"flipped {_n(s['flips'], 'time')}")
    return "; ".join(parts)


# ----------------------------------------------------------------------------------- recall

def _match_sql(table, terms):
    """One statement: each row of an FTS table and how many of `terms` it holds."""
    union = " UNION ALL ".join(f"SELECT rowid FROM {table} WHERE {table} MATCH ?" for _ in terms)
    return f"SELECT rowid AS id, COUNT(*) AS n FROM ({union}) GROUP BY rowid", [f'"{t}"' for t in terms]


def match_things(store, terms):
    """{thing: the name that matched} for every thing one of whose names has all its content words in the message."""
    if not terms:
        return {}
    sql, args = _match_sql("name_fts", terms)
    hit = {}
    for r in store.all(f"SELECT n.thing, n.name FROM ({sql}) m JOIN names n ON n.id = m.id WHERE m.n >= n.words"
                       " ORDER BY n.how != 'name', n.id", *args):
        hit.setdefault(r["thing"], r["name"])
    return hit


def candidates(store, terms, hops, things):
    """[(belief, closeness, how it was reached)]: through the things named (walked out `hops` steps over
    part_of and related edges, WALK_DECAY a step), and by the beliefs' own words. All in SQL."""
    out = {}
    if things:
        seed = " UNION ALL ".join("SELECT ? AS thing, ? AS via" for _ in things)
        args = [x for tid, name in things.items() for x in (tid, f"'{name}'")]
        # CROSS JOIN keeps SQLite's order: the walk first, then each step's edges by index.
        rows = store.all(f"""
            WITH RECURSIVE reach(thing, depth, via) AS (
                SELECT thing, 0, via FROM ({seed})
                UNION
                SELECT e.dst, r.depth + 1, r.via || ' -> ' || e.rel || ' t' || e.dst FROM reach r CROSS JOIN edges e
                  ON e.src_kind = 'thing' AND e.src = r.thing AND e.dst_kind = 'thing' AND e.rel IN ('part_of', 'related')
                  WHERE r.depth < ?
                UNION
                SELECT e.src, r.depth + 1, r.via || ' -> ' || e.rel || ' t' || e.src FROM reach r CROSS JOIN edges e
                  ON e.dst_kind = 'thing' AND e.dst = r.thing AND e.src_kind = 'thing' AND e.rel IN ('part_of', 'related')
                  WHERE r.depth < ?)
            SELECT e.src AS belief, MIN(r.depth) AS depth, r.via FROM reach r CROSS JOIN edges e
              ON e.dst_kind = 'thing' AND e.dst = r.thing AND e.rel = 'about' AND e.src_kind = 'belief'
            GROUP BY e.src""", *args, hops, hops)
        for r in rows:
            out[r["belief"]] = (WALK_DECAY ** r["depth"], f"about {r['via']}")
    if terms:
        sql, args = _match_sql("belief_fts", terms)
        for r in store.all(sql, *args):
            c = r["n"] / len(terms)
            if c >= OWN_WORDS_PINNED and c > out.get(r["id"], (0,))[0]:
                out[r["id"]] = (c, f"its own words ({c:.0%} of the message's)")
    return out


def recall(store, text, now, hops, many, exclude=()):
    """What memory hands a turn for `text`: {beliefs: [...], things, thin, hint}. Every step is a lookup in
    SQL whose cost grows with what matches, not with what is held."""
    terms = words(text)
    things = match_things(store, terms)
    found = {b: v for b, v in candidates(store, terms, hops, things).items() if b not in exclude}
    if not found:
        ranked = []
    else:
        seed = " UNION ALL ".join("SELECT ? AS id, ? AS c, ? AS via" for _ in found)
        args = [x for b, (c, via) in found.items() for x in (b, c, via)]
        ranked = store.all(f"""
            SELECT b.*, f.c AS closeness, f.via,
                   ? * f.c + ? * recency(b.touched, ?) + ? * abs(b.value) AS score
            FROM ({seed}) f JOIN beliefs b ON b.id = f.id
            WHERE f.c >= ? OR (b.pinned AND f.c >= ?)
            ORDER BY score DESC, b.id LIMIT ?""", W_CLOSE, W_RECENT, now, W_SURE, *args, OWN_WORDS, OWN_WORDS_PINNED,
                           many)
    ranked = [dict(r) for r in ranked]
    have = {b["id"] for b in ranked}
    beliefs = []
    # A narrower or for-now belief and the standing one it sits beside come together, the narrower
    # first; opposites come along, marked.
    for b in ranked:
        group = [b]
        for e in store.all("SELECT * FROM edges WHERE src_kind = 'belief' AND src = ? AND dst_kind = 'belief' AND"
                           " rel IN ('exception_to', 'for_now', 'opposes') AND ended_at IS NULL UNION ALL"
                           " SELECT * FROM edges WHERE dst_kind = 'belief' AND dst = ? AND src_kind = 'belief' AND"
                           " rel IN ('exception_to', 'for_now', 'opposes') AND ended_at IS NULL", b["id"], b["id"]):
            other = e["dst"] if e["src"] == b["id"] else e["src"]
            word = {"exception_to": "an exception to", "for_now": "for now, beside", "opposes": "the opposite of"}[e["rel"]]
            if e["src"] == b["id"] and e["rel"] != "opposes":
                b["beside"] = f"{word} b{other}"
            if other in have:
                continue
            row = dict(store.one("SELECT * FROM beliefs WHERE id = ?", other))
            row["closeness"] = b["closeness"]
            row["via"] = (f"{word} b{b['id']}" if e["src"] == other or e["rel"] == "opposes"
                          else f"the standing belief b{b['id']} is {word}")
            if e["src"] == other and e["rel"] != "opposes":
                row["beside"] = f"{word} b{b['id']}"
            group.append(row)
            have.add(other)
        beliefs += sorted(group, key=lambda x: 0 if x.get("beside") else 1)
    thin = not any(str(b.get("via", "")).startswith("about ") for b in beliefs)
    hint = nearest_things(store, terms, many, set(things)) if thin else []
    return {"beliefs": beliefs, "things": things, "thin": thin, "hint": hint, "terms": terms}


def nearest_things(store, terms, many, skip=()):
    """The things nearest the message by name: those sharing most of its words, else the newest held."""
    rows = []
    if terms:
        sql, args = _match_sql("name_fts", terms)
        rows = store.all(f"SELECT n.thing, MAX(m.n) AS n FROM ({sql}) m JOIN names n ON n.id = m.id GROUP BY n.thing"
                         " ORDER BY n DESC, n.thing LIMIT ?", *args, many + len(skip))
    if not rows:
        rows = store.all("SELECT id AS thing FROM things ORDER BY made_at DESC, id DESC LIMIT ?", many + len(skip))
    out = []
    for r in rows:
        if r["thing"] in skip or len(out) >= many:
            continue
        out.append(_thing_line(store, r["thing"]))
    return out


def _thing_line(store, tid):
    t = store.one("SELECT * FROM things WHERE id = ?", tid)
    others = [n["name"] for n in store.all("SELECT name FROM names WHERE thing = ? AND how != 'name' ORDER BY id", tid)]
    return f"[t{tid}] {t['name']}" + (f" (also: {'; '.join(others)})" if others else "")


def rests_on(store, bid):
    """Who a belief rests on: for each speaker and way, how many occasions and the latest of them."""
    groups = {}
    for l in store.links(bid):
        key = (l["said_by"], l["stance"])
        g = groups.setdefault(key, {"n": set(), "last": None})
        g["n"].add(l["occasion"])
        g["last"] = l
    out = []
    for (who, stance), g in groups.items():
        l = g["last"]
        out.append(f"{'+' if stance > 0 else '-'} {who}, {_n(len(g['n']), 'occasion')}, latest {_when(l['at'])[:10]}"
                   f" ({l['kind']}, {l['strength']:.2f}): {l['episode_text']}" + (f" ({l['note']})" if l["note"] else ""))
    return out


def belief_lines(store, b, person=None):
    head = f"[b{b['id']}] {b['summary'] or 'no evidence'}"
    if b["pinned"]:
        p = store.last_pin(b["id"])
        head += f", pinned {_when(p['at'])[:10]} in the words \"{p['words']}\""
    if b.get("beside"):
        head += f"; {b['beside']} (beside it, never against it)"
    if b.get("via"):
        head += f"; found by {b['via']}"
    ask = store.one("SELECT * FROM asks WHERE belief = ? AND answered_at IS NULL ORDER BY id DESC", b["id"])
    if ask:
        head += "; an agent said it no longer holds: ask before acting on it"
    lines = [head, f"    {b['text']}  (about {b['about']})"]
    lines += [f"    {x}" for x in rests_on(store, b["id"])]
    return lines


def for_turn(store, r):
    """The memory block a turn is given under its message."""
    if not r["beliefs"]:
        out = ["## Memory", "Nothing held is close to this message. Before you say you don't remember anything it asks "
               "about, search memory in your own words (agent.memory.search); say you don't remember only if that "
               "finds nothing either."]
    else:
        out = ["## Memory", "What memory holds for this message, closest first (+ for, - against; ids for "
               "agent.memory.pin, unpin and show):"]
        for b in r["beliefs"]:
            out += belief_lines(store, b)
        if r["thin"]:
            out.append("None of these was reached through a thing the message names: if they do not answer it, search "
                       "memory in your own words before you say you don't remember.")
    if r["hint"]:
        out.append("Things memory holds nearest this message by name: " + "; ".join(r["hint"]))
    return "\n".join(out)


# ---------------------------------------------------------------------------------- the store per hub

_store = None
_turning = {}        # a turn's door token -> {session, seq, speaker, person, recalled}
_dreaming = set()    # the door tokens of dreams running now
_idle = {}           # session -> the task waiting for it to go idle
_armed = False       # the idle waits are armed after a start (the first call that runs on the loop)


def store(call):
    global _store
    if _store is None:
        _store = Store(call.data / "memory.db")
    return _store


def on(call):
    return bool(call.settings["memory"])


def _person_of(call, sess):
    return sess["body"]["party"]


def heard(call, sess, seq, speaker, channel, text, at):
    """An entry of the record: a message or a reply. Kept even with memory off, so the first dream
    after it is turned on reads all of it."""
    s = store(call)
    with s.write():
        s.run("INSERT OR IGNORE INTO sessions (session, person) VALUES (?, ?)", sess["guid"], _person_of(call, sess))
        s.run("INSERT OR IGNORE INTO entries (session, seq, at, speaker, channel, text) VALUES (?, ?, ?, ?, ?, ?)",
              sess["guid"], seq, at, speaker, channel, text)
        s.run("UPDATE sessions SET last_at = ? WHERE session = ?", at, sess["guid"])


def backfill(call):
    """Entries the index lacks (a hub that had a conversation before memory, or a restart between a
    message and its index line), from the conversations' streams: the first dream reads all of it."""
    for sess in agent._sessions(call):
        s = store(call)
        have = s.one("SELECT MAX(seq) AS seq FROM entries WHERE session = ?", sess["guid"])["seq"] or 0
        with call._hub._lock:
            rows = call._hub._db.execute("SELECT position, data FROM stream_data WHERE stream = ? AND position >= ?"
                                         " ORDER BY position", (sess["body"]["stream"], have)).fetchall()
        for row in rows:
            got = json.loads(row["data"])
            data = got.get("data") or {}
            if got.get("type") == "input":
                heard(call, sess, row["position"] + 1, data.get("from") or got.get("by"), data.get("channel", "site"),
                      data.get("text", ""), got.get("at") or 0)
            elif got.get("type") == "turn" and data.get("role") == "reply":
                heard(call, sess, row["position"] + 1, "house", "reply", data.get("text", ""), got.get("at") or 0)


async def before_turn(call, sess, seq, speaker, text, token):
    """The memory block for this turn's prompt (recall, the pending ask, the hint), and the turn's
    context for the memory caps it calls."""
    arm(call)
    person = _person_of(call, sess)
    _turning[token] = {"session": sess["guid"], "seq": seq, "speaker": speaker, "person": person, "recalled": []}
    if not on(call):
        return ""
    s, now = store(call), _now(call)
    r = await asyncio.to_thread(recall, s, text, now, int(call.settings["memory_hops"]),
                                int(call.settings["memory_beliefs"]))
    tags = [f"b{b['id']}" for b in r["beliefs"]]
    _turning[token]["recalled"] = tags
    with s.write():
        if speaker == person:      # what a person's own turn was handed counts as used
            for b in r["beliefs"]:
                s.run("UPDATE beliefs SET last_used = ?, touched = MAX(touched, ?) WHERE id = ?", now, now, b["id"])
        if not r["beliefs"]:
            s.run("INSERT INTO misses (session, seq, at, query) VALUES (?, ?, ?, ?)", sess["guid"], seq, now, text)
    block = for_turn(s, r)
    if speaker == person:          # an agent said something held is no longer so: asked once, on their own turn
        claims = {}                # one question per claim, however many beliefs it is against
        for ask in s.all("SELECT a.*, b.text FROM asks a JOIN beliefs b ON b.id = a.belief WHERE a.person = ? AND"
                         " a.shown_at IS NULL AND a.answered_at IS NULL ORDER BY a.id", person):
            claims.setdefault(ask["episode"], []).append(ask)
            s.run("UPDATE asks SET shown_at = ?, shown_seq = ? WHERE id = ?", now, seq, ask["id"])
        for asks in claims.values():
            against = "; ".join(f"[b{a['belief']}] \"{a['text']}\"" for a in asks)
            block += (f"\n\nTo ask {person.removeprefix('person:')} (once, briefly, in this reply; do not raise it again): "
                      f"{asks[0]['claim']} That is against {against}. Ask whether it still holds; their answer decides.")
    return "\n\n" + block


def after_turn(call, sess, seq, token, reply):
    """The reply as it goes out: every pin, unpin and replacement of this turn said in it (the line
    added when the agent did not say it)."""
    s = store(call)
    lines = []
    for p in s.all("SELECT * FROM pins WHERE session = ? AND seq = ? ORDER BY id", sess["guid"], seq):
        if p["action"] == "replace" and not re.search(r"replac", reply, re.I):
            lines.append(f"(That replaces what was pinned: \"{p['was']}\". Pinned now: \"{p['words']}\")")
        elif p["action"] != "replace" and not re.search(r"pin", reply, re.I):
            lines.append(f"(Pinned: \"{p['words']}\")" if p["action"] == "pin" else f"(Unpinned: \"{p['words']}\")")
    if lines:
        reply = reply.rstrip() + "\n\n" + "\n".join(lines)
    return reply


def turn_ended(token):
    _turning.pop(token, None)


def replied(call, sess, seq, reply, at):
    """The reply in the record, and the wait for the conversation to go idle started again."""
    heard(call, sess, seq, "house", "reply", reply, at)
    if on(call):
        _wait_idle(call, sess)


# ---------------------------------------------------------------------------------- the dream's triggers

def arm(call):
    """After a start, wait for every conversation with undreamed words to go idle (once)."""
    global _armed
    if _armed or not on(call):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _armed = True
    for sess in agent._sessions(call):
        s = store(call)
        row = s.one("SELECT dreamed_to FROM sessions WHERE session = ?", sess["guid"])
        if row and s.one("SELECT 1 FROM entries WHERE session = ? AND seq > ?", sess["guid"], row["dreamed_to"]):
            _wait_idle(call, sess)


def _wait_idle(call, sess):
    """Dream once the conversation has been idle memory_idle_minutes (a new entry starts the wait again)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    del loop
    old = _idle.pop(sess["guid"], None)
    if old is not None:
        old.cancel()
    runner = call.as_caller(sess["owner"])

    async def wait():
        while True:
            last = store(runner).one("SELECT last_at FROM sessions WHERE session = ?", sess["guid"])["last_at"] or 0
            left = float(runner.settings["memory_idle_minutes"]) * 60 - (_now(runner) - last)
            if left <= 0:
                break
            await asyncio.sleep(left)
        if _idle.get(sess["guid"]) is task:
            _idle.pop(sess["guid"], None)
        await dream(runner, sess["guid"], "idle")
    task = call.later(f"idle {sess['guid']}", wait)     # ended with the plugins' other work when the hub stops
    _idle[sess["guid"]] = task


async def dream(call, session_guid, trigger):
    """Run one dream of the conversation, after any other of the same conversation (Call.later)."""
    return await call.later(f"dream {session_guid}", lambda: _dream(call, session_guid, trigger))


async def _dream(call, session_guid, trigger):
    s = store(call)
    sess = call.get(session_guid)
    row = s.one("SELECT * FROM sessions WHERE session = ?", session_guid)
    entries = s.all("SELECT * FROM entries WHERE session = ? AND seq > ? ORDER BY seq", session_guid,
                    row["dreamed_to"] if row else 0)
    if not entries:
        return {"dream": None, "note": "nothing new to dream over"}
    did = s.run("INSERT INTO dreams (session, trigger, started_at, seq_from, seq_to) VALUES (?, ?, ?, ?, ?)",
                session_guid, trigger, _now(call), entries[0]["seq"], entries[-1]["seq"])
    token = None
    try:
        house = agent.house(call)
        backend, worker = agent.engine(call, house)
        same = sess["body"].get("harness_backend") == backend
        harness = sess["body"].get("harness") if same else None
        token = await call.call("hub", "token.create", {"purpose": "agent"},
                                f"The door token of dream {did} of the house agent's memory; revoked when it ends.")
        _dreaming.add(token["token"])
        person = _person_of(call, sess)
        merged, calls = {}, 0
        for part in _stretches(entries):
            args = {"prompt": dream_prompt(s, person, part, _now(call)), "system": agent.system_prompt(call, house),
                    "schema": dream_schema(person, part), "door": token["token"]}
            if harness:
                args.update(session=harness, fork=True)
            got = await call.call(worker, backend, args, f"Dream {did} of the house agent's memory ({trigger}): "
                                                         f"messages {part[0]['seq']} to {part[-1]['seq']}.")
            calls += 1
            answer = got.get("structured") if isinstance(got, dict) else None
            if not isinstance(answer, dict):
                raise Error("no_dream", "the engine gave no structured answer", "Read the journal for the dream's call.")
            for key, value in answer.items():
                if isinstance(value, list):
                    merged.setdefault(key, []).extend(value)
        pairs = await asyncio.to_thread(_pending_pairs, s, person, merged, entries)
        verdicts = {}
        if pairs:
            got = await call.call(worker, backend, {"prompt": same_prompt(pairs), "schema": SAME}, f"Dream {did}: "
                                  "whether the new beliefs say the same as ones already held.")
            calls += 1
            for v in (got.get("structured") or {}).get("verdicts") or []:
                if isinstance(v, dict) and isinstance(v.get("pair"), int):
                    verdicts[v["pair"]] = v.get("verdict")
        counts = await asyncio.to_thread(apply, s, did, session_guid, person, entries, merged, verdicts, pairs,
                                         _now(call))
        s.run("UPDATE dreams SET state = 'done', ended_at = ?, calls = ?, counts = ?, held_back = ?, answer = ?"
              " WHERE id = ?", _now(call), calls, json.dumps({k: v for k, v in counts.items() if k != "held_back"}),
              json.dumps(counts["held_back"]), json.dumps(merged), did)
        return {"dream": did, **counts}
    except Exception as e:     # journaled (the engine's call is); the words stay undreamed for the next trigger
        message = e.message["message"] if isinstance(e, Error) else f"{type(e).__name__}: {e}"
        if not isinstance(e, Error):
            log.exception("Dream %s failed", did)
        s.run("UPDATE dreams SET state = 'failed', ended_at = ?, error = ? WHERE id = ?", _now(call), message, did)
        return {"dream": did, "error": message}
    finally:
        if token is not None:
            _dreaming.discard(token["token"])
            try:
                await call.call("hub", "token.revoke", {"id": token["moniker"]}, "The dream ended: its door token goes.")
            except Error as e:
                log.warning("door token %s not revoked: %s", token["moniker"], e.message["message"])


def _stretches(entries):
    run, size = [], 0
    for e in entries:
        n = len(e["text"]) + 60
        if run and size + n > STRETCH:
            yield run
            run, size = [], 0
        run.append(e)
        size += n
    if run:
        yield run


def listing(entries):
    return "\n".join(f"[s{e['seq']}] {_when(e['at'])} {e['speaker']} ({e['channel']}): {e['text']}" for e in entries)


def misses_listing(s, session, entries):
    seqs = [e["seq"] for e in entries]
    rows = s.all(f"SELECT * FROM misses WHERE session = ? AND status = 'open' AND seq IN ({','.join('?' * len(seqs))})"
                 " ORDER BY id", session, *seqs)
    out = []
    for m in rows:
        found, recalled = json.loads(m["found"]), json.loads(m["recalled"])
        out.append(f"[m{m['id']}] at s{m['seq']}: \"{m['query']}\"; automatic recall gave {', '.join(recalled) or 'no beliefs'}"
                   + (f"; the house agent's own search then found {', '.join(found)}" if found else ""))
    return "\n".join(out) or "(none)"


def dream_prompt(s, person, entries, now):
    session = s.one("SELECT session FROM entries WHERE id = ?", entries[0]["id"])["session"]
    return DREAM.format(person=person, name=person.removeprefix("person:"), now=_when(now),
                        entries=listing(entries), misses=misses_listing(s, session, entries))


def dream_schema(person, entries):
    speakers = sorted({e["speaker"] for e in entries})
    nb = {"type": "object", "required": ["belief", "about"], "properties": {
        "belief": {"type": "string"}, "about": {"type": "string", "enum": [person, "how", "world"]},
        "opposite_of": {"type": ["string", "null"]}, "other_wordings": {"type": "array", "items": {"type": "string"}},
        "about_things": {"type": "array", "items": {"type": "string"}}, "refines": {"type": ["string", "null"]},
        "exception_to": {"type": ["string", "null"]}, "for_now_beside": {"type": ["string", "null"]}}}
    strs = {"type": "array", "items": {"type": "string"}}
    return {"type": "object", "required": ["facts"], "properties": {
        "facts": {"type": "array", "items": {"type": "object", "required": ["fact", "entries", "said_by", "kind", "held"],
                                             "properties": {
            "fact": {"type": "string"}, "entries": strs, "said_by": {"type": "string", "enum": speakers},
            "kind": {"type": "string", "enum": list(KINDS)},
            "held": {"type": "array", "items": {"type": "object", "required": ["belief", "stance"], "properties": {
                "belief": {"type": "string"}, "stance": {"type": "string", "enum": ["supports", "against"]},
                "why": {"type": "string"}}}},
            "new_belief": {"anyOf": [{"type": "null"}, nb]}}}},
        "things": {"type": "array", "items": {"type": "object", "required": ["name", "aliases"], "properties": {
            "name": {"type": "string"}, "id": {"type": ["string", "null"]}, "aliases": strs, "entries": strs}}},
        "about": {"type": "array", "items": {"type": "object", "required": ["belief", "thing"], "properties": {
            "belief": {"type": "string"}, "thing": {"type": "string"}}}},
        "relations": {"type": "array", "items": {"type": "object", "required": ["thing", "rel", "other"], "properties": {
            "thing": {"type": "string"}, "rel": {"type": "string", "enum": ["part_of", "related"]},
            "other": {"type": "string"}, "entries": strs}}},
        "heals": {"type": "array", "items": {"type": "object", "required": ["miss", "thing", "alias"], "properties": {
            "miss": {"type": "string"}, "thing": {"type": "string"}, "alias": {"type": "string"},
            "belief": {"type": ["string", "null"]}, "why": {"type": "string"}}}},
        "nothing_held": strs,
        "settle": {"type": "array", "items": {"type": "object", "required": ["belief", "how"], "properties": {
            "belief": {"type": "string"}, "how": {"type": "string", "enum": ["lasting", "lapsed"]}}}},
        "changes": {"type": "array", "items": {"type": "object", "required": ["belief", "now", "entries"], "properties": {
            "belief": {"type": "string"}, "now": {"type": "string"}, "entries": strs, "why": {"type": "string"}}}}}}


SAME = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["pair", "verdict"], "properties": {
        "pair": {"type": "integer"}, "verdict": {"type": "string", "enum": ["same", "opposite", "different"]}}}}}}


def same_prompt(pairs):
    lines = "\n".join(f'{k}. new: "{new}"\n   held: "{held}"' for k, (_, _, new, held) in enumerate(pairs))
    return SAME_PROMPT.format(pairs=lines)


# ---------------------------------------------------------------------------------- applying a dream

def _ids(tags, prefix):
    out = []
    for t in tags or []:
        t = str(t).strip().strip("[]").lower()
        if t.startswith(prefix) and t[1:].isdigit():
            out.append(int(t[1:]))
    return out


def _belief(s, tag):
    if tag is None:
        return None
    ids = _ids([tag], "b")
    if ids:
        return s.one("SELECT * FROM beliefs WHERE id = ?", ids[0])
    return s.one("SELECT * FROM beliefs WHERE lower(text) = lower(?)", str(tag).strip())


def _new_beliefs(merged):
    for n, f in enumerate(merged.get("facts") or []):
        nb = f.get("new_belief") if isinstance(f, dict) else None
        if isinstance(nb, dict) and str(nb.get("belief") or "").strip():
            yield n, nb


def _held_close(s, text):
    """Held beliefs close to `text` (Dice at SAME_FROM or more), from the ones sharing a word with it (FTS)."""
    terms = words(text)
    if not terms:
        return []
    sql, args = _match_sql("belief_fts", terms)
    rows = s.all(f"SELECT b.id, b.text FROM ({sql}) m JOIN beliefs b ON b.id = m.id", *args)
    return [r for r in rows if dice(text, r["text"]) >= SAME_FROM]


def _pending_pairs(s, person, merged, entries):
    """[(fact index, ("b", id) or ("n", fact index), new text, other text)]: each new belief the dream proposes
    against the held ones and the earlier new ones it is close to, for the same check."""
    out, earlier = [], []
    for n, nb in _new_beliefs(merged):
        text = nb["belief"].strip()
        skip = {x["id"] for x in [_belief(s, nb.get(k)) for k in ("opposite_of", "exception_to", "for_now_beside")] if x}
        for r in _held_close(s, text):
            if r["id"] not in skip:
                out.append((n, ("b", r["id"]), text, r["text"]))
        for m, other in earlier:
            if dice(text, other) >= SAME_FROM:
                out.append((n, ("n", m), text, other))
        earlier.append((n, text))
    return out


def apply(s, did, session, person, entries, answer, verdicts=None, pairs=(), now=None):
    """Write what the dream answered, by the record's rules, in one transaction. Who said it is the
    record's: the dream names a speaker, but only one of the entries it cites counts. `pairs` and
    `verdicts`: the same check (_pending_pairs, and what the engine said of each pair)."""
    with s.write():
        return _apply(s, did, session, person, entries, answer, verdicts or {}, list(pairs),
                      time.time() if now is None else now)


def _apply(s, did, session, person, entries, answer, verdicts, pairs, now):
    mine = {f"s{e['seq']}": e for e in entries}
    held_back, made = [], {}
    count = {"facts": 0, "links": 0, "new": 0, "same": 0, "opposite": 0, "things": 0, "things_reused": 0, "aliases": 0,
             "edges": 0, "heals": 0, "asks": 0, "answers": 0, "changes": 0}

    def cited(tags):
        return [mine[f"s{i}"] for i in _ids(tags, "s") if f"s{i}" in mine]

    def link(b, ep, stance, weight, said_by, occ, why, kind):
        note = None
        # An agent's or a job's word never flips a belief (their decision on how something works does); when
        # they say outright that something held is no longer so, the person is asked.
        flips = said_by.startswith("person:") or (b["about"] == "how" and kind == "decision")
        if b["about"].startswith(PERSON_ABOUT + ":") and said_by != b["about"]:
            weight, note = 0.0, f"not {b['about']}'s own words: recorded, not counted"
        elif weight <= 0:
            note = f"{said_by}'s words: recorded, not counted"
        elif s.pinned(b["id"]) and stance < 0:
            note = "pinned: recorded, does not move it"
        elif not flips:
            note = f"{said_by}'s word: recorded, cannot flip it; {person} is asked"
        elif not strong(weight):
            note = "weak: recorded, cannot flip it"
        s.add_link(b["id"], ep, stance, weight, occ, now, did, why=why, note=note, flips=flips)
        count["links"] += 1
        if (stance < 0 and not flips and said_by != "house" and not said_by.startswith("person:")
                and kind in ("correction", "decision", "statement")
                and not s.one("SELECT 1 FROM asks WHERE belief = ? AND answered_at IS NULL", b["id"])):
            claim = s.one("SELECT text FROM episodes WHERE id = ?", ep)["text"]
            s.run("INSERT INTO asks (belief, episode, claim, person, made_at, dream) VALUES (?, ?, ?, ?, ?, ?)",
                  b["id"], ep, claim, person, now, did)
            count["asks"] += 1
        if said_by == person:
            ask = s.one("SELECT * FROM asks WHERE belief = ? AND person = ? AND shown_at IS NOT NULL AND answered_at IS NULL"
                        " ORDER BY id DESC", b["id"], person)
            if ask:
                s.run("UPDATE asks SET answered_at = ?, answer = ? WHERE id = ?", now, "flipped" if stance < 0 else "kept",
                      ask["id"])
                count["answers"] += 1

    episodes = {}
    for n, f in enumerate(answer.get("facts") or []):
        if not isinstance(f, dict):
            continue
        text, srcs = str(f.get("fact") or "").strip(), cited(f.get("entries"))
        if not text:
            continue
        if not srcs:
            held_back.append({"fact": text, "why": "names no entry of this dream's record"})
            continue
        speakers = [e["speaker"] for e in srcs]
        said_by = f.get("said_by") if f.get("said_by") in speakers else speakers[0]
        srcs = [e for e in srcs if e["speaker"] == said_by] or srcs
        kind = f.get("kind") if f.get("kind") in KINDS else "mention"
        at = min(e["at"] for e in srcs)
        channel = srcs[0]["channel"]
        ep = s.run("INSERT INTO episodes (session, text, said_by, channel, kind, entries, happened_at, dream, made_at)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", session, text, said_by, channel, kind,
                   json.dumps([e["id"] for e in srcs]), at, did, now)
        episodes[n] = (ep, said_by, kind, at, [e["id"] for e in srcs])
        count["facts"] += 1
        weight, occ = strength(kind, said_by), _occasion(at)
        nb = f.get("new_belief") if isinstance(f.get("new_belief"), dict) else None
        beside = {x["id"] for x in [_belief(s, (nb or {}).get(k)) for k in ("exception_to", "for_now_beside")] if x}
        for h in f.get("held") or []:
            b = _belief(s, h.get("belief")) if isinstance(h, dict) else None
            if not b:
                held_back.append({"fact": text, "why": f"links to {h!r}, which is no belief"})
                continue
            if b["id"] in beside and h.get("stance") == "against":
                held_back.append({"fact": text, "why": f"against b{b['id']}, which it sits beside (an exception or for now)"})
                continue
            link(b, ep, -1 if h.get("stance") == "against" else 1, weight, said_by, occ, h.get("why"), kind)
        if not nb or not str(nb.get("belief") or "").strip():
            continue
        about = nb.get("about") if nb.get("about") in (person, "how", "world") else "world"
        if about == person and said_by != person:
            held_back.append({"fact": text, "belief": nb["belief"], "why": f"about what {person} wants, from {said_by}'s words"})
            continue
        if weight <= 0:
            held_back.append({"fact": text, "belief": nb["belief"], "why": f"{said_by}'s words count for nothing"})
            continue
        same = [ref for k, (i, ref, _, _) in enumerate(pairs) if i == n and verdicts.get(k) == "same"]
        opposite = [ref for k, (i, ref, _, _) in enumerate(pairs) if i == n and verdicts.get(k) == "opposite"]
        resolve = lambda ref: ref[1] if ref[0] == "b" else made.get(ref[1])
        if same and resolve(same[0]):
            b = s.one("SELECT * FROM beliefs WHERE id = ?", resolve(same[0]))
            link(b, ep, 1, weight, said_by, occ, "says the same as the new belief the dream proposed", kind)
            count["same"] += 1
            made[n] = b["id"]
            continue
        bid = s.add_belief(nb["belief"].strip(), about, now, did,
                           [str(w).strip() for w in nb.get("other_wordings") or [] if str(w).strip()])
        s.add_link(bid, ep, 1, weight, occ, now, did, why="made from this")
        made[n] = bid
        count["new"] += 1
        count["links"] += 1
        for o in [_belief(s, nb.get("opposite_of"))] + [s.one("SELECT * FROM beliefs WHERE id = ?", resolve(r))
                                                        for r in opposite if resolve(r)]:
            if o and o["id"] != bid and s.add_edge("belief", bid, "opposes", "belief", o["id"], now, did, json.loads(
                    s.one("SELECT entries FROM episodes WHERE id = ?", ep)["entries"])):
                count["opposite"] += 1
    _graph(s, did, person, answer, mine, made, episodes, now, count, held_back)
    count["held_back"] = held_back
    return count


def _alias_ok(name, entries):
    """A name or a short phrase for the thing, never a whole sentence of the message it came from."""
    name = " ".join(str(name or "").split())
    if not name or not words(name):
        return False
    norm = lambda t: " ".join(re.findall(r"[a-z0-9']+", t.lower()))
    for e in entries:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", e["text"]):
            if norm(sentence) and norm(sentence) == norm(name):
                return False
    return True


def _graph(s, did, person, answer, mine, made, episodes, now, count, held_back):
    """Things, names and edges from the dream's answer. A held thing is reused by its id or by any of
    its names; only then is one made."""
    everything = list(mine.values())

    def cited(tags):
        return [mine[f"s{i}"]["id"] for i in _ids(tags, "s") if f"s{i}" in mine]

    def thing(ref, entries=(), make=True):
        ref = " ".join(str(ref or "").split())
        if not ref:
            return None
        t = s.one("SELECT * FROM things WHERE id = ?", int(ref[1:])) if re.fullmatch(r"[tT]\d+", ref) else None
        t = t or s.thing_named(ref)
        if t is None and make and _alias_ok(ref, everything):
            t = s.one("SELECT * FROM things WHERE id = ?", s.add_thing(ref, now, did, entries))
            count["things"] += 1
        return t

    def alias(t, name, entries, how="alias"):
        if not _alias_ok(name, everything):
            held_back.append({"alias": name, "why": "a whole sentence of the message, not a name or a short phrase"})
            return None
        nid = s.add_name(t["id"], name, now, did, entries, how=how)
        if nid:
            count["aliases"] += 1
        return nid

    for t in answer.get("things") or []:
        if not isinstance(t, dict):
            continue
        entries = cited(t.get("entries"))
        existed = bool(re.fullmatch(r"[tT]\d+", str(t.get("id") or "")) or s.thing_named(t.get("name")))
        row = thing(t.get("id") or t.get("name"), entries)
        if row is None:
            continue
        count["things_reused"] += int(existed)
        if t.get("name") and row["name"].lower() != str(t["name"]).lower():
            alias(row, t["name"], entries)
        for a in t.get("aliases") or []:
            alias(row, a, entries)
    for n, nb in _new_beliefs(answer):
        bid = made.get(n)
        if not bid or n not in episodes:
            continue
        src = episodes[n][4]
        for ref in nb.get("about_things") or []:
            t = thing(ref)
            if t and s.add_edge("belief", bid, "about", "thing", t["id"], now, did, src):
                count["edges"] += 1
        for key, rel in (("refines", "refines"), ("exception_to", "exception_to"), ("for_now_beside", "for_now")):
            ref = _belief(s, nb.get(key))
            if ref and ref["id"] != bid and s.add_edge("belief", bid, rel, "belief", ref["id"], now, did, src):
                count["edges"] += 1
    for st in answer.get("settle") or []:
        b = _belief(s, st.get("belief")) if isinstance(st, dict) else None
        if b and st.get("how") in ("lasting", "lapsed"):
            s.run("UPDATE edges SET ended_at = ?, ended = ? WHERE src_kind = 'belief' AND src = ? AND rel = 'for_now'"
                  " AND ended_at IS NULL", now, st["how"], b["id"])
    for ch in answer.get("changes") or []:
        b = _belief(s, ch.get("belief")) if isinstance(ch, dict) else None
        text, srcs = str((ch or {}).get("now") or "").strip(), cited((ch or {}).get("entries"))
        ep = next((e for e in episodes.values() if set(e[4]) & set(srcs)), None)
        if not b or not text or not ep:
            held_back.append({"change": ch, "why": "names no belief, no new wording or no fact of this dream"})
            continue
        if s.pinned(b["id"]):
            held_back.append({"change": ch, "why": f"b{b['id']} is pinned: only {person}'s own words replace it, as they say them"})
            continue
        epid, said_by, kind, at, _ = ep
        if b["about"].startswith(PERSON_ABOUT + ":") and said_by != b["about"]:
            held_back.append({"change": ch, "why": f"about what {b['about']} wants, from {said_by}'s words"})
            continue
        s.reword(b["id"], text, now, f"changed (dream {did}): {ch.get('why') or ''}".strip())
        s.add_link(b["id"], epid, 1, strength(kind, said_by), _occasion(at), now, did, why="the changed value",
                   flips=said_by.startswith("person:") or b["about"] == "how")
        count["changes"] += 1
    for a in answer.get("about") or []:
        b, t = (_belief(s, a.get("belief")), thing(a.get("thing"))) if isinstance(a, dict) else (None, None)
        if b and t and s.add_edge("belief", b["id"], "about", "thing", t["id"], now, did):
            count["edges"] += 1
    for r in answer.get("relations") or []:
        a, b = (thing(r.get("thing")), thing(r.get("other"))) if isinstance(r, dict) else (None, None)
        if a and b and a["id"] != b["id"] and r.get("rel") in ("part_of", "related") and s.add_edge(
                "thing", a["id"], r["rel"], "thing", b["id"], now, did, cited(r.get("entries"))):
            count["edges"] += 1
    # Heals: the words a missed message used become a name of the thing (and the edge it lacked).
    for h in answer.get("heals") or []:
        mid = _ids([h.get("miss")], "m") if isinstance(h, dict) else []
        miss = s.one("SELECT * FROM misses WHERE id = ?", mid[0]) if mid else None
        t = thing(h.get("thing"), make=False) if miss else None
        if not miss or not t:
            held_back.append({"heal": h, "why": "names no open miss or no held thing"})
            continue
        b = _belief(s, h.get("belief"))
        held_then = (b["made_at"] <= miss["at"]) if b else (t["made_at"] <= miss["at"])
        if not held_then:      # a miss only if what it was after was held when the message came
            s.run("UPDATE misses SET status = 'nothing held', dream = ?, note = ? WHERE id = ?", did,
                  "new information, not a miss: nothing was held then", miss["id"])
            continue
        done = []
        entry = s.one("SELECT id FROM entries WHERE session = ? AND seq = ?", miss["session"], miss["seq"])
        src = [entry["id"]] if entry else []
        if _alias_ok(h.get("alias"), everything):
            nid = s.add_name(t["id"], h.get("alias"), now, did, src, how="heal")
            if nid:
                s.run("INSERT INTO heals (miss, at, dream, kind, ref, words, why) VALUES (?, ?, ?, 'alias', ?, ?, ?)",
                      miss["id"], now, did, nid, h.get("alias"), h.get("why"))
                done.append("alias")
        if b:
            eid = s.add_edge("belief", b["id"], "about", "thing", t["id"], now, did, src, how="heal", why=h.get("why"))
            if eid:
                s.run("INSERT INTO heals (miss, at, dream, kind, ref, words, why) VALUES (?, ?, ?, 'edge', ?, ?, ?)",
                      miss["id"], now, did, eid, f"b{b['id']} about t{t['id']}", h.get("why"))
                done.append("edge")
        if done:
            s.run("UPDATE misses SET status = 'healed', dream = ? WHERE id = ?", did, miss["id"])
            count["heals"] += 1
    for tag in answer.get("nothing_held") or []:
        for mid in _ids([tag], "m"):
            s.run("UPDATE misses SET status = 'nothing held', dream = ? WHERE id = ? AND status = 'open'", did, mid)
    last = max(e["seq"] for e in mine.values())
    session = next(iter(mine.values()))["session"]
    s.run("UPDATE sessions SET dreamed_to = MAX(dreamed_to, ?) WHERE session = ?", last, session)


# ---------------------------------------------------------------------------------- pins

def _norm(t):
    return " ".join(re.findall(r"[a-z0-9']+", (t or "").lower()))


def his_words(statement, message):
    """The pin's text: the agent's quote if it is the person's words as they said them, else their whole message."""
    quote = (statement or "").strip().strip('"').strip()
    return quote if quote and _norm(quote) and _norm(quote) in _norm(message) else message.strip()


def _turn(call):
    """The turn this call came from (its door token), else None."""
    return _turning.get(call._actor.get("token"))


def _his_message(call, s):
    """(the person's own message a pin rests on, else None, and why not). In a turn, the turn's message;
    from outside one, the caller's latest own message in their conversation."""
    if call._actor.get("token") in _dreaming:
        return None, "A dream does not pin or unpin: only the person's own words do, in their turn. Nothing changed."
    t = _turn(call)
    if t is None:
        person = f"person:{call.person()['name']}"
        row = s.one("SELECT e.* FROM entries e JOIN sessions x ON x.session = e.session WHERE x.person = ? AND"
                    " e.speaker = ? ORDER BY e.at DESC, e.id DESC LIMIT 1", person, person)
        return (row, None) if row else (None, f"{person} has said nothing yet to pin; nothing changed.")
    row = s.one("SELECT * FROM entries WHERE session = ? AND seq = ?", t["session"], t["seq"])
    if t["speaker"] != t["person"]:
        return None, (f"This message is from {t['speaker']}, not {t['person']}'s own words: only their own words pin, "
                      "unpin or replace a pin. Nothing changed.")
    return row, None


def pin(call, args):
    s = store(call)
    row, why = _his_message(call, s)
    if row is None:
        return {"pinned": None, "note": why}
    words_ = his_words(args.get("statement"), row["text"])
    now = _now(call)
    with s.write():
        match = how = None
        if args.get("belief"):
            match = _belief(s, args["belief"])
            if match is None:
                return {"pinned": None, "note": f"There is no belief {args['belief']}; nothing was pinned."}
            how = "the belief named"
        else:
            # Their later statement about the same thing replaces what was pinned (MEM5, answer 2): a pinned
            # belief about a thing their words name, the most recently pinned first.
            named = list(match_things(s, words(words_)))
            same = s.one(f"""SELECT b.* FROM beliefs b JOIN edges e ON e.src_kind = 'belief' AND e.src = b.id AND
                             e.rel = 'about' AND e.dst_kind = 'thing' AND e.dst IN ({','.join('?' * len(named))})
                             WHERE b.pinned ORDER BY (SELECT MAX(id) FROM pins WHERE belief = b.id) DESC LIMIT 1""",
                         *named) if named else None
            best = max(((dice(words_, r["text"]), r["id"]) for r in _held_close_any(s, words_)), default=None)
            if same is not None:
                match, how = same, "the pinned belief about what their words name"
            elif best and best[0] >= PIN_MATCH:
                match, how = s.one("SELECT * FROM beliefs WHERE id = ?", best[1]), f"the closest held belief ({best[0]:.2f})"
        person = row["speaker"]
        ep = s.run("INSERT INTO episodes (session, text, said_by, channel, kind, entries, happened_at, made_at)"
                   " VALUES (?, ?, ?, ?, 'decision', ?, ?, ?)", row["session"],
                   f'{person.removeprefix("person:")} said, definitively: "{words_}"', person, row["channel"],
                   json.dumps([row["id"]]), row["at"], now)
        action, was = "pin", None
        if match is None:
            about = args.get("about")
            about = person if about in (person, PERSON_ABOUT, "person") else about if about in ("how", "world") else "world"
            bid = s.add_belief(words_, about, now)
            how = "a new belief in their words"
        else:
            bid = match["id"]
            last = s.last_pin(bid)
            if s.pinned(bid) and _norm(last["words"]) != _norm(words_):
                action, was = "replace", last["words"]
            s.reword(bid, words_, now, "replaced in their words" if action == "replace" else "pinned in their words")
        s.run("INSERT INTO pins (belief, action, at, session, seq, words, was) VALUES (?, ?, ?, ?, ?, ?, ?)", bid,
              action, now, row["session"], row["seq"], words_, was)
        s.add_link(bid, ep, 1, strength("decision", person), _occasion(row["at"]), now, why="their words when pinning")
        for tid in match_things(s, words(words_)):      # the things their words name: the pin's own edges
            s.add_edge("belief", bid, "about", "thing", tid, now, entries=[row["id"]], how="pin")
    say = {"pin": f"Pinned b{bid} ({how}), in their words: \"{words_}\".",
           "replace": f"That replaces what was pinned: \"{was}\". Pinned b{bid} now: \"{words_}\"."}[action]
    return {"pinned": f"b{bid}", "action": action, "words": words_, "was": was,
            "note": say + " Say so in your reply, in one line. If that is not what they meant, unpin it, or pin again "
                          "with belief= the right id."}


def _held_close_any(s, text):
    terms = words(text)
    if not terms:
        return []
    sql, args = _match_sql("belief_fts", terms)
    return s.all(f"SELECT b.id, b.text FROM ({sql}) m JOIN beliefs b ON b.id = m.id", *args)


def unpin(call, args):
    s = store(call)
    row, why = _his_message(call, s)
    if row is None:
        return {"unpinned": None, "note": why}
    with s.write():
        if args.get("belief"):
            b = _belief(s, args["belief"])
            bid = b["id"] if b else None
        else:
            last = s.one("SELECT belief FROM pins WHERE action != 'unpin' ORDER BY id DESC LIMIT 1")
            bid = last["belief"] if last else None
        if not bid or not s.pinned(bid):
            return {"unpinned": None, "note": f"b{bid} is not pinned; nothing changed." if bid else
                    "Nothing is pinned; nothing changed."}
        text = s.one("SELECT text FROM beliefs WHERE id = ?", bid)["text"]
        s.run("INSERT INTO pins (belief, action, at, session, seq, words) VALUES (?, 'unpin', ?, ?, ?, ?)", bid,
              _now(call), row["session"], row["seq"], text)
        s.refresh(bid)
    return {"unpinned": f"b{bid}", "words": text,
            "note": f"Unpinned b{bid}; it is an ordinary belief again: \"{text}\". Say so in your reply, in one line."}


# ---------------------------------------------------------------------------------- the caps

async def search(call, args):
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise Error("bad_message", "query is required: what to look for, in your own words.", "Give query as text.")
    s = store(call)
    r = await asyncio.to_thread(recall, s, query, _now(call), int(call.settings["memory_hops"]),
                                int(call.settings["memory_beliefs"]))
    t = _turn(call)
    if t is not None:            # found by the agent's own words where recall had missed: a heal for the next dream
        new = [f"b{b['id']}" for b in r["beliefs"] if f"b{b['id']}" not in t["recalled"]]
        if new:
            with s.write():
                m = s.one("SELECT * FROM misses WHERE session = ? AND seq = ?", t["session"], t["seq"])
                if m:
                    found = list(dict.fromkeys(json.loads(m["found"]) + new))
                    s.run("UPDATE misses SET found = ? WHERE id = ?", json.dumps(found), m["id"])
                else:
                    query_text = s.one("SELECT text FROM entries WHERE session = ? AND seq = ?", t["session"], t["seq"])
                    s.run("INSERT INTO misses (session, seq, at, query, recalled, found) VALUES (?, ?, ?, ?, ?, ?)",
                          t["session"], t["seq"], _now(call), query_text["text"] if query_text else query,
                          json.dumps(t["recalled"]), json.dumps(new))
    return {"beliefs": [{"id": f"b{b['id']}", "text": b["text"], "about": b["about"], "standing": b["summary"],
                         "pinned": bool(b["pinned"]), "found_by": b.get("via"), "beside": b.get("beside"),
                         "rests_on": rests_on(s, b["id"])} for b in r["beliefs"]],
            "things": [_thing_line(s, tid) for tid in r["things"]], "nearest_things": r["hint"],
            "note": None if r["beliefs"] else "Nothing held matches these words."}


def show(call, args):
    """A belief's or a thing's neighbourhood and history; with no id, every thing held."""
    s = store(call)
    ref = str(args.get("id") or "").strip()
    if not ref:
        return {"things": [_thing_line(s, t["id"]) for t in s.all("SELECT id FROM things ORDER BY id")]}
    if re.fullmatch(r"[bB]\d+", ref):
        return {"lines": belief_history(s, int(ref[1:]))}
    t = s.one("SELECT * FROM things WHERE id = ?", int(ref[1:])) if re.fullmatch(r"[tT]\d+", ref) else s.thing_named(ref)
    if not t:
        raise Error("unknown_memory", f"Memory holds no belief or thing {ref!r}.",
                    "Give an id from agent.memory.search (b12, t3), or a thing's name; agent.memory.show with no id "
                    "lists the things.")
    return {"lines": neighbourhood(s, "thing", t["id"])}


def neighbourhood(s, kind, ref):
    out = []
    if kind == "thing":
        t = s.one("SELECT * FROM things WHERE id = ?", ref)
        out.append(f"[t{ref}] {t['name']}  (made {_when(t['made_at'])} by dream {t['dream'] or '-'})")
        for n in s.all("SELECT * FROM names WHERE thing = ? AND how != 'name' ORDER BY id", ref):
            out.append(f"    also said: \"{n['name']}\"  ({n['how']}, {_when(n['at'])}, dream {n['dream'] or '-'})")
    for e in s.all("SELECT * FROM edges WHERE src_kind = ? AND src = ? UNION ALL SELECT * FROM edges WHERE dst_kind = ?"
                   " AND dst = ? ORDER BY id", kind, ref, kind, ref):
        mine = e["src_kind"] == kind and e["src"] == ref
        ok, oid = (e["dst_kind"], e["dst"]) if mine else (e["src_kind"], e["src"])
        other = s.one(f"SELECT {'name' if ok == 'thing' else 'text'} AS t FROM {'things' if ok == 'thing' else 'beliefs'}"
                      " WHERE id = ?", oid)["t"]
        ended = f", ended {e['ended']} {_when(e['ended_at'])}" if e["ended_at"] else ""
        out.append(f"    {e['rel'] + ' ->' if mine else '<- ' + e['rel']} [{ok[0]}{oid}] {other}  ({e['how']}, dream "
                   f"{e['dream'] or '-'}{ended})")
    return out


def belief_history(s, bid):
    b = s.one("SELECT * FROM beliefs WHERE id = ?", bid)
    if not b:
        raise Error("unknown_memory", f"Memory holds no belief b{bid}.", "Give an id from agent.memory.search.")
    out = [f"[b{bid}] about {b['about']}, made {_when(b['made_at'])} by {'dream ' + str(b['dream']) if b['dream'] else 'a pin'}",
           f"    {b['text']}"]
    out += neighbourhood(s, "belief", bid)
    for t in s.all("SELECT * FROM belief_texts WHERE belief = ? ORDER BY at", bid):
        out.append(f"    once worded: \"{t['text']}\" (until {_when(t['at'])}: {t['why']})")
    events = [("link", l["at"], l["id"], l) for l in s.links(bid)]
    events += [("pin", p["at"], p["id"], p) for p in s.all("SELECT * FROM pins WHERE belief = ?", bid)]
    events.sort(key=lambda e: (e[1], e[0] == "pin", e[2]))
    out.append("In the order it happened, with the standing after each:")
    for kind, at, _, row in events:
        if kind == "pin":
            out.append(f"  {_when(at)}  {row['action'].upper()}: \"{row['words']}\"" +
                       (f" (was \"{row['was']}\")" if row["was"] else ""))
        else:
            out.append(f"  {_when(at)}  {'supports' if row['stance'] > 0 else 'AGAINST '} {row['strength']:.2f}  "
                       f"{row['said_by']}, {row['kind']} [e{row['episode']}, dream {row['dream'] or '-'}]: "
                       f"{row['episode_text']}" + (f"  ({row['note']})" if row["note"] else ""))
        out.append(f"        -> {summary(s.standing(bid, upto=at))}")
    out.append(f"Now: {summary(s.standing(bid))}")
    return out


def misses(call, args):
    s = store(call)
    rows = s.all("SELECT * FROM misses ORDER BY id")
    out = []
    for m in rows:
        heals = [f"{h['kind']} {h['words']}" + (f" ({h['why']})" if h["why"] else "")
                 for h in s.all("SELECT * FROM heals WHERE miss = ? ORDER BY id", m["id"])]
        out.append({"id": f"m{m['id']}", "at": _when(m["at"]), "message": m["query"], "status": m["status"],
                    "recalled": json.loads(m["recalled"]), "found_by_search": json.loads(m["found"]),
                    "dream": m["dream"], "healed_with": heals, "note": m["note"]})
    counts = {}
    for m in rows:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    return {"misses": out, "counts": counts}


async def dream_cap(call, args):
    """Run a dream of the caller's conversation now (or `party`'s), and answer what it wrote."""
    party = args.get("party")
    person = call.person(party.removeprefix("person:")) if party else call.person()
    t = _turn(call)
    sess = call.get(t["session"]) if t else agent.session(call, person, create=False)
    if sess is None:
        return {"dream": None, "note": f"{person['name']} has no conversation with the house agent yet."}
    return await dream(call.as_caller(sess["owner"]), sess["guid"], str(args.get("trigger") or "asked"))


def made(call):
    """At every load: the index has every entry of the record; a dream cut off by a restart is said so
    (its words are still undreamed, so the next trigger dreams them again)."""
    global _armed
    _armed = False
    if not agent._sessions(call):        # nobody has talked to the house agent yet: nothing to keep
        return
    backfill(call)
    s = store(call)
    s.run("UPDATE dreams SET state = 'cut off', ended_at = ?, error = 'the hub stopped before it ended; its words are "
          "dreamed again at the next trigger' WHERE state = 'running'", _now(call))
    arm(call)


def memory_parts(call, record):
    """The system prompt's memory section (hub.py's seam)."""
    if not on(call):
        return []
    return [MEMORY_PROMPT]


def precompact(call):
    """What the engine's hook before compaction calls (agents-claude-code.turn's `precompact`)."""
    return {"cap": "agent.dream", "args": {"agent": agent.HOUSE, "trigger": "compaction"}} if on(call) else None


def setup(plugin):
    plugin.setting("memory", "The house agent's memory: recall on every turn, dreams, pins (true, the default) or "
                             "none (false: the record is still kept, so the first dream after reads all of it).",
                   default=True)
    plugin.setting("memory_idle_minutes", "Minutes a conversation is idle, with words no dream has read, before the "
                                          "house agent dreams.", default=5)
    plugin.setting("memory_hops", "How far recall walks out from the things a message names (steps over part-of and "
                                  "related edges).", default=1)
    plugin.setting("memory_beliefs", "How many beliefs a turn is handed (and agent.memory.search answers), closest "
                                     "first; their exceptions, for-now and opposite beliefs come along.", default=6)
    plugin.on_load(made)
    for name, (handler, description, schema) in CAPS.items():
        plugin.cap(name, description, schema)(handler)


_ID = {"type": "string", "description": "A belief's id (b12)."}
CAPS = {
    "agent.memory.search": (search, "Search the house agent's memory in your own words: beliefs with their standing "
                                    "and who they rest on, and the things they are about.", {
        "type": "object", "required": ["query"], "additionalProperties": False, "properties": {
            "query": {"type": "string", "description": "What to look for, in your own words."}}}),
    "agent.memory.pin": (pin, "Pin what the person just said definitively, in their own words: the matching belief, "
                              "or a new pinned one; their later statement replaces a pin.", {
        "type": "object", "required": ["statement"], "additionalProperties": False, "properties": {
            "statement": {"type": "string", "description": "Their definitive words, quoted from their message."},
            "belief": _ID, "about": {"type": "string", "description": "For a new belief: world (how things are), how "
                                                                       "(how things work) or person (what they want)."}}}),
    "agent.memory.unpin": (unpin, "Lift a pin the person did not mean; without belief, the most recent pin.", {
        "type": "object", "additionalProperties": False, "properties": {"belief": _ID}}),
    "agent.memory.show": (show, "A belief's or a thing's neighbourhood and history (b12, t3 or a name); no id: every "
                                "thing memory holds.", {
        "type": "object", "additionalProperties": False, "properties": {
            "id": {"type": "string", "description": "b12, t3, or a thing's name; none: list the things."}}}),
    "agent.memory.misses": (misses, "Every turn whose recall missed, and how a dream healed it (or why not).", {
        "type": "object", "additionalProperties": False, "properties": {}}),
    "agent.dream": (dream_cap, "Run the house agent's dream now over your conversation: it distils what was said "
                               "since the last dream into memory, and answers what it wrote.", {
        "type": "object", "additionalProperties": False, "properties": {
            "agent": {"type": "string", "description": "The agent: house."},
            "party": {"type": "string", "description": "Whose conversation: person:<name> or a user's name; none: yours."},
            "trigger": {"type": "string", "description": "Why it runs (asked, compaction, ...), as the record keeps it."}}}),
}


MEMORY_PROMPT = """## Memory
You have a memory that outlasts this conversation. Each message starts with who sent it. Only a message from
the person themselves (person:<name>) is their own words; a message from an agent (agent:<token>) or a job
came with their token but is not what they said.

Under each message comes a "## Memory" block: what memory holds for it, closest first, each belief with its
id, its standing (worked out from the evidence: who said what, how often, how strongly) and who it rests on.
Use it. What was said in this conversation since is newer than memory and wins. If the block does not
answer something you are asked, search memory in your own words (agent.memory.search, through Rook's call
tool, worker hub) before you say you don't remember; if that finds nothing either, say you don't remember.
Never invent. A belief marked "ask before acting" has something fresh against something settled: if you
are about to act on it, ask which holds.

Who decides what. Agents and the steward may decide how things work: how a thing is done, where a job
runs, when something goes out, which tool is used. Carry out such a request yourself, without waiting,
and say whose decision it was. What a thing is for, what the person wants from it, their preferences and
goals are theirs alone: if an agent asks for a change to one of those, hold it for them and say why.

Pins. Whether the person said something definitively, meant to hold for good, is your call ("that is
final", "never again", "it is not coming back" usually are; an ordinary correction or a decision for now
usually is not). Pin it with agent.memory.pin, quoting their words. Say so plainly in the same reply, in
one line, with their words; if it was pinned already, say it is pinned again. When their own message
says something different about what is pinned, pin their new words once, with belief= the pinned one's
id: that replaces the pin, and you say so ("that replaces what was pinned: ..."). They can undo any pin at any time, the next message or days
later ("unpin that", "that wasn't meant to be pinned"): call agent.memory.unpin with its id and say so.
Beyond that one line, never talk about pinning. Only the person's own words pin, unpin or replace.

Asks. A "To ask" line under a message means an agent said something memory holds is no longer so. Their
word does not change what memory holds; the person's does. Put the question once, briefly, in that
reply, and do not raise it again."""


DREAM = """[Memory dream. This turn is a fork: nothing you write here reaches the conversation.]

It is {now}. Below is the record of the conversation with {person} since the last dream, one entry per
message, each with its id, when, who said it and how it came. {person} is the person themselves; agent:...
is an agent that came with their token, job:... a job; "house" is the house agent's own replies: they are
context (often repeating what memory told it), never evidence for or against a belief.

{entries}

Do these things, in order. Ask yourself no other questions.

1. Distil facts. Anything in the record that can stand as a fact: about the machines, the projects, the
   people, what someone wants, decided, asked for or corrected. Each fact says who it comes from in its own
   words ("{name} said...", "The agent decided..."), names the entry ids it comes from (s12), says whose
   words it rests on ("said_by"), and has a kind:
   - correction: someone corrected what was assumed or said;
   - decision: someone decided something ({name} on anything; an agent on how things work);
   - statement: said plainly;
   - mention: implied, assumed, or said in passing;
   - unclear: garbled, half-heard, or a guess.
   Skip greetings, thanks and pure mechanics. A question is not a claim.

2. Novelty. For each fact, look up what is already held: Rook's call tool, worker hub, cap
   agent.memory.search {{"query": ...}} in your own words, and agent.memory.show {{"id": "b12"}} (or "t3", or no
   id for every thing held). Then:
   - a held belief the fact supports: list it under "held" with "supports" (by its id, b12);
   - a held belief the fact counts against (something changed): list it with "against";
   - nothing held says it: give a "new_belief". If a held belief already says the same thing, link to it
     instead. If it says the opposite of a held belief, give that belief's id as "opposite_of". Give each
     new belief two to four "other_wordings": other ways someone might say it or ask about it.
   A belief says one thing: two claims are two facts. Say what each belief is about:
   - "{person}": what {name} wants, prefers, decided or aims at, and what things are for;
   - "how": how things work: how something is done, where a job runs, which tool is used;
   - "world": how things are (machines, places, events).
   Never write a belief about what is not known, not remembered or not yet recorded.

3. The graph. Memory is read without a model: a message finds beliefs through the things they are about,
   by the names and other wordings of those things. So:
   - "things": what the beliefs here are about (a machine, a device, a feature, a person, a project, a job,
     a place). Look first: reuse a held thing by its id (t3) and add what was newly said for it; make a new
     one only when none is held. Give each thing two to four "aliases": other ways it is said or asked
     about, each a name or a short phrase of a few words ("the old server", "always listening", "photos"),
     never a claim or a sentence. Aliases are how a later message finds the thing.
   - each new belief's "about_things" (names or ids), and how it stands to a held belief: "refines" (says
     it more exactly), "exception_to" (narrower in scope: one place, one device, one case), or
     "for_now_beside" (temporary: a trial, "for a bit", "this week"). An exception or a for-now wish is
     never evidence against the standing belief: do not link it "against" that one;
   - "settle": a for-now belief later confirmed as lasting: lasting; withdrawn or over: lapsed;
   - "changes": a value that changed (a time moved, a setting changed) is the same belief changing: give
     the held belief, its new wording and the entries; do not make a new belief and mark the old one its
     opposite;
   - what an agent says against a held belief: link it "against" as theirs; it does not change what is
     held ({name} will be asked);
   - "about": held beliefs that are about a thing but not yet linked to it;
   - "relations": a thing that is part of, or related to, another, where the conversation says so;
   - "heals": below are recall misses: messages where automatic recall turned up no belief, or not the one
     the conversation then showed it should have. It is a miss only if a belief already held when the
     message came was what it was after, and the message named the thing in words of its own; new
     information is not a miss, and nor is a message that only answers what was just said ("no, it's
     still gone"). For a real miss give the thing, an alias in the words the message used for the thing
     (a name or a short phrase, "the old rack box", never a claim such as "still gone"), and the belief.
     Otherwise list the miss under "nothing_held". Never write anything about the gap itself.

Recall misses:
{misses}

Who said it matters most. A fact rests on one speaker's words: the house agent's answer is the house
agent's, a question {name} asked is not a statement by them. A belief about what {name} wants comes
only from {name}'s own words (entries marked {person}). What an agent decided about how things work is
theirs: write it as theirs.

Answer with the JSON asked for."""


SAME_PROMPT = """Each pair below is a new belief and a belief already held (or proposed just before). For each,
say whether they say the same thing (one could stand for the other), the opposite (both cannot hold at
once), or something different.

{pairs}

Answer with the JSON asked for."""
