"""Where the app and the worker plugin keep things, the same for both."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """~/.local/share/rookmote: profiles, settings, scores."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    d = Path(base) / "rookmote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """~/.cache/rookmote: what can be made again (the games' synthesized sound)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "rookmote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path() -> Path:
    """The plugin's socket, which the app connects to (the worker runs as a
    user service and the app in the user's session: both see XDG_RUNTIME_DIR)."""
    run = os.environ.get("ROOKMOTE_SOCKET")
    if run:
        return Path(run)
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(rt) if rt and Path(rt).is_dir() else data_dir()) / "rookmote.sock"
