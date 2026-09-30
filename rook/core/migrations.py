"""Plugin schema migrations for SQLite stores.

A plugin that keeps state in SQLite declares ``MIGRATIONS = "migrations"`` (a
directory beside its module) holding ``NNN_description.sql`` files. Each file
is applied once, in numeric order, inside a transaction; applied versions are
recorded per plugin namespace in ``_rook_migrations``, so several plugins can
share one database file. Files are never edited once released: a fix is a new,
higher-numbered file.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_NAME = re.compile(r"^(\d+)_[\w.-]+\.sql$")


def pending(conn: sqlite3.Connection, directory: Path, namespace: str) -> list[tuple[int, Path]]:
    conn.execute("CREATE TABLE IF NOT EXISTS _rook_migrations ("
                 "namespace TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, "
                 "applied REAL NOT NULL DEFAULT (julianday('now')), "
                 "PRIMARY KEY (namespace, version))")
    done = {r[0] for r in conn.execute(
        "SELECT version FROM _rook_migrations WHERE namespace=?", (namespace,))}
    found: dict[int, Path] = {}
    for f in sorted(Path(directory).glob("*.sql")) if Path(directory).is_dir() else []:
        m = _NAME.match(f.name)
        if not m:
            continue
        v = int(m.group(1))
        if v in found:
            raise ValueError(f"{namespace}: two migrations numbered {v}: {found[v].name}, {f.name}")
        found[v] = f
    return [(v, found[v]) for v in sorted(found) if v not in done]


def apply(conn: sqlite3.Connection, directory: Path | None, namespace: str) -> list[int]:
    """Apply pending migrations; returns the versions applied. A failing file
    rolls back on its own and stops the run (later files are not tried)."""
    if directory is None:
        return []
    applied = []
    if conn.in_transaction:
        conn.commit()
    saved, conn.isolation_level = conn.isolation_level, None  # we manage BEGIN/COMMIT
    try:
        return _apply(conn, directory, namespace, applied)
    finally:
        conn.isolation_level = saved


def _apply(conn: sqlite3.Connection, directory: Path, namespace: str,
           applied: list[int]) -> list[int]:
    for version, path in pending(conn, directory, namespace):
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            for stmt in _statements(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO _rook_migrations(namespace, version, name) VALUES (?,?,?)",
                         (namespace, version, path.name))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def _statements(sql: str) -> list[str]:
    """Split a script into complete statements (sqlite3.complete_statement),
    so each runs inside our transaction (executescript would commit)."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out
