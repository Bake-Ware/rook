"""sessions.shim.*: `claude` and `codex` started in your own terminal, live on
the Sessions page (docs/design/sessions.md §4 G).

Opt-in per host and off by default: nothing changes on a host until
``sessions.shim.install`` runs there. Install writes the shim
(:mod:`rook.worker.shim`) and hooks it into the shells; from then on an
interactive ``claude`` or ``codex`` typed in a terminal runs exactly as
before, and is also a Rook terminal the Sessions page can watch and type
into. ``sessions.shim.uninstall`` removes every trace;
``sessions.shim.status`` says what is installed where.

While the shim is installed this plugin listens on its owner-only local
socket. It shares the ``sessions`` namespace with the catalog and mirror
plugins (caps are registered by full name) and has no heartbeat, so it does
not collide with ``hb.sessions``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from ..plugin import Plugin, capability, place
from .. import shim

log = logging.getLogger("rook.worker.plugins.session_shim")


class SessionShimPlugin(Plugin):
    NAMESPACE = "sessions"
    NAME = "session_shim"
    PLACEMENT = place("not is_hub and has('pty')")
    SKILL = ("Session shim (opt-in per host, off by default): `sessions.shim.install` makes "
             "`claude`/`codex` typed in someone's own terminal run in a Rook terminal, so the "
             "Sessions page shows them live and can type into them; nothing else about them "
             "changes. `sessions.shim.status` says what is installed, `sessions.shim.uninstall` "
             "removes it. Linux and macOS. Ask before installing on a host.")

    def __init__(self) -> None:
        super().__init__()
        self._worker = None
        self._server: shim.LocalTermServer | None = None
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        if sys.platform == "win32" or os.name != "posix":
            return False
        try:
            import pty  # noqa: F401
            import termios  # noqa: F401
        except ImportError:
            return False
        return True

    def bind_worker(self, worker) -> None:
        self._worker = worker

    def _terminals(self):
        for p in getattr(self._worker, "plugins", None) or []:
            if hasattr(p, "attach_local"):
                return p
        return None

    async def start(self) -> None:
        if not shim.installed():
            return
        try:
            # A worker update ships a newer client: keep the copy current.
            if await asyncio.to_thread(shim.write_client):
                log.info("session shim client updated")
            await self._listen()
        except Exception:
            log.exception("session shim could not start")

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def _listen(self) -> None:
        if self._server is None:
            server = shim.LocalTermServer(self._terminals)
            await server.start()
            self._server = server

    # -- caps --------------------------------------------------------------------

    @capability("shim.install", risk="write")
    async def install(self, agents: list | None = None, shells: list | None = None) -> dict:
        """Install the session shim on this host (opt-in; off by default).
        ``agents``: claude and/or codex (default: those installed here).
        ``shells``: bash, zsh and/or fish (default: those configured here plus
        the login shell); each gets one marked block in its rc file (fish: a
        conf.d file) that uninstall removes. Takes effect in new terminals."""
        if self._terminals() is None:
            return {"ok": False, "error": "this worker has no terminals plugin (needs a PTY)"}
        async with self._lock:
            result = await asyncio.to_thread(shim.install, agents, shells)
            await self._listen()
        log.info("session shim installed for %s", ", ".join(result["agents"]))
        return result

    @capability("shim.uninstall", risk="write")
    async def uninstall(self) -> dict:
        """Remove the session shim: its rc blocks, its fish file and its
        folder. Sessions running through it keep running."""
        async with self._lock:
            await self.stop()
            result = await asyncio.to_thread(shim.uninstall)
        log.info("session shim uninstalled")
        return result

    @capability("shim.status", risk="read")
    async def status(self) -> dict:
        """Whether the session shim is installed here, for which agents and
        shells, where the real binaries are, and how many sessions run
        through it now."""
        out = await asyncio.to_thread(shim.status)
        term = self._terminals()
        out["listening"] = bool(self._server and self._server.listening)
        out["local_terminals"] = sum(1 for t in (term.terms.values() if term else ())
                                     if t.running and t.link is not None)
        return out


PLUGIN = SessionShimPlugin
