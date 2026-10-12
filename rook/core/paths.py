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

Stdlib only: it ships inside the worker bundle and the Android app as part of
``rook.core``. ``rook.paths`` re-exports these for hub-side code.
"""

from __future__ import annotations

import os
from pathlib import Path

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
