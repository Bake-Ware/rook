"""Files a plugin hands out once: ``GET /once/<plugin>/<token>`` on the hub's
http door, with no token of its own.

A hub plugin sometimes has to give bytes to a script on another worker (the
Rook remote's voice plugin gives an utterance to a worker that can reach the
voice server). As an argument of a call they would be kept in the journal,
and a big message over the band may not arrive. So the plugin puts the bytes
in its own data folder under a name nobody can guess, and passes only the
address:

    <data>/plugins/<plugin>/once/<token>      token: 32 to 64 hex digits

The first GET gets the file and removes it; a second gets 404. A file
nobody fetched is removed after ``MAX_AGE`` seconds, the next time anything
is asked for. The hub writes nothing here itself.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

FOLDER = "once"
PLUGIN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
TOKEN = re.compile(r"[0-9a-f]{32,64}")
TAKEN = ".taken"
MAX_AGE = 600.0


class OnceFiles:
    def __init__(self, data: Path | str):
        self.root = Path(data) / "plugins"

    def _sweep(self, folder: Path) -> None:
        now = time.time()
        try:
            for p in folder.iterdir():
                if not p.is_symlink() and p.is_file() and now - p.stat().st_mtime > MAX_AGE:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    def take(self, plugin: str, token: str) -> Path | None:
        """Claim the file: a path to send and then remove (``done``), or None.
        The claim is a rename, so of two askers at once only one gets it."""
        if not PLUGIN.fullmatch(plugin or "") or not TOKEN.fullmatch(token or ""):
            return None
        folder = self.root / plugin / FOLDER
        self._sweep(folder)
        path = folder / token
        if path.is_symlink() or not path.is_file():
            return None
        taken = folder / (token + TAKEN)
        try:
            os.rename(path, taken)
        except OSError:
            return None
        return taken

    @staticmethod
    def done(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
