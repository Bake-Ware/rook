"""One-time sign-in links: ``<data>/login-links.json``, {sha256(code): {person, exp}}.

The installer opens the browser on one so a new owner lands signed in, with
no password file or token to copy (castling-first-run; Bake, Nov 4: "no
terminal commands, tokens or password files for the person"). ``make``
(the hub CLI ``login-link``) writes a code good for ``SECS``; the site's
``GET /login/link?code=`` takes it once, starts a session and forgets it.
Only the code's hash is kept. The file is read on every use, so the CLI
works while the hub runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

FILE = "login-links.json"
SECS = 15 * 60
_lock = threading.Lock()


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _read(path: Path) -> dict:
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _write(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(entries))
    os.replace(tmp, path)


def make(data, person: str, secs: float = SECS) -> str:
    """A new one-time code that signs ``person`` (``person:<name>``) in."""
    code = secrets.token_urlsafe(24)
    path, now = Path(data) / FILE, time.time()
    with _lock:
        entries = {d: e for d, e in _read(path).items() if isinstance(e, dict) and e.get("exp", 0) > now}
        entries[_digest(code)] = {"person": person, "exp": now + secs}
        _write(path, entries)
    return code


def take(data, code) -> str | None:
    """The person a code signs in, once; None for an unknown, used or old one."""
    if not isinstance(code, str) or not code or len(code) > 200:
        return None
    path, d, now = Path(data) / FILE, _digest(code), time.time()
    with _lock:
        entries = _read(path)
        e = entries.pop(d, None)
        if e is None:
            return None
        _write(path, {k: v for k, v in entries.items() if isinstance(v, dict) and v.get("exp", 0) > now})
    if not isinstance(e, dict) or e.get("exp", 0) <= now or not isinstance(e.get("person"), str):
        return None
    return e["person"]
