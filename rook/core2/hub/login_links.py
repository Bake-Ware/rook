"""Sign-in links: ``<data>/login-links.json``, {code: {person}}.

The installer opens the browser on one so a new owner lands signed in, with
no password file or token to copy (castling-first-run; Bake, Nov 4: "no
terminal commands, tokens or password files for the person"). ``make``
(the hub CLI ``login-link``) writes a code; the site's
``GET /login/link?code=`` starts a session with it. A link keeps working:
no time limit, any number of uses (Bake, Nov 6: "This security shit nobody
asked for. Why is there a secure only 15 min thing now?"). Codes are kept
as they are (Bake, Nov 6: "Remove all restrictions and gates"). The file is
read on every use, so the CLI works while the hub runs.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

FILE = "login-links.json"
_lock = threading.Lock()


def _read(path: Path) -> dict:
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _write(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entries), encoding="utf-8")
    os.replace(tmp, path)


def make(data, person: str) -> str:
    """A new code that signs ``person`` (``person:<name>``) in."""
    code = secrets.token_urlsafe(24)
    path = Path(data) / FILE
    with _lock:
        entries = _read(path)
        entries[code] = {"person": person}
        _write(path, entries)
    return code


def take(data, code) -> str | None:
    """The person a code signs in; None for an unknown one."""
    if not isinstance(code, str) or not code:
        return None
    with _lock:
        e = _read(Path(data) / FILE).get(code)
    if not isinstance(e, dict) or not isinstance(e.get("person"), str):
        return None
    return e["person"]
