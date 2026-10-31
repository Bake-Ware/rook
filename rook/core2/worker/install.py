"""Plugin installs, worker side: receive a hub-built bundle, verify it,
install it next to the previous version, hand it to the host, run its
``check`` caps, and roll back by itself when anything fails
(castling-manifest-lifecycle; spec 13 for ``plugin.sig``).

    inst = Installer(state / "plugins", worker_id=wid, host=host,
                     keys=approver_keys, approves=approver)
    report = await inst.receive(bundle_path)     # {"ok", "plugin", "version", ...}
    await inst.rollback("deluge")                # back to the kept version
    folders, skipped = inst.installed()          # at start-up: what to load

Nothing is written before the signature checks out: an unsigned bundle, a
hub that may not approve plugins here, a different worker's bundle, an
unexpected or missing file and a hash mismatch are all refused with the
reason. Layout: ``<root>/<plugin>/<version>/`` holds the files and
``plugin.sig``; ``<root>/<plugin>/state.json`` names ``current`` and
``previous``. A version folder never changes once installed: the same
version with different files is refused (bump the version).

``keys(hub)`` returns the verified operational keys ({kid: pub}) of ``hub``
when it may approve plugins on this machine (role ``home`` or ``co-owner``
on some band), else nothing; ``approves(hub)`` says the same from the trust
store alone, for start-up, when op keys may not be known yet or have rotated.
TODO(castling:worker-security): both come from TrustStore/WorkerGate.
TODO(castling:wiring): core.yaml's plugin caps call receive/rollback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from rook.core2 import wire
from rook.kit.ctx import Caller
from rook.kit.manifest import file_hashes, normalize

log = logging.getLogger("rook.worker.install")

SIG_TYP = "rook-plugin"
SIG_FILE = "plugin.sig"
STATE_FILE = "state.json"
SIG_MAX = 4 << 20          # plugin.sig is a file list, not a payload
CHECK_TIMEOUT = 60.0       # seconds per check cap
PIP_TIMEOUT = 900          # seconds to install a plugin's python pins

_NAME = re.compile(r"[a-z][a-z0-9-]*")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                     r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?")
_SHA = re.compile(r"[0-9a-f]{64}")


class InstallError(Exception):
    """A refused or failed install; the message is the report's reason."""


class PluginHost(Protocol):
    """The part of the worker host (rook.core2.worker.host.Host) installs use."""

    async def load(self, folder: Path) -> Any: ...
    async def unload(self, name: str) -> None: ...
    async def call(self, cap: str, args: Mapping | None, caller: Caller) -> Any: ...


def pip_install(pins: list[str]) -> None:
    """Install pins into this worker's own environment (uv when present)."""
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable, *pins] if uv
           else [sys.executable, "-m", "pip", "install", "--no-input", *pins])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("\n".join((r.stderr or r.stdout).strip().splitlines()[-8:]))


def _safe_rel(name: str) -> bool:
    """A plain relative path: no root, drive, stream, backslash, ``.`` or ``..``."""
    return (bool(name) and not name.startswith("/") and "\\" not in name and ":" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def check_sig(sig: Any, keys: Mapping[str, str], worker_id: str) -> str | None:
    """Why ``sig`` is not a plugin.sig this worker may install, or None."""
    if not isinstance(sig, dict) or sig.get("typ") != SIG_TYP or sig.get("v") != 2:
        return "malformed"
    if not isinstance(sig.get("plugin"), str) or not _NAME.fullmatch(sig["plugin"]):
        return "malformed: plugin"
    if not isinstance(sig.get("version"), str) or not _SEMVER.fullmatch(sig["version"]):
        return "malformed: version"
    files, python = sig.get("files"), sig.get("python")
    if not isinstance(files, dict) or not files or not all(
            isinstance(k, str) and _safe_rel(k) and k != SIG_FILE and isinstance(v, str) and _SHA.fullmatch(v)
            for k, v in files.items()):
        return "malformed: files"
    if not isinstance(python, list) or not all(isinstance(p, str) and p and not p.startswith("-") for p in python):
        return "malformed: python"
    pub = keys.get(sig.get("kid")) if isinstance(sig.get("kid"), str) else None
    if not pub:
        return f"not signed by a hub that may approve plugins here (hub {sig.get('hub')})"
    if not wire.verify_sig(pub, sig):
        return "bad signature"
    if sig.get("worker") != worker_id:
        return f"signed for worker {sig.get('worker')}, not this one"
    return None


class Installer:
    def __init__(self, root, *, worker_id: str, host: PluginHost,
                 keys: Callable[[str], Mapping[str, str]], approves: Callable[[str], bool],
                 install_python: Callable[[list[str]], None] = pip_install,
                 check_timeout: float = CHECK_TIMEOUT):
        self.root = Path(root)
        self.worker_id = worker_id
        self.host = host
        self.keys = keys
        self.approves = approves
        self.install_python = install_python
        self.check_timeout = check_timeout
        self._lock = asyncio.Lock()

    # -- state ------------------------------------------------------------------------

    def state(self, plugin: str) -> dict:
        try:
            s = json.loads((self.root / plugin / STATE_FILE).read_text(encoding="utf-8"))
            return {"current": s.get("current"), "previous": s.get("previous")}
        except (OSError, ValueError, AttributeError):
            return {"current": None, "previous": None}

    def _save(self, plugin: str, state: dict) -> None:
        path = self.root / plugin / STATE_FILE
        tmp = path.with_name(STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _prune(self, plugin: str, keep: dict) -> None:
        for p in (self.root / plugin).iterdir():
            if p.is_dir() and p.name not in (keep["current"], keep["previous"]):
                shutil.rmtree(p, ignore_errors=True)

    def installed(self) -> tuple[dict[str, Path], dict[str, str]]:
        """Current version folders to load at start-up, re-checked: file
        hashes still match plugin.sig and its hub still approves plugins
        here. Returns (plugin -> folder, plugin -> why skipped)."""
        ok, skipped = {}, {}
        if not self.root.is_dir():
            return ok, skipped
        for pdir in sorted(p for p in self.root.iterdir() if p.is_dir() and _NAME.fullmatch(p.name)):
            current = self.state(pdir.name)["current"]
            if not current:
                continue
            try:
                ok[pdir.name] = self._recheck(pdir.name, current)
            except InstallError as e:
                skipped[pdir.name] = f"{pdir.name}@{current}: {e}"
        return ok, skipped

    def _recheck(self, plugin: str, version: Any) -> Path:
        """An installed version folder that may load again: its files still
        match its plugin.sig and the signing hub still approves plugins here."""
        if not (_NAME.fullmatch(plugin) and isinstance(version, str) and _SEMVER.fullmatch(version)):
            raise InstallError("not an installed plugin version")
        folder = self.root / plugin / version
        try:
            sig = json.loads((folder / SIG_FILE).read_text(encoding="utf-8"))
            if sig.get("worker") != self.worker_id or sig.get("plugin") != plugin:
                raise InstallError("plugin.sig is for another worker or plugin")
            if not self.approves(sig.get("hub")):
                raise InstallError(f"hub {sig.get('hub')} no longer approves plugins here")
            if file_hashes(folder) != sig.get("files"):
                raise InstallError("files changed since install")
        except (OSError, ValueError, AttributeError) as e:
            raise InstallError(str(e)) from None
        return folder

    # -- install ----------------------------------------------------------------------

    async def receive(self, bundle) -> dict:
        """Install the bundle at ``bundle`` (a path). Never raises for a bad
        bundle or a failed install: the report's ``reason`` says what happened."""
        async with self._lock:
            try:
                return await self._receive(Path(bundle))
            except InstallError as e:
                return {"ok": False, "reason": str(e)}
            except (tarfile.TarError, EOFError) as e:      # truncated or corrupt tar
                return {"ok": False, "reason": f"not a plugin bundle: {e}"}
            except (OSError, ValueError, yaml.YAMLError) as e:  # disk full, a file where a folder goes
                return {"ok": False, "reason": f"install failed: {e}"}

    def _unpack(self, bundle: Path) -> tuple[dict, Path]:
        """Verify and extract into a staging folder; returns (sig, staging)."""
        try:
            tar = tarfile.open(bundle, "r:")
        except (OSError, tarfile.TarError) as e:
            raise InstallError(f"not a plugin bundle: {e}") from None
        with tar:
            members: dict[str, tarfile.TarInfo] = {}
            folded: set[str] = set()     # case-insensitive disks would merge these
            for m in tar.getmembers():
                if not m.isreg():
                    raise InstallError(f"bundle holds a non-file entry: {m.name}")
                if not _safe_rel(m.name) or m.name.casefold() in folded:
                    raise InstallError(f"bundle holds a bad or repeated path: {m.name}")
                members[m.name] = m
                folded.add(m.name.casefold())
            if SIG_FILE not in members:
                raise InstallError("unsigned bundle: no plugin.sig")
            if members[SIG_FILE].size > SIG_MAX:
                raise InstallError("plugin.sig is too large")
            raw = tar.extractfile(members.pop(SIG_FILE)).read()
            try:
                sig = json.loads(raw)
            except ValueError:
                raise InstallError("plugin.sig is not JSON") from None
            hub = sig.get("hub") if isinstance(sig, dict) else None
            why = check_sig(sig, self.keys(hub) if isinstance(hub, str) else {}, self.worker_id)
            if why:
                raise InstallError(f"signature refused: {why}")
            extra = sorted(set(members) - set(sig["files"]))
            missing = sorted(set(sig["files"]) - set(members))
            if extra or missing:
                raise InstallError("tampered bundle: " + "; ".join(
                    [f"unsigned file {n}" for n in extra] + [f"missing file {n}" for n in missing]))
            if "plugin.yaml" not in members:
                raise InstallError("bundle has no plugin.yaml")
            staging = self.root / sig["plugin"] / f".incoming-{uuid.uuid4().hex}"
            staging.mkdir(parents=True)
            try:
                for rel, m in sorted(members.items()):
                    dest = staging / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    h = hashlib.sha256()
                    with tar.extractfile(m) as src, open(dest, "wb") as out:
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            h.update(chunk)
                            out.write(chunk)
                    if h.hexdigest() != sig["files"][rel]:
                        raise InstallError(f"tampered bundle: {rel} does not match its signed hash")
                (staging / SIG_FILE).write_bytes(raw)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return sig, staging

    async def _receive(self, bundle: Path) -> dict:
        sig, staging = await asyncio.to_thread(self._unpack, bundle)
        plugin, version = sig["plugin"], sig["version"]
        target = self.root / plugin / version
        try:
            data = yaml.safe_load((staging / "plugin.yaml").read_text(encoding="utf-8"))
            if not isinstance(data, dict) or (data.get("plugin"), str(data.get("version"))) != (plugin, version):
                raise InstallError(f"plugin.yaml is not {plugin}@{version}")
            state = self.state(plugin)
            fresh, damaged = True, False
            if target.is_dir() and version in (state["current"], state["previous"]):
                try:
                    old_files = json.loads((target / SIG_FILE).read_text(encoding="utf-8"))["files"]
                except (OSError, ValueError, KeyError, TypeError):
                    old_files = None
                if old_files != sig["files"]:
                    raise InstallError(f"{plugin}@{version} is already installed with different files; "
                                       "bump the version")
                fresh = False
                try:
                    damaged = file_hashes(target) != sig["files"]
                except ValueError:       # a symlink appeared
                    damaged = True
                if not damaged and version == state["current"]:
                    return {"ok": True, "plugin": plugin, "version": version,
                            "previous": state["previous"], "unchanged": True}
            if sig["python"]:
                try:
                    await asyncio.to_thread(self.install_python, sig["python"])
                except Exception as e:
                    raise InstallError(f"python requirements failed to install: {e}") from None
            if fresh or damaged:             # a leftover, or files changed on disk: replace
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return await self._activate(plugin, version, target, state, data, fresh)

    async def _activate(self, plugin: str, version: str, target: Path, state: dict,
                        data: dict, fresh: bool) -> dict:
        report = {"plugin": plugin, "version": version, "previous": state["current"]}
        try:
            await self.host.load(target)
        except Exception as e:           # the host keeps the running version
            if fresh:
                shutil.rmtree(target, ignore_errors=True)
            return {**report, "ok": False, "rolled_back": False, "reason": f"load failed: {e}"}
        failed = await self._checks(plugin, version, data)
        if failed:
            back = await self._restore(plugin, state["current"])
            if fresh:
                shutil.rmtree(target, ignore_errors=True)
            return {**report, "ok": False, "rolled_back": back is None,
                    "reason": failed + (f"; rollback failed: {back}" if back else "")}
        new = {"current": version,
               "previous": state["current"] if state["current"] != version else state["previous"]}
        self._save(plugin, new)
        self._prune(plugin, new)
        log.info("installed %s@%s (previous %s)", plugin, version, new["previous"])
        return {**report, "ok": True, "previous": new["previous"], "checks": self._check_caps(data)}

    @staticmethod
    def _check_caps(data: dict) -> list[str]:
        """The manifest's ``check`` caps that run here (hub caps are the hub's)."""
        try:
            caps = normalize(data)["caps"]
        except Exception:
            caps = {}
        return [c for c in data.get("check") or () if caps.get(c, {}).get("on", "worker") == "worker"]

    async def _checks(self, plugin: str, version: str, data: dict) -> str | None:
        caller = Caller(agent="system:install", why=f"post-install check of {plugin}@{version}")
        for cap in self._check_caps(data):
            try:
                await asyncio.wait_for(self.host.call(cap, {}, caller), self.check_timeout)
            except asyncio.TimeoutError:
                return f"check {cap} timed out after {self.check_timeout:g}s"
            except Exception as e:
                return f"check {cap} failed: {e}"
        return None

    async def _restore(self, plugin: str, version: str | None) -> str | None:
        """Put ``version`` back in the host (None = unload). Returns an error or None."""
        try:
            if version:
                await self.host.load(self.root / plugin / version)
            else:
                await self.host.unload(plugin)
        except Exception as e:
            log.error("%s: rollback to %s failed: %s", plugin, version, e)
            return str(e) or type(e).__name__
        return None

    # -- rollback ---------------------------------------------------------------------

    async def rollback(self, plugin: str) -> dict:
        """Swap back to the kept previous version (which becomes the kept one)."""
        async with self._lock:
            if not _NAME.fullmatch(plugin):
                return {"ok": False, "plugin": plugin, "reason": "not a plugin name"}
            state = self.state(plugin)
            prev = state["previous"]
            if not prev or not (self.root / plugin / prev).is_dir():
                return {"ok": False, "plugin": plugin, "reason": f"no previous version of {plugin} is kept"}
            try:
                self._recheck(plugin, prev)    # the kept folder is on disk: verify it as at start-up
            except InstallError as e:
                return {"ok": False, "plugin": plugin, "reason": f"previous version {prev} refused: {e}"}
            err = await self._restore(plugin, prev)
            if err:
                return {"ok": False, "plugin": plugin, "reason": f"rollback to {prev} failed: {err}"}
            self._save(plugin, {"current": prev, "previous": state["current"]})
            return {"ok": True, "plugin": plugin, "version": prev, "previous": state["current"]}
