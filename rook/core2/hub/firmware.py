"""Firmware for the band's small devices: ``GET /firmware.json`` and ``GET
/firmware/<name>`` on the hub's http door, with no token, so a device
anywhere can be told to update itself from its hub (the Rook remote's
``remote.update`` takes a URL and the build's SHA-256).

The hub only serves files an owner put in its data folder. It never builds
or fetches them:

    <data>/firmware/<name>.bin

A name is letters, digits, dots, dashes and underscores, and ends in .bin.
``/firmware.json`` lists what is there: ``{files: [{name, size, sha256,
modified}]}``, newest first. Each version of a file is hashed once. Nothing
here is secret: a build carries no keys (a device keeps its enrollment in its
own storage), and what makes a device trust a build is the hash it was given
over the band, not where the file came from.
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

FOLDER = "firmware"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.bin")
MAX_FILES = 200


class FirmwareFeed:
    def __init__(self, data: Path | str):
        self.root = Path(data) / FOLDER
        self._lock = threading.Lock()
        self._hashed: dict[str, tuple] = {}      # name -> (mtime_ns, size, sha256): each version hashed once

    def file(self, name: str) -> Path | None:
        """The file called ``name``, or None when there is none by a name this serves."""
        if not NAME.fullmatch(name or ""):
            return None
        path = self.root / name
        return path if path.is_file() and not path.is_symlink() else None

    def _sha(self, path: Path) -> tuple[int, int, str]:
        st = path.stat()
        with self._lock:
            got = self._hashed.get(path.name)
            if got and got[:2] == (st.st_mtime_ns, st.st_size):
                return st.st_size, st.st_mtime_ns, got[2]
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        with self._lock:
            self._hashed[path.name] = (st.st_mtime_ns, st.st_size, h.hexdigest())
        return st.st_size, st.st_mtime_ns, h.hexdigest()

    def listing(self) -> dict:
        files = []
        try:
            names = sorted(p.name for p in self.root.iterdir())[:MAX_FILES]
        except OSError:
            names = []
        for name in names:
            path = self.file(name)
            if path is None:
                continue
            try:
                size, mtime, sha = self._sha(path)
            except OSError:
                continue
            files.append({"name": name, "size": size, "sha256": sha, "modified": round(mtime / 1e9, 3)})
        files.sort(key=lambda f: -f["modified"])
        return {"files": files}
