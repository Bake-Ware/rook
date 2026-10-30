"""The 0.5 hub's vault file, as the migration writes it.

``<data>/vault.db``: one row per secret, the value sealed with NaCl
SecretBox under a key derived from the hub's root key
(``<data>/keys/root.key``), so the vault needs no key file of its own and a
backup of the hub's keys covers it. Names, descriptions and times are plain
(they show on the dashboard); values never leave this module except to the
caller that asked for one.

TODO(castling:vault): the hub's vault (dispatch's ``{{secret:NAME}}`` step,
plugin settings with ``secret: true``) reads this file; move this module next
to it when it lands.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import nacl.hash
import nacl.secret
from nacl.encoding import RawEncoder

PERSON = b"rook-vault-v1"           # BLAKE2b personalisation: this key is for the vault only

SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY, value BLOB NOT NULL, description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL, set_by TEXT NOT NULL)
"""


def box(data: Path) -> nacl.secret.SecretBox:
    """The vault's SecretBox: BLAKE2b of the hub's root key seed."""
    seed = bytes.fromhex((Path(data) / "keys" / "root.key").read_text(encoding="ascii").strip())
    key = nacl.hash.blake2b(b"", key=seed, person=PERSON, digest_size=32, encoder=RawEncoder)
    return nacl.secret.SecretBox(key)


def _open(data: Path) -> sqlite3.Connection:
    path = Path(data) / "vault.db"
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    return db


def put(data: Path, secrets: list[dict]) -> list[str]:
    """Store ``[{name, value, description, created, set_by}]``; a name already
    there keeps its value. Returns the names written."""
    sealer, db, written = box(data), _open(data), []
    try:
        with db:
            for s in secrets:
                if db.execute("SELECT 1 FROM secrets WHERE name=?", (s["name"],)).fetchone():
                    continue
                db.execute("INSERT INTO secrets VALUES (?,?,?,?,?,?)",
                           (s["name"], sealer.encrypt(s["value"].encode()), s.get("description") or "",
                            s.get("created") or time.time(), time.time(), s.get("set_by") or "import:0.4"))
                written.append(s["name"])
    finally:
        db.close()
    return written


def names(data: Path) -> list[str]:
    path = Path(data) / "vault.db"
    if not path.exists():
        return []
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT name FROM secrets ORDER BY name")]
    finally:
        db.close()


def get(data: Path, name: str) -> str:
    db = _open(data)
    try:
        row = db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise KeyError(name)
    return box(data).decrypt(row[0]).decode()
