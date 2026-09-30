"""The hub settings store: ``settings.db`` (docs/design/settings.md 3.4).

One sqlite file shared by the MCP server and the dashboard. It sits beside
``enrollment.db`` (both processes must already share that one, or bands break)
unless ``ROOK_SETTINGS_DB`` names another path, so the two processes cannot
disagree about where settings live.

Tables:

* ``settings(key, scope, target, value_json, secret_ref, rev, updated_by, updated_at)``
  - ``target`` is ``''`` for hub, a band id, a worker name or a user id.
  - Secrets never enter this file: ``secret_ref`` names the vault entry and
    ``value_json`` is null.
* ``history(...)``: every change, attributed; secrets as fingerprints only.
* ``runtime(process, data_json, updated_at)``: what each hub process saw at
  start (env-locked keys, legacy-file values, store paths, conflicts), so the
  UI can explain a process whose environment the other process cannot read.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCOPES = ("hub", "band", "worker", "user")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
    value_json TEXT, secret_ref TEXT, rev INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
    PRIMARY KEY(key, scope, target));
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
    old_json TEXT, new_json TEXT, actor_kind TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '', actor_label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', at REAL NOT NULL, source TEXT NOT NULL DEFAULT 'api');
CREATE INDEX IF NOT EXISTS history_key ON history(key, scope, target, id);
CREATE TABLE IF NOT EXISTS runtime(
    process TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at REAL NOT NULL);
"""


def default_path() -> Path:
    explicit = os.environ.get("ROOK_SETTINGS_DB")
    if explicit:
        return Path(explicit).expanduser()
    enroll = os.environ.get("ROOK_ENROLLMENT_DB")
    if enroll:
        return Path(enroll).expanduser().with_name("settings.db")
    from ..remote import setup_store
    return setup_store.setup_path().with_name("settings.db")


def split_actor(actor: str) -> tuple[str, str, str]:
    """``human:alice`` -> (human, alice, human:alice); ``agent:ci`` likewise."""
    actor = str(actor or "system")
    kind, sep, rest = actor.partition(":")
    if not sep:
        return "system", actor, actor
    return kind, rest, actor


class SettingsStore:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._ready = False

    # -- plumbing ----------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def _init(self) -> None:
        if self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.executescript(_SCHEMA)
            db.commit()
        finally:
            db.close()
        self._ready = True

    @contextmanager
    def _db(self, write: bool = False):
        self._init()
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"key": r["key"], "scope": r["scope"], "target": r["target"],
                "value": json.loads(r["value_json"]) if r["value_json"] is not None else None,
                "secret_ref": r["secret_ref"], "rev": r["rev"],
                "updated_by": r["updated_by"], "updated_at": r["updated_at"]}

    # -- reads -------------------------------------------------------------
    def get(self, key: str, scope: str, target: str = "") -> dict | None:
        if not self.exists():
            return None
        with self._db() as db:
            r = db.execute("SELECT * FROM settings WHERE key=? AND scope=? AND target=?",
                           (key, scope, target or "")).fetchone()
        return self._row(r) if r else None

    def rows(self, scope: str | None = None, target: str | None = None,
             prefix: str = "") -> list[dict]:
        if not self.exists():
            return []
        q, args = "SELECT * FROM settings WHERE key LIKE ?", [prefix.replace("%", "") + "%"]
        if scope is not None:
            q += " AND scope=?"
            args.append(scope)
        if target is not None:
            q += " AND target=?"
            args.append(target)
        with self._db() as db:
            return [self._row(r) for r in db.execute(q + " ORDER BY key, scope, target", args)]

    def namespace_values(self, namespace: str, scope: str = "hub", target: str = "") -> dict:
        """``{name: value}`` of non-secret rows under ``<namespace>.`` (what a
        plugin's SettingsView reads as stored values)."""
        out = {}
        for r in self.rows(scope, target, prefix=namespace + "."):
            if r["secret_ref"]:
                continue
            out[r["key"][len(namespace) + 1:]] = r["value"]
        return out

    def history(self, key: str | None = None, scope: str | None = None,
                target: str | None = None, limit: int = 50) -> list[dict]:
        if not self.exists():
            return []
        q, args = "SELECT * FROM history WHERE 1=1", []
        for col, val in (("key", key), ("scope", scope), ("target", target)):
            if val is not None:
                if col == "key" and val.endswith("."):
                    q += " AND key LIKE ?"
                    args.append(val + "%")
                else:
                    q += f" AND {col}=?"
                    args.append(val)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit or 50), 500)))
        with self._db() as db:
            rows = db.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append({"id": r["id"], "key": r["key"], "scope": r["scope"], "target": r["target"],
                        "old": json.loads(r["old_json"]) if r["old_json"] else None,
                        "new": json.loads(r["new_json"]) if r["new_json"] else None,
                        "actor": r["actor_label"], "actor_kind": r["actor_kind"],
                        "note": r["note"], "at": r["at"], "source": r["source"]})
        return out

    # -- writes ------------------------------------------------------------
    def set(self, key: str, scope: str, target: str, *, value: Any = None,
            secret_ref: str | None = None, actor: str = "system", note: str = "",
            source: str = "api", old_shown: Any = None, new_shown: Any = None,
            expect_rev: int | None = None) -> dict:
        """Write one row and its history entry. ``old_shown``/``new_shown`` are
        what history records (the caller passes fingerprints for secrets)."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        target = target or ""
        kind, ident, label = split_actor(actor)
        now = time.time()
        with self._lock, self._db(write=True) as db:
            cur = db.execute("SELECT * FROM settings WHERE key=? AND scope=? AND target=?",
                             (key, scope, target)).fetchone()
            if expect_rev is not None and cur is not None and cur["rev"] != expect_rev:
                raise ValueError(f"{key} changed since you loaded it (rev {cur['rev']}); reload")
            rev = (cur["rev"] + 1) if cur else 1
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?,?,?,?,?,?)",
                       (key, scope, target,
                        None if secret_ref else json.dumps(value),
                        secret_ref, rev, label, now))
            old = old_shown if old_shown is not None else (
                None if cur is None else (json.loads(cur["value_json"])
                                          if cur["value_json"] is not None else cur["secret_ref"]))
            new = new_shown if new_shown is not None else (secret_ref or value)
            db.execute("INSERT INTO history(key,scope,target,old_json,new_json,actor_kind,"
                       "actor_id,actor_label,note,at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key, scope, target, json.dumps(old), json.dumps(new), kind, ident,
                        label, str(note or "")[:500], now, source))
        return {"key": key, "scope": scope, "target": target, "rev": rev}

    def delete(self, key: str, scope: str, target: str = "", *, actor: str = "system",
               note: str = "", source: str = "api", old_shown: Any = None) -> bool:
        target = target or ""
        kind, ident, label = split_actor(actor)
        with self._lock, self._db(write=True) as db:
            cur = db.execute("SELECT * FROM settings WHERE key=? AND scope=? AND target=?",
                             (key, scope, target)).fetchone()
            if cur is None:
                return False
            db.execute("DELETE FROM settings WHERE key=? AND scope=? AND target=?",
                       (key, scope, target))
            old = old_shown if old_shown is not None else (
                json.loads(cur["value_json"]) if cur["value_json"] is not None else cur["secret_ref"])
            db.execute("INSERT INTO history(key,scope,target,old_json,new_json,actor_kind,"
                       "actor_id,actor_label,note,at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key, scope, target, json.dumps(old), None, kind, ident, label,
                        str(note or "")[:500], time.time(), source))
        return True

    def record(self, key: str, scope: str, target: str, *, old: Any, new: Any,
               actor: str, note: str = "", source: str = "api") -> None:
        """History-only entry for a change stored elsewhere (plugin toggles on a
        worker, a band key rotation, an import conflict)."""
        kind, ident, label = split_actor(actor)
        with self._lock, self._db(write=True) as db:
            db.execute("INSERT INTO history(key,scope,target,old_json,new_json,actor_kind,"
                       "actor_id,actor_label,note,at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (key, scope, target or "", json.dumps(old), json.dumps(new), kind,
                        ident, label, str(note or "")[:500], time.time(), source))

    # -- runtime reports ---------------------------------------------------
    def report_runtime(self, process: str, data: dict) -> None:
        with self._lock, self._db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?,?)",
                       (process, json.dumps(data, default=str), time.time()))

    def runtime(self, process: str | None = None) -> dict:
        if not self.exists():
            return {}
        with self._db() as db:
            rows = db.execute("SELECT * FROM runtime").fetchall()
        out = {r["process"]: {**json.loads(r["data_json"]), "updated_at": r["updated_at"]}
               for r in rows}
        return out.get(process, {}) if process else out
