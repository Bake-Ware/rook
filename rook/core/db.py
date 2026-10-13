"""Shared persistence helpers: SQLite connections and atomic file writes.

Two connection styles are in use and both are kept, because they suit
different stores:

* :func:`open_db` — one long-lived connection a store holds for its lifetime
  and serializes itself (usually ``check_same_thread=False`` plus a lock).
* :func:`transaction` — a short connection per call that commits on success,
  rolls back on error and always closes. Stores shared by two processes (the
  MCP server and the dashboard) use this so neither holds a lock open.

Schema setup stays with each store: plugin stores pass ``migrations``/``ns``
(see :mod:`rook.core.migrations`); older stores keep ``CREATE TABLE IF NOT
EXISTS`` and backfill new columns with :func:`add_columns`, which is safe to
run on every open against a database written by any earlier release.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import migrations as _migrations

__all__ = ["open_db", "transaction", "add_columns", "secure_file",
           "atomic_write_text", "atomic_write_json"]


def _is_file(path) -> bool:
    p = str(path)
    return bool(p) and p != ":memory:" and not p.startswith("file:")


def secure_file(path, mode: int = 0o600) -> None:
    """Create ``path`` (if missing) with ``mode`` before SQLite opens it, so a
    secret-bearing database is never briefly world-readable, then enforce the
    mode on an existing file. Best-effort where the OS has no POSIX modes."""
    if not _is_file(path):
        return
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, mode))
    except OSError:
        pass
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


#: SQLite's sidecar files: the WAL, its shared-memory index, the rollback journal.
SIDECARS = ("-wal", "-shm", "-journal")


def _secure_sidecars(path, mode: int) -> None:
    """chmod existing sidecars of ``path`` to ``mode``. SQLite normally
    creates them with the database file's mode, but ones left by an older
    release (or a build that does not copy the mode) keep the umask."""
    for suffix in SIDECARS:
        try:
            os.chmod(str(path) + suffix, mode)
        except OSError:
            pass


@contextmanager
def _umask_for(mode: int | None) -> Iterator[None]:
    """Tighten the umask to match ``mode`` while SQLite creates files. The
    umask is per process: another thread creating a file in this window gets
    a stricter mode than usual (never a looser one); the window is the
    connect and its first pragma."""
    if mode is None or os.name != "posix":
        yield
        return
    old = os.umask(0o777 & ~mode)
    try:
        yield
    finally:
        os.umask(old)


def _setup(conn: sqlite3.Connection, *, wal: bool, row_factory: bool,
           foreign_keys: bool, synchronous: str | None) -> None:
    if row_factory:
        conn.row_factory = sqlite3.Row
    if wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Read-only media or a database another connection holds in a
            # transaction: the store still works in its current mode.
            pass
    if synchronous:
        conn.execute(f"PRAGMA synchronous={synchronous}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")


def open_db(path, *, wal: bool = True, row_factory: bool = True,
            check_same_thread: bool = False, timeout: float = 10.0,
            isolation_level: Any = "", foreign_keys: bool = False,
            synchronous: str | None = None, mode: int | None = None,
            mkdir: bool = True, migrations: Path | None = None,
            ns: str | None = None) -> sqlite3.Connection:
    """Open a long-lived connection.

    ``check_same_thread`` defaults to False because every long-lived store is
    used from asyncio's executor threads and serializes access itself.
    ``isolation_level`` is passed through when given (``None`` = autocommit);
    the default keeps sqlite3's own. ``mode`` creates/chmods the file first
    (see :func:`secure_file`), and its ``-wal``/``-shm``/``-journal``
    sidecars get it too: they hold the same rows. ``migrations``/``ns`` apply
    plugin migrations (:func:`rook.core.migrations.apply`) before returning."""
    if mkdir and _is_file(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    secure = mode is not None and _is_file(path)
    if secure:
        secure_file(path, mode)
        _secure_sidecars(path, mode)
    kw: dict = {"timeout": timeout, "check_same_thread": check_same_thread}
    if isolation_level != "":
        kw["isolation_level"] = isolation_level
    with _umask_for(mode if secure else None):
        conn = sqlite3.connect(str(path), **kw)
        try:
            _setup(conn, wal=wal, row_factory=row_factory, foreign_keys=foreign_keys,
                   synchronous=synchronous)
        except BaseException:
            conn.close()
            raise
    if secure:
        _secure_sidecars(path, mode)
    try:
        if migrations is not None:
            if not ns:
                raise ValueError("migrations need a namespace (ns=)")
            _migrations.apply(conn, migrations, ns)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(path, *, begin: str | None = "IMMEDIATE", timeout: float = 10.0,
                row_factory: bool = True, foreign_keys: bool = False,
                wal: bool = False) -> Iterator[sqlite3.Connection]:
    """A per-call connection: ``BEGIN <begin>`` (``IMMEDIATE`` takes the write
    lock up front, so a read-decide-write is atomic across processes;
    ``None`` lets sqlite3 open a deferred transaction on the first write),
    commit on success, roll back on any error, always close."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        _setup(conn, wal=wal, row_factory=row_factory, foreign_keys=foreign_keys,
               synchronous=None)
        if begin:
            conn.execute("BEGIN " + begin)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    """Add each missing ``name -> type/default`` column to ``table`` (the
    additive backfill older stores do on open). Returns the names added."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def atomic_write_text(path, text: str, mode: int | None = 0o600, *,
                      encoding: str = "utf-8", fsync: bool = True) -> None:
    """Replace ``path`` with ``text`` so readers see the old or the new file,
    never a torn one: write a temp file beside it, then ``os.replace``.
    ``mode`` is applied to the new file (``None`` keeps the temp file's 0600).
    Where the path cannot be replaced (a bind-mounted single file: EBUSY or
    EXDEV) it is rewritten in place and fsync'd instead, keeping ``mode``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        try:
            os.replace(tmp, p)
        except OSError as e:
            if e.errno not in _IN_PLACE_ERRNOS:
                raise
            # A single file bind-mounted into a container (ROOK_SETUP_PATH
            # in compose) cannot be replaced (EBUSY), nor can a file across
            # a mount boundary (EXDEV): write it in place instead. Not atomic,
            # so fsync'd; the temp file goes.
            _write_in_place(p, text, mode, encoding)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: os.replace failures that mean "this path cannot be swapped", not "broken".
_IN_PLACE_ERRNOS = {errno.EBUSY, errno.EXDEV}


def _write_in_place(p: Path, text: str, mode: int | None, encoding: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(p), flags, mode if mode is not None else 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode(encoding))
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        try:
            os.chmod(str(p), mode)
        except OSError:
            pass


def atomic_write_json(path, obj: Any, mode: int | None = 0o600, **dumps: Any) -> None:
    """:func:`atomic_write_text` of ``json.dumps(obj, **dumps)``."""
    atomic_write_text(path, json.dumps(obj, **dumps), mode)
