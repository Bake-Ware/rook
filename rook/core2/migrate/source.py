"""A copy of a 0.4 hub's data folder, read-only.

The 0.4 hub kept its stores in one folder (``ROOK_DATA_DIR``, by default
``/var/lib/rook-band-mcp``), hub plugins under ``plugins/<name>/``. Each store
is found by its file name anywhere in the copy (the shallowest one wins), so
a copy of the folder as it was works, and so does a folder of loose files.

Only a copy is read, never the live hub: a folder under a known live path or
``$ROOK_DATA_DIR``, one with a SQLite ``-wal`` file beside a database (a live
database, or a plain copy of one that may miss recent writes), or one a
running process holds a file in is refused. Databases are opened read-only
and immutable, so not even a ``-wal`` is written.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import nacl.secret

LIVE = ("/var/lib/rook-band-mcp",)
FILES = {"access": "access.db", "accounts": "enrollment.db", "trust": "trust.json", "vault": "vault.db",
         "vault_key": "vault.key", "memory": "memory.db", "journal": "journal.db",
         "knowledge": "knowledge.db", "sessions": "sessions.db"}
HEX_ID = re.compile(r"[0-9a-f]{32}")
HUB_NAMES = {"rook"}


class NotACopy(ValueError):
    """The folder isn't a safe copy of a 0.4 hub's data."""


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_copy(root: Path) -> None:
    """Refuse a folder that looks like a live 0.4 hub's data."""
    if not root.is_dir():
        raise NotACopy(f"{root}: not a folder")
    live = [Path(p).resolve() for p in (*LIVE, os.environ.get("ROOK_DATA_DIR", "").strip()) if p]
    for p in live:
        if _within(root, p) or _within(p, root):
            raise NotACopy(f"{root} is (or holds) a live hub's data folder ({p}): migrate from a copy")
    wal = sorted(root.rglob("*-wal"))
    if wal:
        db = str(wal[0])[:-4]
        raise NotACopy(f"{wal[0]} exists: the database is live or was copied mid-write. Copy each database "
                       f"with sqlite3 {db} \".backup <copy>\" (one consistent file)")
    pid = _holder(root)
    if pid:
        raise NotACopy(f"process {pid} has a file open in {root}: stop it or migrate from a copy")


def _holder(root: Path) -> str | None:
    """A running process with a file open under ``root`` (Linux; best effort)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    me = str(os.getpid())
    for d in proc.iterdir():
        if not d.name.isdigit() or d.name == me:
            continue
        try:
            for fd in (d / "fd").iterdir():
                if _within(Path(os.readlink(fd)), root):
                    return d.name
        except OSError:
            continue
    return None


def open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _tables(db: sqlite3.Connection) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


class Source:
    """The stores found in a 0.4 copy, and plain readers over them."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        check_copy(self.root)
        self.paths: dict[str, Path] = {}
        for key, name in FILES.items():
            hits = sorted(self.root.rglob(name), key=lambda p: (len(p.parts), str(p)))
            if hits:
                self.paths[key] = hits[0]
        self._dbs: dict[str, sqlite3.Connection] = {}

    def found(self) -> dict:
        return {k: str(self.paths[k].relative_to(self.root)) if k in self.paths else None for k in FILES}

    def db(self, key: str) -> sqlite3.Connection | None:
        if key not in self.paths:
            return None
        if key not in self._dbs:
            self._dbs[key] = open_ro(self.paths[key])
        return self._dbs[key]

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()

    # -- people -------------------------------------------------------------

    def people(self) -> list[dict]:
        """Members of access.db with their active grants, their account's
        username/name (enrollment.db) and their API keys."""
        db = self.db("access")
        if db is None or "members" not in _tables(db):
            return []
        accounts = {}
        adb = self.db("accounts")
        if adb is not None and "users" in _tables(adb):
            accounts = {r["id"]: dict(r) for r in adb.execute("SELECT id, username, name FROM users")}
        tables = _tables(db)
        out = []
        for m in db.execute("SELECT * FROM members ORDER BY created, user_id"):
            acct = accounts.get(m["user_id"], {})
            grants = [] if "grants" not in tables else [
                {**dict(g), "caps": _json_list(g["caps"])}
                for g in db.execute("SELECT * FROM grants WHERE user_id=? AND revoked IS NULL ORDER BY created",
                                    (m["user_id"],))]
            tokens = [] if "tokens" not in tables else [
                dict(t) for t in db.execute("SELECT agent_id, label FROM tokens WHERE user_id=?", (m["user_id"],))]
            links = [] if "chat_links" not in tables else [
                dict(c) for c in db.execute("SELECT platform, handle FROM chat_links WHERE user_id=?", (m["user_id"],))]
            out.append({"id": m["user_id"], "role": m["role"], "label": m["label"],
                        "username": acct.get("username") or "", "name": acct.get("name") or "",
                        "created": m["created"], "grants": grants, "tokens": tokens, "chat_links": links})
        return out

    def bands(self) -> list[str]:
        db = self.db("access")
        return [r["id"] for r in db.execute("SELECT id FROM bands")] if db and "bands" in _tables(db) else []

    def spaces(self) -> list[dict]:
        db = self.db("access")
        return [dict(r) for r in db.execute("SELECT platform, space_id, name FROM spaces")] \
            if db and "spaces" in _tables(db) else []

    # -- trust ----------------------------------------------------------------

    def trust(self) -> dict | None:
        path = self.paths.get("trust")
        if path is None:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None

    # -- vault ------------------------------------------------------------------

    def secrets(self) -> tuple[list[dict], list[str]]:
        """``([{name, value, description, created, set_by}], [problems])``.
        Values are decrypted with the copy's ``vault.key``; they go only to
        the 0.5 vault, never into a report."""
        db = self.db("vault")
        if db is None or "secrets" not in _tables(db):
            return [], []
        rows = list(db.execute("SELECT name, value, description, created, set_by FROM secrets ORDER BY name"))
        if not rows:
            return [], []
        if "vault_key" not in self.paths:
            return [], [f"vault.key is not in the copy: {len(rows)} secrets can't be read (copy it beside vault.db)"]
        sealed = nacl.secret.SecretBox(self.paths["vault_key"].read_bytes())
        out, problems = [], []
        for r in rows:
            try:
                value = sealed.decrypt(r["value"]).decode()
            except Exception:                     # noqa: BLE001 - a wrong key or a damaged row: name it, no value
                problems.append(f"secret {r['name']} doesn't decrypt with this vault.key: not carried over")
                continue
            out.append({"name": r["name"], "value": value, "description": r["description"],
                        "created": r["created"], "set_by": r["set_by"]})
        return out, problems

    # -- memory -------------------------------------------------------------------

    def memories(self) -> list[dict]:
        db = self.db("memory")
        if db is None or "memories" not in _tables(db):
            return []
        approved = "approved" if "approved" in _columns(db, "memories") else "0 AS approved"
        return [dict(r) for r in db.execute(
            f"SELECT id, kind, scope, text, state, pinned, {approved}, created, updated FROM memories "
            "ORDER BY created, id")]

    # -- journal and workers --------------------------------------------------------

    def journal_stats(self) -> dict | None:
        db = self.db("journal")
        if db is None or "calls" not in _tables(db):
            return None
        r = db.execute("SELECT count(*), min(ts), max(ts) FROM calls").fetchone()
        return {"rows": r[0], "first": r[1], "last": r[2]}

    def workers(self) -> list[dict]:
        """Every machine the 0.4 hub knew: the journal's call targets and
        access.db's resources, by name (``[{name, calls, last_seen}]``)."""
        names: dict[str, str] = {}                # worker id -> name
        seen: dict[str, dict] = {}

        def add(name, calls=0, last=None):
            if not name or name.casefold() in HUB_NAMES:
                return
            w = seen.setdefault(name.casefold(), {"name": name, "calls": 0, "last_seen": None})
            w["calls"] += calls or 0
            if last and (w["last_seen"] is None or last > w["last_seen"]):
                w["last_seen"] = last

        adb = self.db("access")
        if adb is not None and "resources" in _tables(adb):
            for r in adb.execute("SELECT ref, label, seen FROM resources WHERE kind='worker'"):
                names[r["ref"]] = r["label"] or r["ref"]
                add(r["label"] or r["ref"], 0, r["seen"])
        jdb = self.db("journal")
        if jdb is not None and "calls" in _tables(jdb):
            for r in jdb.execute("SELECT worker, count(*) AS n, max(ts) AS last FROM calls "
                                 "WHERE worker IS NOT NULL AND worker != '' GROUP BY worker"):
                w = r["worker"]
                if HEX_ID.fullmatch(w) and w not in names:
                    continue                      # an id the copy can't name (audit rows)
                add(names.get(w, w), r["n"], r["last"])
        return sorted(seen.values(), key=lambda w: w["name"].casefold())

    def worker_names(self) -> dict[str, str]:
        """0.4 worker id -> name, from access.db's resources."""
        adb = self.db("access")
        if adb is None or "resources" not in _tables(adb):
            return {}
        return {r["ref"]: r["label"] for r in adb.execute("SELECT ref, label FROM resources WHERE kind='worker'")
                if r["label"]}


def _json_list(v) -> list:
    try:
        out = json.loads(v or "[]")
    except ValueError:
        return []
    return [str(x) for x in out] if isinstance(out, list) else []
