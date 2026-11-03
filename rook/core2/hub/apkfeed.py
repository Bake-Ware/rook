"""The phone app's update feed: ``GET /apk.json`` and ``GET /apk`` on the
hub's http door, with no token, giving the answers 0.4's site gave. The app
checks ``<its server>/apk.json`` (``?channel=beta`` on the beta channel) and
downloads ``<its server>/apk``, so phones keep updating once
rook.bakeforge.com points at castling. This lasts until 0.5 has its own path
(docs/castling-phone.md, "APK updates").

The hub only serves files an owner put in its data folder. It never builds
or fetches them:

    <data>/apk/rook-worker.apk       + rook-worker-apk.json    the stable app
    <data>/apk-beta/rook-worker.apk  + rook-worker-apk.json    the beta channel

A manifest is ``{package, version_code, version_name, sha256, size, url?}``
(android/build_apk_manifest.py). A channel is served only when its manifest
names this app, its size matches the file and the file's sha256 matches the
manifest. Each version of a file is hashed once, whether it matched or not. As on 0.4,
a phone on the beta channel gets the beta only while it is newer than the
stable app. Otherwise it gets exactly the stable answer, so it rejoins
stable once stable passes the beta. An empty folder answers 404, so a phone
says "no update" and keeps its app.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("rook.hub.apkfeed")

PACKAGE = "systems.bake.rook"
APK, MANIFEST = "rook-worker.apk", "rook-worker-apk.json"
STABLE, BETA = "apk", "apk-beta"
MAX_MANIFEST = 16384             # the app refuses a bigger one
SHA = re.compile(r"[0-9a-f]{64}")


class ApkFeed:
    def __init__(self, data: Path | str):
        self.root = Path(data)
        self._lock = threading.Lock()
        self._hashed: dict[str, tuple] = {}      # folder -> (file version, its hash matched): each version hashed once

    def _channel(self, folder: str) -> tuple[Path, dict] | None:
        apk, man = self.root / folder / APK, self.root / folder / MANIFEST
        try:
            if man.stat().st_size > MAX_MANIFEST:
                return None
            m = json.loads(man.read_text(encoding="utf-8"))
            st = apk.stat()
        except (OSError, ValueError):
            return None
        if not (isinstance(m, dict) and m.get("package") == PACKAGE
                and type(m.get("version_code")) is int and m["version_code"] > 0
                and isinstance(m.get("version_name"), str)
                and isinstance(m.get("sha256"), str) and SHA.fullmatch(m["sha256"])
                and type(m.get("size")) is int and m["size"] == st.st_size):
            return None
        seen = (st.st_ino, st.st_size, st.st_mtime_ns, m["sha256"])
        with self._lock:
            verdict = self._hashed.get(folder)
        if verdict is None or verdict[0] != seen:
            try:
                with apk.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
            except OSError:
                return None
            verdict = (seen, hmac.compare_digest(digest, m["sha256"]))
            if not verdict[1]:
                log.warning("apk does not match its manifest", extra={"fields": {"folder": folder}})
            with self._lock:
                self._hashed[folder] = verdict
        return (apk, m) if verdict[1] else None

    def pick(self, channel: str | None = None) -> tuple[Path, dict] | None:
        """(apk file, manifest) a phone on ``channel`` gets, or None."""
        stable = self._channel(STABLE)
        if channel == "beta":
            beta = self._channel(BETA)
            if beta is not None and beta[1]["version_code"] > (stable[1]["version_code"] if stable else 0):
                return beta
        return stable
