"""The hub journal store (rook/core2/hub/journal_store.py): stamping, chains,
write-once triggers, tamper detection, queries and retention pruning."""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path

import pytest

from rook.core2 import journal
from rook.core2.hub import journal_store
from rook.core2.hub.journal_store import COLUMNS, JournalError, JournalStore, _col

VECTORS = Path(__file__).resolve().parents[1] / "conformance" / "core-v2" / "vectors" / "journal.json"
DAY = journal.DAY
T0 = 1_793_300_000
S_CASTLING = {"id": "sess-1", "project": "castling", "person": "person:bake", "agent": "agent:cc"}
S_NONE = {"id": "sess-2", "project": None, "person": "person:bake", "agent": "agent:cc"}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def call(cap="shell.exec", risk="exec", **kw):
    return {"kind": "call", "cap": cap, "risk": risk, "call": kw.pop("call", None) or f"c-{cap}",
            "worker": "w1", "person": "person:bake", "agent": "agent:cc", "why": "Test.", "ok": True, **kw}


def raw_insert(db: sqlite3.Connection, e: dict) -> None:
    """Write a row the way the store does, without going through append."""
    db.execute(f"INSERT INTO journal ({', '.join(COLUMNS)}, body) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
               [_col(c, e.get(c)) for c in COLUMNS] + [json.dumps(e)])


def drop_triggers(store: JournalStore) -> None:
    """What someone going around the hub would do first."""
    for (name,) in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        store._db.execute(f"DROP TRIGGER {name}")


@pytest.fixture
def store():
    s = JournalStore(clock=Clock())
    yield s
    s.close()


# -- pure helpers ---------------------------------------------------------------

def test_chain_names_and_stamp():
    assert journal.chain_name("castling") == "project:castling"
    assert journal.chain_name(None) == journal.chain_name("") == journal.GENERAL == "general"
    e = journal.stamp({"kind": "call", "chain": "project:other", "project": "other"}, S_CASTLING)
    assert (e["chain"], e["project"], e["session"]) == ("project:castling", "castling", "sess-1")
    e = journal.stamp({"kind": "call", "chain": "project:other"}, None)
    assert (e["chain"], e["project"]) == ("general", None) and "session" not in e


def test_tiers_and_expiry():
    assert [journal.tier(r) for r in (None, "read", "write", "exec", "admin", "bogus")] == \
        ["read", "read", "write", "exec", "admin", "admin"]
    days = journal.RETENTION_DAYS
    assert 30 <= min(days.values()) and max(days.values()) <= 90  # spec 11.4: 30 to 90 days
    assert not journal.expired(0, "read", 30 * DAY - 1)
    assert journal.expired(0, "read", 30 * DAY)
    assert not journal.expired(0, "admin", 89 * DAY)
    assert journal.expired(0, "admin", 7 * DAY, {"admin": 7})


def test_first_break_takes_a_head_and_any_iterable():
    chain, head = [], None
    for i in range(3):
        chain.append(journal.append(head, {"id": str(i), "kind": "call", "chain": "general"}))
        head = chain[-1]["hash"]
    assert journal.first_break(iter(chain[1:]), chain[0]["hash"]) is None
    assert journal.first_break(chain[1:]) == 0


# -- conformance vectors through the store ------------------------------------

def _store_with(tmp_path, entries) -> JournalStore:
    path = str(tmp_path / "j.sqlite3")
    JournalStore(path).close()
    db = sqlite3.connect(path)
    for e in entries:
        raw_insert(db, e)
    db.commit()
    db.close()
    return JournalStore(path)


def test_vector_chain_verifies_and_continues(tmp_path):
    v = json.loads(VECTORS.read_text())
    s = _store_with(tmp_path, v["chain"])
    assert s.check() == []
    assert s.heads() == {"project:castling": v["chain"][-1]["hash"]}
    nxt = s.append(call(), {"id": "s", "project": "castling"})
    assert nxt["prev"] == v["chain"][-1]["hash"]
    assert list(s.entries("project:castling"))[:3] == v["chain"]
    assert s.check() == []


def test_vector_tamper_checks(tmp_path):
    v = json.loads(VECTORS.read_text())
    for i, c in enumerate(v["checks"]):
        d = tmp_path / str(i)
        d.mkdir()
        s = _store_with(d, c["entries"])
        problems = s.check()
        if c["first_break"] is None:
            assert problems == [], c["name"]
        else:
            assert [(p["chain"], p["id"], p["problem"]) for p in problems] == \
                [("project:castling", c["entries"][c["first_break"]]["id"], "broken link")], c["name"]
        s.close()


# -- append and stamping --------------------------------------------------------

def test_append_stamps_and_links(store):
    a = store.append({**call(), "chain": "general", "prev": "f" * 64, "hash": "e" * 64}, S_CASTLING)
    b = store.append(call("light.set", "write"), S_CASTLING)
    g = store.append(call("deluge.list", "read"), S_NONE)
    n = store.append({"kind": "session.start"})
    assert (a["chain"], a["project"], a["session"], a["prev"]) == ("project:castling", "castling", "sess-1",
                                                                   journal.GENESIS)
    assert b["prev"] == a["hash"] and a["hash"] == journal.entry_hash(a)
    assert (g["chain"], g["prev"]) == ("general", journal.GENESIS)
    assert n["prev"] == g["hash"] and n["ts"] == T0 and len(n["id"]) == 32
    assert store.heads() == {"project:castling": b["hash"], "general": n["hash"]}
    assert store.get(a["id"]) == a and store.get("nope") is None
    assert store.check() == []


def test_append_refuses_bad_entries_and_keeps_the_head(store):
    a = store.append(call(), S_CASTLING)
    for bad in ({"cap": "x"}, {"kind": ""}, {"kind": "call", "ts": "today"}, {"kind": "call", "ts": True},
                {"kind": "call", "id": 7}):
        with pytest.raises(JournalError):
            store.append(bad, S_CASTLING)
    with pytest.raises(JournalError, match="duplicate"):
        store.append({**call(), "id": a["id"]}, S_CASTLING)
    with pytest.raises(ValueError):  # no canonical form: nothing stored, head unchanged
        store.append({**call(), "n": float("nan")}, S_CASTLING)
    assert store.heads()["project:castling"] == a["hash"]
    b = store.append(call(), S_CASTLING)
    assert b["prev"] == a["hash"] and store.check() == []


def test_restart_keeps_heads_and_checkpoints(tmp_path):
    path = str(tmp_path / "j.sqlite3")
    clock = Clock()
    s = JournalStore(path, clock=clock)
    a = s.append(call(), S_CASTLING)
    s.append(call(), S_NONE)
    clock.t += 40 * DAY
    s.prune()
    heads = s.heads()
    s.close()
    s = JournalStore(path, clock=clock)
    assert s.heads() == heads and s.check() == []
    assert s.append(call(), S_CASTLING)["prev"] == a["hash"]
    s.close()


def test_newer_schema_refused(tmp_path):
    path = str(tmp_path / "j.sqlite3")
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 9")
    db.close()
    with pytest.raises(JournalError, match="newer"):
        JournalStore(path)


# -- write-once -----------------------------------------------------------------

def test_rows_are_write_once(store):
    a = store.append(call(), S_CASTLING)
    store.append(call(), S_CASTLING)
    db = store._db
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.execute("UPDATE journal SET cap = 'x' WHERE id = ?", (a["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.execute("UPDATE journal SET body = '{}' WHERE id = ?", (a["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.execute("DELETE FROM journal WHERE id = ?", (a["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        db.execute("DELETE FROM journal")
    assert store.check() == [] and len(store.query()) == 2


def test_checkpoints_only_move_forward_to_real_entries(store):
    a = store.append(call(), S_CASTLING)
    b = store.append(call(), S_CASTLING)
    db = store._db
    seq = {i: s for s, i in db.execute("SELECT seq, id FROM journal")}
    with pytest.raises(sqlite3.DatabaseError, match="checkpoint"):  # a hash that isn't that entry's
        db.execute("INSERT INTO journal_checkpoints VALUES ('project:castling', ?, ?, 0)", (seq[b["id"]], "0" * 64))
    with pytest.raises(sqlite3.DatabaseError, match="checkpoint"):  # an entry of another chain
        db.execute("INSERT INTO journal_checkpoints VALUES ('general', ?, ?, 0)", (seq[b["id"]], b["hash"]))
    db.execute("INSERT INTO journal_checkpoints VALUES ('project:castling', ?, ?, 0)", (seq[b["id"]], b["hash"]))
    with pytest.raises(sqlite3.DatabaseError, match="checkpoint"):
        db.execute("UPDATE journal_checkpoints SET seq = ?, hash = ?", (seq[a["id"]], a["hash"]))
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        db.execute("DELETE FROM journal_checkpoints")
    # A checkpoint written behind the hub's back is still caught.
    assert store.check() == [{"chain": "project:castling", "id": None, "problem": "checkpoint differs"}]


# -- tamper detection around the triggers ------------------------------------

def _three(store):
    return [store.append(call(f"cap.{i}", call=f"c{i}"), S_CASTLING) for i in range(3)]


def test_edited_body_is_caught(store):
    e = _three(store)
    drop_triggers(store)
    forged = {**e[1], "why": "Covering tracks."}
    store._db.execute("UPDATE journal SET body = ? WHERE id = ?", (json.dumps(forged), e[1]["id"]))
    assert store.check() == [{"chain": "project:castling", "id": e[1]["id"], "problem": "broken link"}]


def test_rehashed_entry_breaks_the_next_link(store):
    e = _three(store)
    drop_triggers(store)
    forged = {**e[1], "why": "Covering tracks."}
    forged["hash"] = journal.entry_hash(forged)
    store._db.execute("UPDATE journal SET body = ?, hash = ? WHERE id = ?",
                      (json.dumps(forged), forged["hash"], e[1]["id"]))
    assert store.check()[0]["id"] == e[2]["id"]


def test_rewritten_tail_is_caught_by_the_head_in_memory(store):
    e = _three(store)
    drop_triggers(store)
    forged = {**e[2], "ok": False}
    forged["hash"] = journal.entry_hash(forged)
    store._db.execute("UPDATE journal SET body = ?, hash = ? WHERE id = ?",
                      (json.dumps(forged), forged["hash"], e[2]["id"]))
    assert store.check() == [{"chain": "project:castling", "id": None, "problem": "head differs"}]


def test_deleted_entries_are_caught(store):
    e = _three(store)
    drop_triggers(store)
    store._db.execute("DELETE FROM journal WHERE id = ?", (e[1]["id"],))
    assert store.check()[0] == {"chain": "project:castling", "id": e[2]["id"], "problem": "broken link"}
    store._db.execute("DELETE FROM journal WHERE id = ?", (e[2]["id"],))
    assert store.check()[0]["problem"] == "head differs"
    store._db.execute("DELETE FROM journal")
    assert store.check()[0]["problem"] == "head differs"


def test_deleted_start_is_caught(store):
    e = _three(store)
    drop_triggers(store)
    store._db.execute("DELETE FROM journal WHERE id = ?", (e[0]["id"],))
    assert store.check()[0]["id"] == e[1]["id"]


def test_edited_index_column_is_caught(store):
    e = _three(store)
    drop_triggers(store)
    store._db.execute("UPDATE journal SET cap = 'innocent.read' WHERE id = ?", (e[0]["id"],))
    assert store.check() == [{"chain": "project:castling", "id": e[0]["id"], "problem": "columns differ from entry"}]


def test_forged_chain_is_caught(store):
    _three(store)
    forged = journal.append(None, {"id": "f1", "kind": "call", "chain": "project:ghost", "ts": T0})
    raw_insert(store._db, forged)
    assert store.check() == [{"chain": "project:ghost", "id": None, "problem": "chain this hub never wrote"}]


def test_verify_walks_in_pages(store, monkeypatch):
    monkeypatch.setattr(journal_store, "PAGE", 3)
    e = [store.append(call(call=f"c{i}"), S_CASTLING) for i in range(10)]
    assert store.check() == []
    assert [x["id"] for x in store.entries("project:castling")] == [x["id"] for x in e]
    drop_triggers(store)
    store._db.execute("DELETE FROM journal WHERE id = ?", (e[7]["id"],))
    assert store.check()[0]["id"] == e[8]["id"]


# -- queries ----------------------------------------------------------------------

def test_queries(store):
    clock = store.clock
    a = store.append(call("shell.exec", worker="w1", call="c1"), S_CASTLING)
    clock.t += 10
    b = store.append(call("shell.read", "read", worker="w2", call="c2", agent="agent:other"), S_CASTLING)
    clock.t += 10
    c = store.append(call("light.set", "write", worker="w1", call="c3", person="person:sam", agent=None),
                     {"id": "s3", "project": "home", "person": "person:sam"})
    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert ids(store.query()) == [c["id"], b["id"], a["id"]]
    assert ids(store.query(call="c2")) == [b["id"]]
    assert ids(store.query(cap="shell.exec")) == [a["id"]]
    assert ids(store.query(cap="shell.*")) == [b["id"], a["id"]]
    assert ids(store.query(cap="shell%.*")) == []
    assert ids(store.query(worker="w1")) == [c["id"], a["id"]]
    assert ids(store.query(actor="agent:other")) == [b["id"]]
    assert ids(store.query(actor="person:sam")) == [c["id"]]
    assert ids(store.query(project="castling")) == [b["id"], a["id"]]
    assert ids(store.query(chain="project:home")) == [c["id"]]
    assert ids(store.query(session="s3")) == [c["id"]]
    assert ids(store.query(since=T0 + 10)) == [c["id"], b["id"]]
    assert ids(store.query(since=T0, until=T0 + 10)) == [a["id"]]
    assert ids(store.query(worker="w1", project="castling")) == [a["id"]]
    assert ids(store.query(limit=1)) == [c["id"]]
    assert store.query(kind="prune") == []
    # 11.4 readers.
    assert len(store.query(viewer={"owner": True, "person": "person:bake"})) == 3
    assert ids(store.query(viewer={"person": "person:sam"})) == [c["id"]]
    assert ids(store.query(viewer={"agent": "agent:other"})) == [b["id"]]
    assert ids(store.query(viewer={"agent": "agent:other", "projects": ["castling"]})) == [b["id"], a["id"]]
    assert store.query(viewer={}) == []


# -- retention --------------------------------------------------------------------

def test_general_chain_prunes_whole_days_by_tier(store):
    clock = store.clock
    day0 = T0 - T0 % DAY
    clock.t = day0 + 100
    d1 = [store.append(call(risk="read"), S_NONE) for _ in range(2)]
    clock.t = day0 + DAY + 100
    d2 = [store.append(call(risk="admin"), S_NONE), store.append(call(risk="read"), S_NONE)]
    clock.t = day0 + 2 * DAY + 100
    d3 = [store.append(call(risk="read"), S_NONE)]
    store.append(call(), S_CASTLING)

    clock.t = day0 + 31 * DAY - 1  # day 1 ends at day0 + DAY, kept 30 days
    assert store.prune() == []
    clock.t = day0 + 31 * DAY  # day 1 goes; day 2 kept (admin, 90)
    [p] = store.prune()
    assert (p["kind"], p["chain"], p["pruned"], p["reason"], p["count"]) == ("prune", "general", "general", "age", 2)
    assert (p["through"], p["through_hash"], p["from_ts"]) == (d1[-1]["id"], d1[-1]["hash"], d1[0]["ts"])
    left = [e["id"] for e in store.entries("general")]
    assert left == [e["id"] for e in d2 + d3] + [p["id"]]
    assert store.check() == []

    clock.t = day0 + 60 * DAY  # day 3 is past 30 days but day 2 still holds it: no gaps
    assert store.prune() == []
    clock.t = day0 + 92 * DAY  # day 2 goes, then day 3 and the first prune entry (read tier) with it
    [p2] = store.prune()
    assert p2["count"] == 4 and p2["through"] == p["id"]
    assert [e["id"] for e in store.entries("general")] == [p2["id"]]
    assert store.check() == []
    # Rows past the checkpoint stay write-once.
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        store._db.execute("DELETE FROM journal WHERE id = ?", (p2["id"],))
    # The project chain is untouched by age.
    assert len(list(store.entries("project:castling"))) == 1


def test_closed_project_chain_goes_whole(store):
    clock = store.clock
    a = [store.append(call(risk="exec"), S_CASTLING) for _ in range(3)]
    closed = {"castling": T0 + DAY}
    clock.t = T0 + DAY + 59 * DAY
    assert store.prune(closed=closed) == []  # exec tier: 60 days after close
    clock.t = T0 + DAY + 60 * DAY
    [p] = store.prune(closed=closed)
    assert (p["pruned"], p["reason"], p["count"], p["through"]) == ("project:castling", "project closed", 3,
                                                                     a[-1]["id"])
    assert list(store.entries("project:castling")) == []
    assert store.check() == []
    assert store.prune(closed=closed) == []  # nothing left to prune
    # A late entry on the closed project links after the checkpoint.
    late = store.append(call(), S_CASTLING)
    assert late["prev"] == a[-1]["hash"] and store.check() == []


def test_unclosed_projects_and_custom_tiers(tmp_path):
    clock = Clock()
    s = JournalStore(str(tmp_path / "j.sqlite3"), clock=clock, retention={"read": 1, "write": 1, "exec": 1})
    s.append(call(risk="read"), S_NONE)
    s.append(call(risk="read"), S_CASTLING)
    clock.t += 3 * DAY
    [p] = s.prune()
    assert p["pruned"] == "general"
    assert len(list(s.entries("project:castling"))) == 1  # never pruned while open
    s.close()


# -- speed ----------------------------------------------------------------------

def test_append_is_fast_enough_for_every_call(tmp_path):
    s = JournalStore(str(tmp_path / "j.sqlite3"))
    n = 2000
    start = time.perf_counter()
    for i in range(n):
        s.append(call(call=f"c{i}"), S_CASTLING if i % 2 else S_NONE)
    per_call = (time.perf_counter() - start) / n
    assert per_call < 0.005, f"{per_call * 1000:.2f} ms per append"
    assert s.check() == []
    s.close()


def test_entries_are_copies(store):
    a = store.append(call(), S_CASTLING)
    got = store.get(a["id"])
    got["why"] = "changed"
    assert store.get(a["id"]) == a and copy.deepcopy(a) == store.query()[0]


def test_filtered_reads_walk_an_index_in_order_and_old_indexes_go(tmp_path):
    """A person's, agent's, cap's or worker's newest entries come straight off
    an index: no sort of every matching row under the store's lock."""
    path = str(tmp_path / "j.db")
    s = JournalStore(path)
    s.append({"kind": "call", "cap": "a.b", "person": "person:bake"})
    for c in ("cap", "worker", "person", "agent", "project"):   # a journal from before: (column, ts)
        s._db.execute(f"DROP INDEX journal_{c}_seq")
        s._db.execute(f"CREATE INDEX journal_{c} ON journal ({c}, ts)")
    s.close()
    s = JournalStore(path)
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"journal_person_seq", "journal_agent_seq", "journal_cap_seq"} <= names
    assert not names & {"journal_person", "journal_agent", "journal_cap", "journal_worker", "journal_project"}
    assert [e["cap"] for e in s.query(person="person:bake")] == ["a.b"]
    for where in ("person = ?", "agent = ?", "cap = ?", "worker = ?", "project = ?"):
        plan = " ".join(r[3] for r in s._db.execute(
            f"EXPLAIN QUERY PLAN SELECT body FROM journal WHERE {where} ORDER BY seq DESC LIMIT 5", ("x",)))
        assert "TEMP B-TREE" not in plan, (where, plan)
    s.close()
