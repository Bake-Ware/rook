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


# -- stores that now write through atomic_write_* ------------------------------

def _no_temp(d):
    return not [p.name for p in d.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_open_db_mode_covers_the_wal_sidecars(tmp_path):
    """vault.db was 0600 but its -wal/-shm, which hold the same rows, could
    keep the umask: ones left from before, or a SQLite that does not copy the
    database's mode. Every 0600 database gets 0600 sidecars."""
    path = tmp_path / "secret.db"
    # Left over from a release that opened the database without a mode.
    loose = sqlite3.connect(str(path))
    loose.execute("PRAGMA journal_mode=WAL")
    loose.execute("CREATE TABLE t(x)")
    loose.execute("INSERT INTO t VALUES (1)")
    loose.commit()
    for suffix in ("", "-wal", "-shm"):
        os.chmod(str(path) + suffix, 0o644)
    conn = coredb.open_db(path, mode=0o600)
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    for suffix in ("", "-wal", "-shm"):
        assert _mode(str(path) + suffix) == 0o600, suffix
    conn.close()
    loose.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_open_db_creates_sidecars_under_a_tight_umask(tmp_path, monkeypatch):
    """Even a SQLite that creates sidecars with the process umask (not the
    database's mode) makes them 0600: the umask is tightened while it opens,
    and put back afterwards."""
    real_connect = sqlite3.connect
    seen = []

    def connect(*a, **k):
        seen.append(os.umask(0o022))
        os.umask(seen[-1])
        return real_connect(*a, **k)
    monkeypatch.setattr(coredb.sqlite3, "connect", connect)
    before = os.umask(0o022)
    try:
        conn = coredb.open_db(tmp_path / "s.db", mode=0o600)
        assert seen == [0o177] and os.umask(0o022) == 0o022
        conn.close()
        coredb.open_db(tmp_path / "plain.db").close()
        assert seen[-1] == 0o022   # no mode: umask untouched
    finally:
        os.umask(before)


