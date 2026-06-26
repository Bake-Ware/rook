"""First-run setup store for the Rook hub dashboard.

Holds the operator-supplied band/hub settings that must NOT live in the
committed ``config.yaml``: the public hub address workers call back to, the
band PSK, the installer download domain, and a cosmetic band name.

Written by the web setup wizard (``CombinedServer`` ``/setup``) and read at
server start. Persisted to ``data/setup.json`` (gitignored) so secrets never
touch version control. Override the location with ``ROOK_SETUP_PATH``.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

# repo-root/data/setup.json  (rook/remote/setup_store.py -> parents[2] == repo root)
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "setup.json"

FIELDS = ("band_name", "band_psk", "hub_public", "pyz_domain")


def setup_path() -> Path:
    env = os.environ.get("ROOK_SETUP_PATH")
    return Path(env).expanduser() if env else _DEFAULT_PATH


def load() -> dict[str, str]:
    p = setup_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return {k: str(data.get(k, "")) for k in FIELDS}
    except Exception:
        return {}


def save(data: dict) -> dict[str, str]:
    p = setup_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: str(data.get(k, "")).strip() for k in FIELDS}
    p.write_text(json.dumps(clean, indent=2) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return clean


def is_configured() -> bool:
    """The band is usable once it has both a PSK and a public hub address."""
    d = load()
    return bool(d.get("band_psk") and d.get("hub_public"))


def gen_psk() -> str:
    """A fresh, strong band PSK suggestion for the wizard."""
    return "band-" + secrets.token_urlsafe(24)
