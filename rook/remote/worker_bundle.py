"""The band-worker bundle a hub serves at /band-worker.pyz and /band-worker.json.

An operator-built bundle (``python3 rook/remote/build_band_worker.py``, written
next to that script) always wins and is never touched: public hubs keep
choosing when a new build, and with it an OTA push, goes out.

Without one, the dashboard builds its own into the data directory
(``$ROOK_DATA_DIR/worker-bundle``, else ``~/.local/share/rook/worker-bundle``)
at start, and again whenever that copy is older than the installed worker
code or its manifest names another public URL. The build runs
build_band_worker.py in a subprocess on a background thread, so it is signed
and stamped with this hub's update key exactly like a manual build, and the
dashboard keeps serving while it runs (the bundle routes answer 503 until it
is ready).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
BUILDER = PKG_DIR / "build_band_worker.py"
PYZ = "band-worker.pyz"
MANIFEST = "band-worker.json"
BUILD_TIMEOUT = 600


def auto_dir() -> Path:
    from ..paths import data_path
    return Path(data_path("worker-bundle",
                          str(Path.home() / ".local" / "share" / "rook" / "worker-bundle")))


def _source_roots() -> list[Path]:
    """What goes into a bundle (build_band_worker.build), for staleness."""
    roots = [PKG_DIR.parent / "worker", PKG_DIR.parent / "core",
             PKG_DIR.parent / "cli" / "band_tui.py", BUILDER]
    try:
        import telesthete
        roots.append(Path(telesthete.__file__).resolve().parent / "protocol")
    except Exception:
        pass
    return roots


def newest_source_mtime(roots: Optional[list[Path]] = None) -> float:
    newest = 0.0
    for root in roots if roots is not None else _source_roots():
        if root.is_file():
            newest = max(newest, root.stat().st_mtime)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in filenames:
                if f.endswith(".py"):
                    try:
                        newest = max(newest, os.stat(os.path.join(dirpath, f)).st_mtime)
                    except OSError:
                        pass
    return newest


class WorkerBundle:
    """Find, and when needed build, the bundle the installers download."""

    def __init__(self, public_base: Callable[[], str], out_dir: Optional[Path] = None,
                 manual_dir: Path = PKG_DIR):
        self._public_base = public_base
        self.out_dir = Path(out_dir) if out_dir is not None else auto_dir()
        self.manual_dir = Path(manual_dir)
        self.error = ""
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def manual(self) -> bool:
        return (self.manual_dir / PYZ).is_file()

    @property
    def building(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def path(self, name: str) -> Optional[Path]:
        """The file to serve for ``name`` (band-worker.pyz / .json /
        band-worker-enrollment.pyz), or None when there is none yet.

        The enrollment URL is the same worker: the installer's flags
        (``--enroll``/``--enrolled``) make the difference. An operator may
        still drop a separate ``band-worker-enrollment.pyz`` beside the script."""
        if name == "band-worker-enrollment.pyz":
            special = self.manual_dir / name
            if special.is_file():
                return special
            name = PYZ
        # A rebuild swaps files in atomically, so the previous bundle keeps
        # being served while it runs.
        p = (self.manual_dir if self.manual else self.out_dir) / name
        return p if p.is_file() else None

    def stale_reason(self) -> str:
        """Why the auto-built bundle needs (re)building, or "" when it is
        current or an operator-built one is in charge."""
        if self.manual:
            return ""
        pyz, manifest = self.out_dir / PYZ, self.out_dir / MANIFEST
        if not pyz.is_file() or not manifest.is_file():
            return "no worker bundle yet"
        try:
            built = pyz.stat().st_mtime
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unreadable worker bundle"
        if newest_source_mtime() > built:
            return "worker code is newer than the bundle"
        want = self._manifest_base()
        if want and data.get("url") != f"{want}/{PYZ}":
            return "public URL changed"
        return ""

    def _manifest_base(self) -> str:
        return (os.environ.get("ROOK_PUBLIC_BASE", "").strip().rstrip("/")
                or self._public_base())

    def ensure(self) -> bool:
        """Start a background build when one is needed. True if one is running."""
        with self._lock:
            if self.building:
                return True
            reason = self.stale_reason()
            if not reason:
                return False
            self.error = ""
            self._thread = threading.Thread(target=self._run, args=(reason,),
                                            name="worker-bundle-build", daemon=True)
            self._thread.start()
            return True

    def _run(self, reason: str) -> None:
        started = time.monotonic()
        log.info("building the band-worker bundle into %s (%s); installers get "
                 "503 until it is ready", self.out_dir, reason)
        env = dict(os.environ)
        env["ROOK_PUBLIC_BASE"] = self._manifest_base()
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [sys.executable, str(BUILDER), "--output-dir", str(self.out_dir)],
                env=env, capture_output=True, text=True, timeout=BUILD_TIMEOUT,
                stdin=subprocess.DEVNULL)
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.strip():
                    log.info("worker bundle: %s", line)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
                raise RuntimeError(f"build_band_worker.py exited {proc.returncode}: {tail[0]}")
            log.info("band-worker bundle ready in %.0fs", time.monotonic() - started)
        except Exception as e:
            self.error = str(e)
            log.warning("could not build the band-worker bundle: %s. Build it by hand: "
                        "python3 %s", e, BUILDER)

    def wait(self, timeout: Optional[float] = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)
