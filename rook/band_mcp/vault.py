"""Secret vault: credentials agents can use, with every access on the record.

Values are encrypted at rest (NaCl SecretBox) with a key kept in a separate
0600 file next to the database. That protects copies and backups of the
database; it does not protect against root on the hub, and doesn't pretend to.

Agents use a secret two ways:

* ``{{secret:<name>}}`` inside ``rook_call`` args. The hub substitutes the
  value just before dispatch, journals the placeholder, and masks the value in
  the reply, so it never reaches the agent's context (preferred).
* ``rook_secret(action="get")`` returns the raw value (audited).

Every read, use, set and delete is written to the access log. When a value is
set, the hub also masks it anywhere it already appears in the call journal.
Nothing here gates a band call; an unknown placeholder only fails that call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time

import nacl.secret
import nacl.utils

log = logging.getLogger("rook.band_mcp.vault")

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{secret:([a-z0-9][a-z0-9._-]{0,63})\}\}")
MASK = "***"
MAX_VALUE = 16384


class Vault:
    def __init__(self, path: str, key_path: str | None = None) -> None:
        self.path = path
        self.key_path = key_path or os.path.join(os.path.dirname(path) or ".", "vault.key")
        self._lock = threading.Lock()
        self._box = nacl.secret.SecretBox(self._load_key())
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS secrets(
                name TEXT PRIMARY KEY, value BLOB NOT NULL, description TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, set_by TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS access(
                seq INTEGER PRIMARY KEY, name TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, via TEXT, task TEXT, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS access_name ON access(name, seq);
        """)
        os.chmod(path, 0o600)

    def _load_key(self) -> bytes:
        try:
            with open(self.key_path, "rb") as f:
                key = f.read()
            if len(key) != nacl.secret.SecretBox.KEY_SIZE:
                raise ValueError("vault key has the wrong size")
            return key
        except FileNotFoundError:
            key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            log.warning("created new vault key at %s — back it up; without it the vault is unreadable", self.key_path)
            return key

    # -- audit ------------------------------------------------------------------

    def _audit(self, name, actor, action, via=None, task=None):
        self._db.execute("INSERT INTO access(name,actor,action,via,task,ts) VALUES(?,?,?,?,?,?)",
                         (name, actor, action, via, task, time.time()))

    def access_log(self, name: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name,actor,action,via,task,ts FROM access" + (" WHERE name=?" if name else "")
                + " ORDER BY seq DESC LIMIT ?", ((name, limit) if name else (limit,))).fetchall()
        return [dict(zip(("name", "actor", "action", "via", "task", "ts"), r)) for r in rows]

    # -- secrets ----------------------------------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT s.name,s.description,s.created,s.updated,s.set_by,"
                "(SELECT max(ts) FROM access a WHERE a.name=s.name AND a.action IN ('get','use')) "
                "FROM secrets s ORDER BY s.name").fetchall()
        return [dict(zip(("name", "description", "created", "updated", "set_by", "last_used"), r)) for r in rows]

    def get(self, name: str, actor: str, via: str = "get", task: str | None = None) -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            with self._db:
                self._audit(name, actor, "get" if via == "get" else "use", via, task)
        return self._box.decrypt(row[0]).decode()

    def set(self, name: str, value: str, description: str, actor: str) -> dict:
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError("Secret names are lowercase letters, digits, '.', '_' and '-' (max 64)")
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE:
            raise ValueError(f"Value must be 1–{MAX_VALUE} characters")
        description = (description or "").strip()[:500]
        now = time.time()
        blob = self._box.encrypt(value.encode())
        with self._lock, self._db:
            old = self._db.execute("SELECT description,created FROM secrets WHERE name=?", (name,)).fetchone()
            self._db.execute("INSERT OR REPLACE INTO secrets VALUES(?,?,?,?,?,?)",
                             (name, blob, description or (old[0] if old else ""),
                              old[1] if old else now, now, actor))
            self._audit(name, actor, "replace" if old else "create")
        return {"name": name, "replaced": bool(old)}

    def delete(self, name: str, actor: str) -> bool:
        with self._lock, self._db:
            gone = self._db.execute("DELETE FROM secrets WHERE name=?", (name,)).rowcount
            if gone:
                self._audit(name, actor, "delete")
        return bool(gone)

    # -- placeholders -------------------------------------------------------------

    def names_in(self, obj) -> set[str]:
        found: set[str] = set()

        def walk(o):
            if isinstance(o, str):
                found.update(PLACEHOLDER.findall(o))
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, (list, tuple)):
                for v in o:
                    walk(v)
        walk(obj)
        return found

    def substitute(self, obj, actor: str, via: str, task: str | None = None):
        """Return (obj with placeholders replaced, {name: value} used).
        Raises KeyError naming the first unknown secret."""
        values = {n: self.get(n, actor, via=via, task=task) for n in sorted(self.names_in(obj))}

        def walk(o):
            if isinstance(o, str):
                return PLACEHOLDER.sub(lambda m: values[m.group(1)], o)
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(v) for v in o]
            if isinstance(o, tuple):
                return tuple(walk(v) for v in o)
            return o
        return walk(obj), values


def mask(obj, values):
    """Replace any occurrence of the given secret values in a JSON-able object."""
    secrets = [v for v in values if v and len(v) >= 4]
    if not secrets:
        return obj

    def walk(o):
        if isinstance(o, str):
            for v in secrets:
                o = o.replace(v, MASK)
            return o
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o
    return walk(obj)


def encoded_forms(value: str) -> list[str]:
    """The value as it appears raw, and inside once- and twice-JSON-encoded
    text (e.g. a worker's stdout that itself contains JSON, stored in the
    journal as JSON). Longest first, so a longer form is masked before a
    shorter one it contains."""
    forms = {value}
    for _ in range(2):
        forms |= {json.dumps(f)[1:-1] for f in forms} | {json.dumps(f, ensure_ascii=False)[1:-1] for f in forms}
    return sorted((f for f in forms if len(f) >= 4), key=len, reverse=True)
