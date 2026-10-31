"""The hub's vault (castling gate R1): ``<data>/vault.db``.

One row per secret, its value sealed with NaCl SecretBox under the vault's
data key. The data key is random and kept twice, sealed:

- under a key derived from the hub's root key (``<data>/keys/root.key``),
  so the hub unlocks the vault on start with nothing more;
- under a key derived from the owner's recovery key (castling-backup), so
  a vault copied to a new hub (new root key) opens again with
  ``python -m rook.core2.hub --config hub.yaml recover-vault``.

Names, descriptions, times and who set them are plain (they show in
``secret.list``). Values are only decrypted for one use: a ``{{secret:NAME}}``
reference in a call to a worker (:func:`resolver`), a hub plugin's secret
setting (:meth:`Vault.setting`), or ``secret.reveal``, a hard stop. Nothing
here caches a value, so the CLI can change the file while the hub runs.

The migration tool (:mod:`rook.core2.migrate`) writes through :meth:`Vault.put`.
"""
from __future__ import annotations

import base64
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import nacl.exceptions
import nacl.hash
import nacl.secret
import nacl.utils
from nacl.encoding import RawEncoder

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A reference anywhere in a string: "Bearer {{secret:gh-token}}" works.
REF = re.compile(r"\{\{\s*secret:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*\}\}")
LONE_REF = re.compile(r"\s*" + REF.pattern + r"\s*")
# Band keys belong to enrollment's membership flow (core.trust.set, an owner
# in person); the vault never hands one out.
RESERVED = re.compile(r"band-psk-.*")

ROOT = b"rook-vault-v1"          # BLAKE2b personalisation: the root key's vault key
RECOVERY = b"rook-vault-rk1"     # ... and the recovery key's

SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY, value BLOB NOT NULL, description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL, set_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wraps (kind TEXT PRIMARY KEY, box BLOB NOT NULL);
"""


class VaultError(Exception):
    pass


class Locked(VaultError):
    pass


def _derive(material: bytes, person: bytes) -> nacl.secret.SecretBox:
    key = nacl.hash.blake2b(b"", key=material, person=person, digest_size=32, encoder=RawEncoder)
    return nacl.secret.SecretBox(key)


def root_box(data: Path) -> nacl.secret.SecretBox:
    seed = bytes.fromhex((Path(data) / "keys" / "root.key").read_text(encoding="ascii").strip())
    return _derive(seed, ROOT)


def canonical(key: str) -> str:
    """The recovery key as the hub prints it, from however the owner wrote
    it down (dashes, spaces and case don't matter)."""
    raw = re.sub(r"[\s-]", "", str(key)).upper()
    try:
        ok = len(raw) == 32 and len(base64.b32decode(raw)) == 20
    except ValueError:
        ok = False
    if not ok:
        raise VaultError("that isn't a recovery key (32 letters and digits, in groups of four)")
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def recovery_box(key: str) -> nacl.secret.SecretBox:
    """160 random bits, so no password stretching is needed."""
    return _derive(base64.b32decode(canonical(key).replace("-", "")), RECOVERY)


class Vault:
    def __init__(self, data: str | Path):
        self.data = Path(data)
        self.path = self.data / "vault.db"
        self._box: nacl.secret.SecretBox | None = None
        self._lock = threading.Lock()

    @property
    def locked(self) -> bool:
        return self._box is None

    def _db(self) -> sqlite3.Connection:
        if not self.path.exists():
            self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))    # a racing opener is fine
        db = sqlite3.connect(self.path, timeout=10)
        db.executescript(SCHEMA)
        return db

    def unlock(self) -> "Vault":
        """Open the data key with the hub's root key; a new vault gets a new
        data key. The key is kept under the root key and under the recovery
        key owners are shown (made now if the hub has none yet; re-sealed if
        that key changed), so the shown key always opens it. Raises
        :class:`Locked` when the file was sealed by another hub."""
        from rook.core2.enroll.hub import recovery_key

        root = root_box(self.data)
        with self._lock:
            db = self._db()
            db.isolation_level = None
            try:
                db.execute("BEGIN IMMEDIATE")      # one data key, even if the CLI opens it at the same time
                wraps = dict(db.execute("SELECT kind, box FROM wraps"))
                if "root" in wraps:
                    try:
                        dek = root.decrypt(wraps["root"])
                    except nacl.exceptions.CryptoError:
                        raise Locked("vault.db was sealed by another hub's root key: run recover-vault "
                                     "with the owner's recovery key") from None
                elif db.execute("SELECT 1 FROM secrets LIMIT 1").fetchone():
                    raise Locked("vault.db has secrets but no keys: it can't be opened")
                else:
                    dek = nacl.utils.random(32)
                    db.execute("INSERT INTO wraps VALUES ('root', ?)", (root.encrypt(dek),))
                rec = recovery_box(recovery_key(self.data))
                try:
                    ok = rec.decrypt(wraps["recovery"]) == dek
                except (KeyError, nacl.exceptions.CryptoError):
                    ok = False
                if not ok:
                    db.execute("INSERT OR REPLACE INTO wraps VALUES ('recovery', ?)", (rec.encrypt(dek),))
                db.execute("COMMIT")
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                db.close()
            self._box = nacl.secret.SecretBox(dek)
        return self

    @staticmethod
    def _wrap(db: sqlite3.Connection, kind: str, box: nacl.secret.SecretBox, dek: bytes) -> None:
        with db:
            db.execute("INSERT OR REPLACE INTO wraps VALUES (?, ?)", (kind, box.encrypt(dek)))

    def recover(self, key: str) -> None:
        """Open the vault with the owner's recovery key and seal its data key
        under this hub's root key (the vault came from a hub that died). The
        recovery key becomes this hub's again, so the owner keeps one key."""
        from rook.core2.enroll.hub import _write_private

        if not self.path.exists():
            raise VaultError(f"no vault at {self.path}")
        box = recovery_box(key)
        with self._lock:
            db = self._db()
            try:
                row = db.execute("SELECT box FROM wraps WHERE kind='recovery'").fetchone()
                if row is None:
                    raise VaultError("this vault has no recovery key")
                try:
                    dek = box.decrypt(row[0])
                except nacl.exceptions.CryptoError:
                    raise VaultError("that recovery key doesn't open this vault") from None
                self._wrap(db, "root", root_box(self.data), dek)
            finally:
                db.close()
            (self.data / "keys").mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private(self.data / "keys" / "recovery.key", canonical(key) + "\n")
            self._box = nacl.secret.SecretBox(dek)

    # -- secrets ---------------------------------------------------------------

    def _open(self) -> tuple[nacl.secret.SecretBox, sqlite3.Connection]:
        if self._box is None:
            raise Locked("the vault is locked: recover it with the owner's recovery key (recover-vault)")
        return self._box, self._db()

    def list(self) -> list[dict]:
        """Every secret's name, description, times and setter; never a value."""
        if not self.path.exists():
            return []
        db = self._db()
        try:
            rows = db.execute("SELECT name, description, created, updated, set_by FROM secrets ORDER BY name")
            return [dict(zip(("name", "description", "created", "updated", "set_by"), r)) for r in rows]
        finally:
            db.close()

    def names(self) -> list[str]:
        return [s["name"] for s in self.list()]

    def get(self, name: str) -> str:
        """One value, decrypted now. KeyError when there is none."""
        box, db = self._open()
        try:
            row = db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise KeyError(name)
        return box.decrypt(row[0]).decode()

    def set(self, name: str, value: str, by: str, description: str | None = None) -> dict:
        """Store or replace a value. ``description`` None keeps the old one."""
        _check_name(name)
        if not isinstance(value, str) or not value:
            raise VaultError("a secret's value is a non-empty string")
        box, db = self._open()
        now = time.time()
        try:
            with db:
                old = db.execute("SELECT description, created FROM secrets WHERE name=?", (name,)).fetchone()
                desc = description if description is not None else (old[0] if old else "")
                db.execute("INSERT OR REPLACE INTO secrets VALUES (?,?,?,?,?,?)",
                           (name, box.encrypt(value.encode()), desc, old[1] if old else now, now, by))
        finally:
            db.close()
        return {"name": name, "description": desc, "updated": now, "set_by": by, "replaced": old is not None}

    def delete(self, name: str) -> bool:
        db = self._db()
        try:
            with db:
                return db.execute("DELETE FROM secrets WHERE name=?", (name,)).rowcount > 0
        finally:
            db.close()

    def put(self, secrets: list[dict]) -> list[str]:
        """The migration's import: ``[{name, value, description, created,
        set_by}]``; a name already here keeps its value. Returns the names
        written."""
        box, db = self._open()
        written = []
        try:
            with db:
                for s in secrets:
                    if db.execute("SELECT 1 FROM secrets WHERE name=?", (s["name"],)).fetchone():
                        continue
                    db.execute("INSERT INTO secrets VALUES (?,?,?,?,?,?)",
                               (s["name"], box.encrypt(s["value"].encode()), s.get("description") or "",
                                s.get("created") or time.time(), time.time(), s.get("set_by") or "import:0.4"))
                    written.append(s["name"])
        finally:
            db.close()
        return written

    def setting(self, name: str) -> str:
        """A hub plugin's secret setting (its value is a vault name)."""
        from rook.kit.ctx import KitError

        try:
            return self.get(str(name))
        except KeyError:
            raise KitError(f"secret {name} is not in the vault (secret.set it)") from None
        except Locked as e:
            raise KitError(str(e)) from None


def _check_name(name: Any) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise VaultError("a secret's name is 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
    if RESERVED.fullmatch(name):
        raise VaultError("band-psk-* names belong to band memberships")


def substitute(value: Any, lookup: Callable[[str], str]) -> Any:
    """``value`` with every ``{{secret:NAME}}`` in its strings replaced by
    ``lookup(NAME)`` (a copy; dict keys are left alone)."""
    if isinstance(value, str):
        return REF.sub(lambda m: lookup(m[1]), value) if "{{" in value else value
    if isinstance(value, dict):
        return {k: substitute(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, lookup) for v in value]
    return value


def resolver(vault: Vault, is_owner: Callable[[str | None], bool]) -> Callable[..., Any]:
    """Dispatch's step for vault references, as ``resolve(args, caller)``:
    only an owner's calls (in person or through their agents) use the vault;
    an unknown name or a locked vault fails the call."""
    def resolve(args: Any, caller) -> Any:
        def lookup(name: str) -> str:
            if RESERVED.fullmatch(name):
                raise ValueError("band keys travel only in core.trust.set")
            if not is_owner(caller.person):
                raise ValueError("only an owner's calls use vault secrets")
            try:
                return vault.get(name)
            except KeyError:
                raise ValueError(f"{name} is not in the vault") from None
            except Locked as e:
                raise ValueError(str(e)) from None
        return substitute(args, lookup)
    return resolve


def _hidden(args: Any, arg_specs: Any) -> set:
    """The args a cap declares ``type: secret`` that hold a value, not a reference."""
    if not isinstance(args, dict) or not isinstance(arg_specs, dict):
        return set()
    return {k for k, spec in arg_specs.items()
            if isinstance(spec, dict) and (spec.get("type") == "secret" or spec.get("secret") is True)
            and k in args and not (isinstance(args[k], str) and LONE_REF.fullmatch(args[k]))}


def journal_args(args: Any, arg_specs: Any) -> Any:
    """``args`` as the journal keeps them (spec 11.1): an argument the cap
    declares ``type: secret`` that holds a value (not a reference) becomes
    ``[redacted]``."""
    hidden = _hidden(args, arg_specs)
    return {k: "[redacted]" if k in hidden else v for k, v in args.items()} if hidden else args


def secret_values(args: Any, arg_specs: Any) -> list[str]:
    """The values of those args, to redact from error text."""
    return [v if isinstance(v, str) else repr(v) for k in _hidden(args, arg_specs) if (v := args[k]) not in ("", None)]
