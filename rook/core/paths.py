"""Where a worker keeps its files.

Two places, which are the same directory unless ``ROOK_WORKER_HOME`` is set:

* the **state directory** (:func:`worker_state_dir`): identity, enrollment,
  pushed config, audit ring, chat/inbox, plugin data, the deauth flag. It is
  ``ROOK_WORKER_HOME`` when set, else ``~/.rook-band-worker``
  (``%USERPROFILE%\\.rook-band-worker`` on Windows), so a second worker on one
  machine, a test, or a container can keep its own state.
* the **install directory** (:func:`worker_install_dir`): where the installer
  puts ``band-worker.pyz`` and the venv, and where the service manager runs
  the bundle from. The installer always uses ``~/.rook-band-worker``, so OTA
  must swap the bundle there whatever ``ROOK_WORKER_HOME`` says.

Before this release only the session mirror honoured ``ROOK_WORKER_HOME``;
everything else stayed in ``~/.rook-band-worker``. A worker that had the
variable set (for the mirror) would come back from an update without its
identity, enrollment or deauth flag. :func:`migrate_legacy_state` copies
those over once, at worker start; :func:`legacy_state_path` lets the deauth
check read the old flag regardless.

Stdlib only: it ships inside the worker bundle and the Android app as part of
``rook.core``. ``rook.paths`` re-exports these for hub-side code.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("rook.core.paths")

#: The directory name under the home directory, before ROOK_WORKER_HOME existed.
WORKER_DIRNAME = ".rook-band-worker"


def worker_install_dir() -> Path:
    """Where the installer puts the worker bundle (always under the home dir)."""
    return Path.home() / WORKER_DIRNAME


def worker_state_dir() -> Path:
    """The worker's state directory: ``ROOK_WORKER_HOME``, else
    ``~/.rook-band-worker``. Read on every call, so a test or an embedding
    process can set the variable after import."""
    env = os.environ.get("ROOK_WORKER_HOME", "").strip()
    return Path(env).expanduser() if env else worker_install_dir()


def worker_state_path(*parts: str) -> Path:
    """A path inside :func:`worker_state_dir` (not created)."""
    return worker_state_dir().joinpath(*parts)


#: Worker state that lived in ~/.rook-band-worker whatever ROOK_WORKER_HOME
#: said, before it moved all state (files, then directories).
LEGACY_STATE_FILES = (
    "worker_id", "enrollment.json", "banned", "hold", "update_state.json",
    "config.json", "config.json.prev", "config.json.pending",
    "plugins.json", "custom_caps.json", "anchors.json",
    "audit.jsonl", "messages.jsonl", "wake-last.txt",
    "work.sqlite3", "work.sqlite3-wal", "work.sqlite3-shm",
)
LEGACY_STATE_DIRS = ("chat", "plugins", "terminals")
#: Written in the state directory once the copy has run (it runs only once).
MIGRATED_MARKER = ".migrated-from-legacy"
#: Start a separate worker with its own identity: copy nothing over.
FRESH_ENV = "ROOK_WORKER_HOME_FRESH"


def _moved() -> bool:
    """ROOK_WORKER_HOME is set and is not the legacy directory itself."""
    state, legacy = worker_state_dir(), worker_install_dir()
    if state == legacy:
        return False
    try:
        return state.resolve() != legacy.resolve()
    except OSError:
        return True


def legacy_state_path(name: str) -> Path | None:
    """``~/.rook-band-worker/<name>`` when ROOK_WORKER_HOME moved the state
    elsewhere, else ``None``. For checks that must see the old copy even if
    the migration did not run (the deauth flag)."""
    return worker_install_dir() / name if _moved() else None


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, copy_function=shutil.copy2)
        return
    tmp = dst.with_name(dst.name + ".migrating")
    shutil.copy2(src, tmp)  # keeps the mode (0600 config, enrollment)
    os.replace(tmp, dst)


def migrate_legacy_state() -> list[str]:
    """Copy worker state missing from ``ROOK_WORKER_HOME`` but present in
    ``~/.rook-band-worker``, once (a marker records it). The legacy copies
    stay, so a rollback to an older bundle still finds them. Returns the names
    copied. No-op when the variable is unset, when the marker exists, and
    with ``ROOK_WORKER_HOME_FRESH=1`` (a second worker on one machine, which
    must not take the first one's identity)."""
    if not _moved():
        return []
    state, legacy = worker_state_dir(), worker_install_dir()
    marker = state / MIGRATED_MARKER
    if marker.exists() or not legacy.is_dir():
        return []
    if os.environ.get(FRESH_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return []
    copied: list[str] = []
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("cannot create the worker state directory %s", state)
        return []
    for name in LEGACY_STATE_FILES + LEGACY_STATE_DIRS:
        src, dst = legacy / name, state / name
        if not src.exists() or dst.exists():
            continue
        try:
            _copy(src, dst)
            copied.append(name)
        except OSError:
            log.exception("copying %s to %s failed", src, dst)
    try:
        marker.write_text(json.dumps({"from": str(legacy), "at": time.time(),
                                      "copied": copied}), encoding="utf-8")
    except OSError:
        log.exception("cannot write %s; the copy will be retried", marker)
    if copied:
        log.warning("ROOK_WORKER_HOME=%s: copied %s from %s (state kept there before this "
                    "release). Set %s=1 for a separate worker with its own identity.",
                    state, ", ".join(copied), legacy, FRESH_ENV)
    return copied
