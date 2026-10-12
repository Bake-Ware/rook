"""rook.core.db helpers, and stores opening databases written by older code."""
import json
import os
import sqlite3
import stat
import threading

import pytest

from rook.core import db as coredb


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_open_db_defaults(tmp_path):
    path = tmp_path / "sub" / "a.db"
    conn = coredb.open_db(path, mode=0o600)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert conn.execute("SELECT x FROM t").fetchone()["x"] == 1
    assert _mode(path) == 0o600
    # Long-lived connections are shared with executor threads.
    out = []
    t = threading.Thread(target=lambda: out.append(conn.execute("SELECT count(*) FROM t").fetchone()[0]))
    t.start()
    t.join()
    assert out == [1]
    conn.close()


def test_open_db_options(tmp_path):
    conn = coredb.open_db(tmp_path / "b.db", wal=False, row_factory=False, isolation_level=None,
                          foreign_keys=True, synchronous="NORMAL")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert conn.isolation_level is None
    conn.close()


def test_open_db_applies_migrations(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_init.sql").write_text("CREATE TABLE t(x);")
    conn = coredb.open_db(tmp_path / "c.db", migrations=mig, ns="demo")
    assert conn.execute("SELECT version FROM _rook_migrations WHERE namespace='demo'").fetchall()[0][0] == 1
    conn.close()
    with pytest.raises(ValueError):
        coredb.open_db(tmp_path / "d.db", migrations=mig)


def test_transaction_commits_and_rolls_back(tmp_path):
    path = tmp_path / "e.db"
    with coredb.transaction(path) as db:
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        with coredb.transaction(path) as db:
            db.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError
    with coredb.transaction(path, begin=None) as db:
        assert [r["x"] for r in db.execute("SELECT x FROM t")] == [1]


def test_add_columns_is_idempotent(tmp_path):
    conn = coredb.open_db(tmp_path / "f.db")
    conn.execute("CREATE TABLE t(a)")
    assert coredb.add_columns(conn, "t", {"b": "TEXT", "c": "INTEGER NOT NULL DEFAULT 0"}) == ["b", "c"]
    assert coredb.add_columns(conn, "t", {"b": "TEXT"}) == []
    conn.close()


def test_atomic_write(tmp_path):
    p = tmp_path / "dir" / "state.json"
    coredb.atomic_write_json(p, {"a": "café"}, indent=1)
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": "café"}
    assert _mode(p) == 0o600
    coredb.atomic_write_text(p, "x", 0o644)
    assert p.read_text() == "x" and _mode(p) == 0o644
    assert sorted(os.listdir(p.parent)) == ["state.json"]  # no temp files left behind

    class Boom:
        pass
    with pytest.raises(TypeError):
        coredb.atomic_write_json(p, Boom())
    assert p.read_text() == "x"
    assert sorted(os.listdir(p.parent)) == ["state.json"]


# -- stores opening databases written by the pre-helper code ------------------

def test_journal_backfills_an_old_database(tmp_path):
    path = str(tmp_path / "journal.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE calls (seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, ts REAL, "
                "identity TEXT, cap TEXT, worker TEXT, thread_id TEXT, ok INTEGER, error TEXT, reply TEXT)")
    old.execute("INSERT INTO calls (call_id, ts, identity, cap, worker, ok, reply) "
                "VALUES ('old', 1.0, 'me', 'x.y', 'w', 1, '{}')")
    old.commit()
    old.close()
    from rook.band_mcp.journal import Journal
    j = Journal(path)
    assert j.enabled
    j.record(cap="a.b", worker="w", identity="me", args={}, reply={"ok": True},
             authz={"decision": "allow", "policy_rev": 3})
    rows = j.query(limit=10)
    assert {r["call_id"] for r in rows} >= {"old"}
    cols = {r[1] for r in j._db.execute("PRAGMA table_info(calls)")}
    assert {"agent_id", "key_id", "auth", "actor", "principal", "decision", "rule",
            "policy_rev", "tier"} <= cols


def test_console_store_backfills_transport(tmp_path):
    path = str(tmp_path / "console.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY, title TEXT, worker TEXT, worker_name TEXT, "
                "handle TEXT, cmd TEXT, pty INTEGER, state TEXT, opened_by TEXT, participants TEXT, "
                "created REAL, last_activity REAL, ended REAL, exit_code INTEGER, bytes INTEGER, "
                "cursor INTEGER, summary TEXT)")
    old.execute("INSERT INTO rooms (id, state) VALUES ('r1', 'frozen')")
    old.commit()
    old.close()
    from rook.band_mcp.console_rooms import ConsoleStore
    store = ConsoleStore(path)
    assert store.enabled
    assert store._db.execute("SELECT transport FROM rooms WHERE id='r1'").fetchone()[0] == "proc"


def test_enrollment_backfills_deleted_and_keeps_mode(tmp_path):
    path = tmp_path / "enrollment.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE bands (id TEXT PRIMARY KEY, name TEXT NOT NULL, psk TEXT NOT NULL, "
                "psk_hash TEXT UNIQUE NOT NULL, hub TEXT NOT NULL DEFAULT '', "
                "is_primary INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, "
                "epoch INTEGER NOT NULL DEFAULT 1)")
    old.execute("INSERT INTO bands(id,name,psk,psk_hash) VALUES('b1','band','p','h')")
    old.commit()
    old.close()
    os.chmod(path, 0o644)
    from rook.remote.enrollment import EnrollmentStore
    store = EnrollmentStore(path)
    assert _mode(path) == 0o600
    with store._db() as db:
        assert db.execute("SELECT deleted FROM bands WHERE id='b1'").fetchone()["deleted"] == 0


def test_work_stores_share_one_file_both_ways(tmp_path):
    """Hub and worker ship separately: each opens what the other wrote, and a
    file from the pre-index code gets its work_index backfilled."""
    from rook.remote.work_web import WorkStore
    from rook.worker.work_runtime import RuntimeStore
    path = tmp_path / "work.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE work_sessions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "updated REAL NOT NULL, state TEXT NOT NULL)")
    old.execute("CREATE TABLE work_commands(session TEXT NOT NULL, id TEXT NOT NULL, "
                "result TEXT NOT NULL, PRIMARY KEY(session,id))")
    state = {"id": "s1", "owner": "host", "revision": 1, "updated": 1.0, "items": {"a": {}}}
    old.execute("INSERT INTO work_sessions VALUES('s1','host',1.0,?)", (json.dumps(state),))
    old.execute("INSERT INTO work_commands VALUES('s1','c1',?)", (json.dumps({"status": "submitted"}),))
    old.commit()
    old.close()
    rt = RuntimeStore(path)
    assert rt.get("s1")["items"] == {"a": {}}
    assert "items" not in rt.all(details=False)[0]
    assert rt.revision("s1", "host") == 1
    hub = WorkStore(path)                       # adds actor + session_links
    assert hub.result("s1", "c1") == {"status": "submitted"}
    assert hub.claim("s1", "c2", "human:a") and not hub.claim("s1", "c2", "human:a")
    rt2 = RuntimeStore(path)                    # worker code on the hub's file
    assert rt2.claim("s1", "c3") and rt2.result("s1", "c3") == {"status": "accepted"}
    assert _mode(path) == 0o600
