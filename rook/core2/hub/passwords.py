"""People's sign-in passwords: ``<data>/passwords.json``, {name: {hash, set_by, set}}.

A person signs in to the hub site (and to a claude.ai connector, via the MCP
door's ``/authorize``) with their user name and password, like 0.4's
dashboard (castling-switchover-decisions, Move-in: "a normal username and
password login, like 0.4 is"). Only a scrypt hash is kept. ``set_by`` says who
set it: ``generated`` (made by ``init`` without a terminal, left in
``<data>/owner.password`` for the deploy to pick up; the site offers to
change it at the first sign-in), ``init`` (typed at init), ``passwd`` (the hub
CLI: also the way back in if a password is lost), ``web`` (the person, on
their Account page) or ``migrate`` (0.4's dashboard password, carried over).

The file is read on every check, so ``passwd`` works while the hub runs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

FILE = "passwords.json"
GENERATED = "generated"
N, R, P = 2 ** 14, 8, 1
_DUMMY = None       # a hash to check against for unknown names, so they take as long


def _path(data) -> Path:
    return Path(data) / FILE


def _read(data) -> dict:
    try:
        out = json.loads(_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=32)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def make(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_hash(password, salt))}"


def _matches(stored: str, password: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                             p=int(p), dklen=len(base64.b64decode(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(want))


def set_password(data, name: str, password: str, by: str) -> None:
    """Set ``name``'s password (written 0600, atomically)."""
    if not isinstance(password, str) or not password:
        raise ValueError("the password is empty")
    entries = _read(data)
    entries[name] = {"hash": make(password), "set_by": by, "set": int(time.time())}
    path = _path(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(entries, indent=1))
    os.replace(tmp, path)


def set_by(data, name: str) -> str | None:
    """Who set ``name``'s password (init, passwd, migrate), or None: no password."""
    e = _read(data).get(name)
    return e.get("set_by") or "?" if isinstance(e, dict) and e.get("hash") else None


def check(data, name, password) -> bool:
    """True when ``password`` is ``name``'s. Slow on purpose (scrypt), for any name."""
    global _DUMMY
    e = _read(data).get(name) if isinstance(name, str) else None
    if not (isinstance(password, str) and isinstance(e, dict) and isinstance(e.get("hash"), str)):
        _DUMMY = _DUMMY or make(secrets.token_urlsafe(8))
        _matches(_DUMMY, password if isinstance(password, str) else "")
        return False
    return _matches(e["hash"], password)


def sign_in(hub, user, password) -> str | None:
    """The person (``person:<name>``) a user name and password sign in as, or
    None. Thread-safe: a door's request thread calls it, not the hub's loop."""
    name = user.strip().lower() if isinstance(user, str) else ""
    if not name or hub.identities.user(f"person:{name}") is None:
        check(hub.data, None, password)          # as slow as a known name
        return None
    return f"person:{name}" if check(hub.data, name, password) else None
