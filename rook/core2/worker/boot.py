"""Which core a worker process runs: its installed ``rook`` package (the
base) or a release a hub installed under ``<data>/core/<version>/``
(docs/castling-updates.md). Called first thing by ``python -m
rook.core2.worker``; stdlib only.

``<data>/core/state.json``::

    {"current": "0.5.1" | null,      # null: the base
     "previous": "0.5.0" | null,     # kept for rollback
     "trial": {"version": "0.5.2", "boots": 1, "from": "0.5.1", "at": ...} | null,
     "failed": {"version": ..., "reason": ..., "at": ...} | null}

A trial is a release that hasn't proved itself yet. Every process start
that picks it counts a boot; after ``MAX_BOOTS`` starts without the worker
confirming it (hub contact, :mod:`.update`), the next start drops it and runs
``current`` again. That needs a supervisor that restarts the worker when it
exits (systemd ``Restart=always``, the desktop app): a worker run by hand that
crashes stays down until it is started again, then rolls back.

Switching is ``execv`` of the same command line with the release folder first
on ``PYTHONPATH``; the worker puts ``PYTHONPATH`` back as it was once it runs
(:func:`restore_env`), so programs it starts never see the release.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("rook.worker.boot")

STATE = "state.json"
SIG_FILE = "release.sig"
MAX_BOOTS = 3
HOP = "ROOK_CORE_HOP"                 # set across our own exec: the version (or "base") it went to
SAVED_PATH = "ROOK_CORE_PYTHONPATH"   # PYTHONPATH as it was before the exec ("" = unset)


def root_of(data: Path) -> Path:
    return Path(data) / "core"


def read_state(root: Path) -> dict:
    try:
        s = json.loads((Path(root) / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s = s if isinstance(s, dict) else {}
    return {k: s.get(k) for k in ("current", "previous", "trial", "failed")}


def write_state(root: Path, state: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = root / (STATE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, root / STATE)


def _ok_name(version) -> bool:
    return isinstance(version, str) and bool(version) and "/" not in version and "\\" not in version \
        and version not in (".", "..") and not version.startswith(".")


def intact(folder: Path) -> str | None:
    """Why the release in ``folder`` can't run (files missing or changed
    since it was installed), or None."""
    try:
        sig = json.loads((Path(folder) / SIG_FILE).read_text(encoding="utf-8"))
        files = sig["files"]
        found = {}
        for p in Path(folder).rglob("*"):
            rel = p.relative_to(folder).as_posix()
            if rel == SIG_FILE or "__pycache__" in p.parts or p.suffix == ".pyc" or p.is_dir():
                continue
            if p.is_symlink():
                return f"{rel} is a symlink"
            found[rel] = p
        if set(found) != set(files):
            return "files added or removed since install"
        for rel, p in found.items():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != files[rel]:
                return f"{rel} changed since install"
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return f"unreadable: {e}"
    return None


def running_folder() -> Path | None:
    """The release folder this process imported ``rook`` from, or None for the base."""
    import rook
    folder = Path(rook.__file__).resolve().parent.parent
    return folder if (folder / SIG_FILE).is_file() else None


@lru_cache(maxsize=1)
def running() -> str | None:
    """The release version this process runs, or None for the base (fixed
    for the life of a process)."""
    folder = running_folder()
    if folder is None:
        return None
    try:
        return str(json.loads((folder / SIG_FILE).read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return folder.name


def choose(root: Path, hop: str | None = None, now: float | None = None) -> str | None:
    """The version this start should run (None: the base), counting a trial's
    boots and dropping a trial that used them up. ``hop`` is the version our
    own exec just went to: that start was counted before the exec."""
    root = Path(root)
    state = read_state(root)
    trial = state["trial"]
    if isinstance(trial, dict) and _ok_name(trial.get("version")):
        v = trial["version"]
        if hop == v:
            return v
        why = intact(root / v)
        if why is None and int(trial.get("boots") or 0) >= MAX_BOOTS:
            why = f"did not come up: {MAX_BOOTS} starts without hub contact"
        if why is None:
            trial["boots"] = int(trial.get("boots") or 0) + 1
            write_state(root, state)
            return v
        state["trial"] = None
        state["failed"] = {"version": v, "reason": why, "at": round(now or time.time(), 3)}
        write_state(root, state)
        log.error("core release %s dropped: %s", v, why)
    elif trial is not None:
        state["trial"] = None
        write_state(root, state)
    for v in (state["current"], state["previous"]):
        if v is None:
            return None              # the base, known good
        if _ok_name(v) and intact(root / v) is None:
            return v
        log.error("core release %s can't run: %s", v, intact(root / v) if _ok_name(v) else "bad name")
    return None


def restore_env() -> str | None:
    """Undo our exec's environment changes (call once, at start): PYTHONPATH
    back as it was. Returns the hop marker, if this start came from our exec."""
    hop = os.environ.pop(HOP, None)
    saved = os.environ.pop(SAVED_PATH, None)
    if saved is not None:
        if saved:
            os.environ["PYTHONPATH"] = saved
        else:
            os.environ.pop("PYTHONPATH", None)
    return hop


def select(data: Path, execv=os.execv) -> str | None:
    """Pick the core for this start and exec into it if this process isn't
    already running it. Returns the version this process runs (None: base)."""
    hop = restore_env()
    root = root_of(data)
    want = choose(root, hop if hop != "base" else None)
    have = running_folder()
    want_folder = (root / want).resolve() if want else None
    if want_folder == have:
        return want
    if hop is not None:
        # We just exec'd for this and still landed elsewhere: don't loop.
        log.error("core release switch did not take (wanted %s, running %s)", want or "base", have or "base")
        return running()
    env = os.environ
    env[HOP] = want or "base"
    if want_folder is not None:
        env[SAVED_PATH] = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(want_folder), env.get("PYTHONPATH", "")) if p)
    sys.stdout.flush()
    sys.stderr.flush()
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    # -P: no current folder first on sys.path, where a rook/ checkout would shadow the release.
    safe = ["-P"] if want_folder is not None and not sys.flags.safe_path else []
    execv(sys.executable, [sys.executable, *safe, *argv[1:]])
    return want                      # only a test's execv returns
