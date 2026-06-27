"""worker.* — self-management: status, restart, reconfigure, update.

Lets the band migrate or upgrade a worker WITHOUT the curl|bash self-kill
problem. The trick is the same one the installer uses: never tear down the old
worker from inside the process being torn down. Here the worker hands its own
restart to the service manager (``systemctl --user restart`` / runit), which
owns the lifecycle independently; on hosts with no service manager it re-execs
in place. The restart is scheduled *after* the reply is sent, so the caller
gets an ack before the worker drops.

Capabilities:
    worker.status                         — pid / argv / pyz / supervisor
    worker.restart                        — clean restart, same config
    worker.reconfigure(hub?, psk?, name?) — repoint at a new band, then restart
    worker.update(url?, hub?, psk?, name?) — fetch a new bundle, then restart
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from ..plugin import Plugin, capability

log = logging.getLogger("rook.worker.plugins.selfupdate")

_HOME = Path(os.path.expanduser("~"))
_WORKER_DIR = _HOME / ".rook-band-worker"
_PYZ = _WORKER_DIR / "band-worker.pyz"
_UNIT = _HOME / ".config" / "systemd" / "user" / "rook-band-worker.service"
_SVC = "rook-band-worker"


def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or bool(os.environ.get("TERMUX_VERSION"))


def _supervisor() -> str:
    if _UNIT.exists():
        return "systemd-user"
    if _is_termux():
        return "runit"
    return "exec"


class SelfUpdatePlugin(Plugin):
    NAMESPACE = "worker"

    # -- introspection -------------------------------------------------------

    @capability("status")
    def _status(self) -> dict:
        return {
            "pid": os.getpid(),
            "python": sys.executable,
            "argv": sys.argv,
            "pyz": str(_PYZ),
            "pyz_exists": _PYZ.exists(),
            "supervisor": _supervisor(),
        }

    # -- lifecycle -----------------------------------------------------------

    @capability("restart")
    async def _restart(self) -> dict:
        """Restart this worker with its current config, via the service manager
        (or re-exec). Kill-safe."""
        self._schedule_restart(self._current_argv())
        return {"ok": True, "supervisor": _supervisor(), "restarting": True}

    @capability("reconfigure")
    async def _reconfigure(self, hub: str | None = None, psk: str | None = None,
                           name: str | None = None, restart: bool = True) -> dict:
        """Repoint this worker at a new band (hub/psk) and/or rename it, then
        restart. The new config is persisted to the systemd unit so it survives
        reboots. Used to migrate bands without a curl|bash reinstall.
        """
        if hub is None and psk is None and name is None:
            return {"ok": False, "error": "nothing to change (pass hub/psk/name)"}
        new_argv = self._current_argv(hub=hub, psk=psk, name=name)
        persisted = self._persist(new_argv)
        if restart:
            self._schedule_restart(new_argv)
        return {
            "ok": True,
            "supervisor": _supervisor(),
            "persisted_unit": persisted,
            "restarting": restart,
            # never echo the psk value back onto the band
            "changed": [k for k, v in (("hub", hub), ("psk", psk), ("name", name)) if v is not None],
        }

    @capability("update")
    async def _update(self, url: str | None = None, hub: str | None = None,
                      psk: str | None = None, name: str | None = None) -> dict:
        """Fetch a fresh worker bundle from ``url`` (if given), optionally
        repoint at a new band, then restart. Kill-safe."""
        notes = []
        if url:
            try:
                await self._download(url, _PYZ)
                notes.append(f"bundle updated from {url}")
            except Exception as e:
                return {"ok": False, "error": f"download failed: {type(e).__name__}: {e}"}
        new_argv = self._current_argv(hub=hub, psk=psk, name=name)
        persisted = self._persist(new_argv)
        self._schedule_restart(new_argv)
        return {"ok": True, "supervisor": _supervisor(), "notes": notes,
                "persisted_unit": persisted, "restarting": True}

    # -- internals -----------------------------------------------------------

    def _current_argv(self, hub: str | None = None, psk: str | None = None,
                      name: str | None = None) -> list[str]:
        """Reconstruct the full launch command (python + pyz + args), applying
        any hub/psk/name overrides. Preserves every other existing flag."""
        argv = [sys.executable, *sys.argv]  # sys.argv[0] is the pyz path

        def setflag(flag: str, val: str | None) -> None:
            if val is None:
                return
            if flag in argv:
                argv[argv.index(flag) + 1] = val
            else:
                argv.extend([flag, val])

        setflag("--hub", hub)
        setflag("--psk", psk)
        setflag("--name", name)
        return argv

    def _persist(self, argv: list[str]) -> bool:
        """Write the new launch command into the systemd --user unit so it
        survives restarts/reboots. No-op (returns False) when not systemd."""
        if not _UNIT.exists():
            return False
        try:
            text = _UNIT.read_text()
            execstart = "ExecStart=" + " ".join(argv)
            out, replaced = [], False
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    out.append(execstart)
                    replaced = True
                else:
                    out.append(line)
            if not replaced:
                return False
            _UNIT.write_text("\n".join(out) + "\n")
            return True
        except Exception:
            log.exception("failed to persist unit")
            return False

    async def _download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".pyz.new")

        def _fetch() -> None:
            import urllib.request
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            os.chmod(dest, 0o755)

        await asyncio.get_event_loop().run_in_executor(None, _fetch)

    def _schedule_restart(self, argv: list[str], delay: float = 1.0) -> None:
        """Fire the restart shortly *after* this call returns, so the reply is
        delivered to the caller before the worker is replaced."""
        loop = asyncio.get_event_loop()
        loop.call_later(delay, lambda: asyncio.ensure_future(self._do_restart(argv)))

    async def _do_restart(self, argv: list[str]) -> None:
        sup = _supervisor()
        try:
            if sup == "systemd-user":
                # systemd owns the swap: it stops this process (and the
                # systemctl client) and starts a fresh instance from the unit.
                # The job is queued before the kill, so it completes regardless.
                await self._run("systemctl", "--user", "daemon-reload")
                await self._run("systemctl", "--user", "restart", "--no-block", _SVC)
                return
            if sup == "runit":
                await self._run("sv", "restart", _SVC)
                return
            # No service manager: replace this process image in place.
            log.info("re-exec: %s", " ".join(argv))
            os.execv(argv[0], argv)
        except Exception:
            log.exception("restart failed; attempting in-place re-exec")
            try:
                os.execv(argv[0], argv)
            except Exception:
                log.exception("re-exec failed")

    async def _run(self, *cmd: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass


PLUGIN = SelfUpdatePlugin
