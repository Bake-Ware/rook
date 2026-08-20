"""Worker config overrides — remotely settable, commit-confirmed (design §1).

A worker's tunables (name, announce interval, log level) and — crucially —
plugin **env-gates** (``ROOK_WAKE_CMD``, ``ROOK_MEMORY_VAULT``, …) can be set
over the band without touching the box or re-running the installer. Overrides
live in ``~/.rook-band-worker/config.json``, loaded at boot; ``env`` entries are
pushed into ``os.environ`` *before* plugins load, so a config push can turn on a
gated capability (wake, memory vault) on any worker.

Changing settings you depend on to reach the site (nothing here does by default,
but ``env`` could) can strand a worker. So risky applies are **commit-confirmed**
(the network world calls it commit-confirmed / reload-confirmed): the new config
is written as *pending* with a deadline, the worker restarts under it, and it
must be confirmed by the site within the window — otherwise a watchdog reverts to
the previous config and restarts again. A worker that never came back to confirm
reverts itself.

State files (all under ~/.rook-band-worker/):
    config.json          the active overrides (merged into argv at boot)
    config.json.prev     snapshot to revert to while a pending apply is unconfirmed
    config.json.pending  {epoch, deadline, confirm_within} — present ⇒ awaiting confirm
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("rook.worker.wconfig")

_DIR = Path(os.path.expanduser("~")) / ".rook-band-worker"
_ACTIVE = _DIR / "config.json"
_PREV = _DIR / "config.json.prev"
_PENDING = _DIR / "config.json.pending"

# Only these keys are honored from a pushed config — an allowlist so a stray/
# malicious field can't set arbitrary worker state. ``env`` is a nested dict of
# environment overrides (plugin gates etc.).
_ALLOWED = ("name", "announce_interval", "log_level", "hub", "psk", "env", "epoch")


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _write(p: Path, data: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def load() -> dict:
    """The active overrides, filtered to the allowlist."""
    cfg = _read(_ACTIVE)
    return {k: cfg[k] for k in _ALLOWED if k in cfg}


def apply_env(cfg: dict | None = None) -> list[str]:
    """Push config ``env`` overrides into ``os.environ``. Call BEFORE loading
    plugins so gated caps (wake, memory) see them. Returns the keys set."""
    cfg = cfg if cfg is not None else load()
    env = cfg.get("env") or {}
    keys = []
    if isinstance(env, dict):
        for k, v in env.items():
            if v is None:
                os.environ.pop(str(k), None)
            else:
                os.environ[str(k)] = str(v)
            keys.append(str(k))
    return keys


def current() -> dict:
    """Active config + pending/confirm state, for worker.config_get."""
    return {"config": load(), "epoch": _read(_ACTIVE).get("epoch", 0),
            "pending": _read(_PENDING) or None}


def boot_reconcile() -> str | None:
    """At boot: if a pending config is already past its deadline (the worker
    crashed or never got confirmed), revert to prev before doing anything else.
    Returns a message if it reverted, else None."""
    pend = _read(_PENDING)
    if not pend:
        return None
    if time.time() >= pend.get("deadline", 0):
        prev = _read(_PREV)
        try:
            if prev:
                _write(_ACTIVE, prev)
            else:
                _ACTIVE.unlink(missing_ok=True)
            _PENDING.unlink(missing_ok=True)
            _PREV.unlink(missing_ok=True)
        except Exception:
            log.exception("boot reconcile revert failed")
        msg = (f"reverted unconfirmed config epoch {pend.get('epoch')} "
               f"(deadline passed) to previous")
        log.warning(msg)
        return msg
    return None


def stage_apply(settings: dict, epoch: int, confirm_within: float) -> dict:
    """Write a new config as *pending* (snapshotting the current as prev) and
    return the merged config. The caller restarts the worker afterward; a
    watchdog (see config plugin) enforces the confirm deadline."""
    cur = _read(_ACTIVE)
    merged = {k: cur.get(k) for k in _ALLOWED if k in cur}
    for k, v in settings.items():
        if k not in _ALLOWED:
            continue
        if k == "env" and isinstance(v, dict):
            env = dict(merged.get("env") or {})
            env.update({str(kk): vv for kk, vv in v.items()})
            merged["env"] = env
        else:
            merged[k] = v
    merged["epoch"] = int(epoch)
    # snapshot current as prev (only if there isn't already an unconfirmed prev)
    if not _PENDING.exists():
        _write(_PREV, cur)
    _write(_ACTIVE, merged)
    _write(_PENDING, {"epoch": int(epoch),
                      "deadline": time.time() + float(confirm_within),
                      "confirm_within": float(confirm_within)})
    return merged


def confirm(epoch: int) -> dict:
    """Commit the pending config (called by the site once it sees the worker
    back on the band under the new epoch)."""
    pend = _read(_PENDING)
    if not pend:
        return {"ok": True, "note": "nothing pending"}
    if int(pend.get("epoch", -1)) != int(epoch):
        return {"ok": False, "error": f"pending epoch is {pend.get('epoch')}, "
                                      f"not {epoch}"}
    try:
        _PENDING.unlink(missing_ok=True)
        _PREV.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "committed_epoch": int(epoch)}


def revert() -> dict:
    """Force a revert to prev (drops any pending). Returns the reverted-to
    config; the caller restarts."""
    prev = _read(_PREV)
    if prev:
        _write(_ACTIVE, prev)
    else:
        _ACTIVE.unlink(missing_ok=True)
    _PENDING.unlink(missing_ok=True)
    _PREV.unlink(missing_ok=True)
    return {"ok": True, "reverted_to_epoch": prev.get("epoch", 0)}


def pending() -> dict:
    return _read(_PENDING)
