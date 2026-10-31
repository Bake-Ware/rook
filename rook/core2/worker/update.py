"""Core updates, worker side (docs/castling-updates.md): receive a hub-signed
release, verify it like a plugin bundle, smoke-check it, and make it the
trial for the next start; confirm it once the new process serves a call from
its home hub, or drop it and go back to the core it replaced.

    up = Updater(data / "core", keys=approver_keys, running=boot.running())
    report = await up.receive(tarball)   # verified, unpacked, smoke-checked: restart next
    up.confirm()                         # the trial process served its home hub
    up.abandon("no call served")         # the trial didn't make it: restart next
    up.rollback()                        # back to the kept previous core: restart next

Releases live in ``<data>/core/<version>/`` beside :mod:`.boot`'s
``state.json``; only ``current``, ``previous`` and the trial are kept.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from rook.core2 import release
from rook.core2.worker import boot
from rook.core2.worker.install import pip_install

log = logging.getLogger("rook.worker.update")

SMOKE_TIMEOUT = 120.0        # seconds for the new core to import and check itself
TRIAL_SECS = 120.0           # a trial start has this long to serve a call from its home hub


class UpdateError(Exception):
    """A refused or failed update; the message says why."""


def unmet(requires: list[str]) -> list[str]:
    """The requirements this environment doesn't satisfy (all of them when
    ``packaging`` isn't there to tell)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        from packaging.requirements import Requirement
    except ImportError:
        return list(requires)
    out = []
    for line in requires:
        try:
            req = Requirement(line)
        except Exception:
            out.append(line)
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            have = version(req.name)
        except PackageNotFoundError:
            out.append(line)
            continue
        if req.url:              # a direct reference (telesthete @ git+...): present is all we can tell
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            out.append(line)
    return out


def smoke(folder: Path, config: Path | None) -> None:
    """Run the release's own ``check`` in a child: it imports the new core
    from ``folder``, reads the config and validates core's manifests."""
    env = dict(os.environ)
    for k in (boot.HOP, boot.SAVED_PATH):
        env.pop(k, None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(folder), env.get("PYTHONPATH", "")) if p)
    argv = [sys.executable, "-P", "-m", "rook.core2.worker"] + (["--config", str(config)] if config else []) + ["check"]
    try:
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=SMOKE_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"smoke check took over {SMOKE_TIMEOUT:g}s") from None
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-6:])
        raise UpdateError(f"smoke check failed: {tail or f'exit {r.returncode}'}")
    try:
        got = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise UpdateError("smoke check printed no result") from None
    if Path(str(got.get("folder"))).resolve() != Path(folder).resolve():
        raise UpdateError(f"smoke check ran {got.get('folder') or 'the installed core'}, not the release")


class Updater:
    def __init__(self, root: Path, *, keys: Callable[[str], Mapping[str, str]], running: str | None,
                 config: Path | None = None,
                 install_python: Callable[[list[str]], None] = pip_install,
                 check: Callable[[Path, Path | None], None] = smoke):
        self.root = Path(root)
        self.keys = keys
        self.running = running
        self.config = config
        self.install_python = install_python
        self.check = check
        self._lock = asyncio.Lock()

    def state(self) -> dict:
        return boot.read_state(self.root)

    def _save(self, state: dict) -> None:
        boot.write_state(self.root, state)

    def status(self) -> dict:
        s = self.state()
        kept = sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if self.root.is_dir() else []
        return {"running": self.running, **s, "kept": kept}

    def in_trial(self) -> bool:
        t = self.state()["trial"]
        return isinstance(t, dict) and self.running is not None and t.get("version") == self.running

    def _prune(self, state: dict) -> None:
        keep = {state["current"], state["previous"], (state["trial"] or {}).get("version"), self.running}
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)

    # -- install --------------------------------------------------------------------

    async def receive(self, tarball: Path) -> dict:
        """Verify, unpack, install requirements and smoke-check a release,
        then make it the trial. The caller restarts the worker after replying."""
        async with self._lock:
            state = self.state()
            if state["trial"]:
                raise UpdateError(f"an update to {state['trial'].get('version')} is already waiting for its restart")
            try:
                sig = await asyncio.to_thread(release.read_sig, tarball)
            except release.ReleaseError as e:
                raise UpdateError(str(e)) from None
            hub = sig.get("hub") if isinstance(sig, dict) else None
            why = release.check_sig(sig, self.keys(hub) if isinstance(hub, str) else {})
            if why:
                raise UpdateError(f"signature refused: {why}")
            version = sig["version"]
            if version == self.running:
                raise UpdateError(f"already running {version}")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder = self.root / version
            if folder.is_dir():
                try:
                    old = json.loads((folder / release.SIG_FILE).read_text(encoding="utf-8"))["files"]
                except (OSError, ValueError, KeyError, TypeError):
                    old = None
                if old != sig["files"]:
                    raise UpdateError(f"{version} is already here with different files; bump the version")
                if boot.intact(folder) is not None:
                    shutil.rmtree(folder)
            if not folder.is_dir():
                staging = self.root / f".incoming-{uuid.uuid4().hex}"
                try:
                    await asyncio.to_thread(release.extract, tarball, sig, staging)
                    staging.replace(folder)
                except release.ReleaseError as e:
                    raise UpdateError(str(e)) from None
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
            missing = unmet(sig["requires"])
            if missing:
                try:
                    await asyncio.to_thread(self.install_python, missing)
                except Exception as e:
                    raise UpdateError(f"requirements failed to install: {e}") from None
            try:
                await asyncio.to_thread(self.check, folder, self.config)
            except UpdateError:
                if version not in (state["current"], state["previous"]):
                    shutil.rmtree(folder, ignore_errors=True)
                raise
            state["trial"] = {"version": version, "boots": 0, "from": self.running, "at": round(time.time(), 3)}
            state["failed"] = None            # this attempt's outcome is the one to report
            self._save(state)
            self._prune(state)
            log.info("core update ready", extra={"fields": {"version": version, "from": self.running or "base"}})
            return {"version": version, "from": self.running, "hub": hub, "restarting": True}

    # -- after the restart ----------------------------------------------------------

    def confirm(self) -> bool:
        """The trial process served a call from its home hub: it becomes current."""
        state = self.state()
        trial = state["trial"]
        if not (isinstance(trial, dict) and trial.get("version") == self.running and self.running):
            return False
        if state["current"] != self.running:
            state["previous"] = state["current"]
        state["current"], state["trial"] = self.running, None
        if (state["failed"] or {}).get("version") == self.running:
            state["failed"] = None
        self._save(state)
        self._prune(state)
        log.info("core update confirmed", extra={"fields": {"version": self.running,
                                                             "previous": state["previous"] or "base"}})
        return True

    def abandon(self, reason: str) -> None:
        """Drop the trial (the caller restarts into ``current``)."""
        state = self.state()
        trial = state["trial"]
        if not isinstance(trial, dict):
            return
        state["trial"] = None
        state["failed"] = {"version": trial.get("version"), "reason": reason, "at": round(time.time(), 3)}
        self._save(state)
        log.error("core update rolled back", extra={"fields": {"version": trial.get("version"), "reason": reason}})

    def rollback(self) -> dict:
        """Swap current and previous (None is the installed core). The caller restarts."""
        state = self.state()
        if state["trial"]:
            raise UpdateError("an update is waiting for its restart; restart or let it finish first")
        cur, prev = state["current"], state["previous"]
        if cur is None and prev is None:
            raise UpdateError("no other core is kept to go back to")
        if prev is not None and boot.intact(self.root / prev) is not None:
            raise UpdateError(f"the kept core {prev} can't run: {boot.intact(self.root / prev)}")
        state["current"], state["previous"] = prev, cur
        self._save(state)
        log.warning("core rolled back", extra={"fields": {"to": prev or "base", "from": cur or "base"}})
        return {"version": prev, "from": cur, "restarting": True}

    def refuse_running(self, reason: str) -> None:
        """The running release may not run any more (its hub no longer
        approves code here): back to the installed core at the next start."""
        state = self.state()
        if (state["trial"] or {}).get("version") == self.running:
            self.abandon(reason)
            return
        state["failed"] = {"version": self.running, "reason": reason, "at": round(time.time(), 3)}
        if state["current"] == self.running:
            state["current"] = None
        if state["previous"] == self.running:
            state["previous"] = None
        self._save(state)
        log.error("core release refused", extra={"fields": {"version": self.running, "reason": reason}})
