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
    # Preserve any keys we don't manage here (notably `bands`) so the setup
    # wizard doesn't clobber the dashboard's known-band list.
    existing: dict = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            existing = {}
    clean = {k: str(data.get(k, "")).strip() for k in FIELDS}
    existing.update(clean)
    p.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return clean


def load_bands() -> list[dict]:
    """Known bands for the dashboard selector: ``[{"name", "psk"}, ...]``.

    Reads the optional ``bands`` list from setup.json and always includes the
    primary configured band (``band_psk``) so existing single-band setups keep
    working. PSKs stay server-side; only band_id labels are ever sent to the UI.
    """
    p = setup_path()
    raw: list = []
    if p.exists():
        try:
            raw = json.loads(p.read_text()).get("bands", []) or []
        except Exception:
            raw = []
    out: list[dict] = []
    seen: set[str] = set()
    d = load()
    if d.get("band_psk"):  # primary band first
        out.append({"name": d.get("band_name") or "default", "psk": d["band_psk"]})
        seen.add(d["band_psk"])
    for b in raw:
        if isinstance(b, dict) and b.get("psk") and b["psk"] not in seen:
            seen.add(b["psk"])
            out.append({"name": str(b.get("name") or "band"), "psk": str(b["psk"])})
    return out


def save_bands(bands: list[dict]) -> None:
    """Persist the extra known-band list (excludes the primary band, which is
    derived from band_psk). Dedupes by PSK."""
    p = setup_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            existing = {}
    clean: list[dict] = []
    seen: set[str] = set()
    for b in bands or []:
        psk = str((b or {}).get("psk") or "").strip()
        if psk and psk not in seen:
            seen.add(psk)
            clean.append({"name": str((b or {}).get("name") or "band").strip(), "psk": psk})
    existing["bands"] = clean
    p.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def is_configured() -> bool:
    """The band is usable once it has both a PSK and a public hub address."""
    d = load()
    return bool(d.get("band_psk") and d.get("hub_public"))


def gen_psk() -> str:
    """A fresh, strong band PSK suggestion for the wizard."""
    return "band-" + secrets.token_urlsafe(24)
