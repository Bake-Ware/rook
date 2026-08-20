"""worker.config_* — remotely set worker overrides, commit-confirmed (design §1).

Push tunables and plugin env-gates to a worker over the band; it applies them,
restarts under the new config, and — for a risky apply — must be confirmed by
the site within a window or it auto-reverts. The heavy lifting (state files,
boot reconcile, env-gate application before plugin load) lives in
:mod:`rook.worker.wconfig`; this plugin is the band surface + the confirm
watchdog.

Turn on a gated cap remotely, e.g.::

    worker.config_apply(settings={"env": {"ROOK_WAKE_CMD": "claude -p {prompt_file}"}},
                        epoch=<n>, confirm_within=120)

The worker restarts, the wake plugin's ``available()`` now sees ROOK_WAKE_CMD and
announces ``agent.wake``. The site confirms once it sees the worker back.
"""

from __future__ import annotations

import asyncio
import logging

from ..plugin import Plugin, capability
from .. import wconfig

log = logging.getLogger("rook.worker.plugins.config")


class ConfigPlugin(Plugin):
    NAMESPACE = "worker"

    def __init__(self) -> None:
        super().__init__()
        self._worker = None
        self._watchdog: asyncio.Task | None = None

    def bind_worker(self, worker) -> None:
        # Grab a handle so we can trigger the existing worker.restart cap.
        self._worker = worker

    async def start(self) -> None:
        # If we booted into a pending (unconfirmed) config, arm the watchdog:
        # revert + restart if the site doesn't confirm before the deadline.
        pend = wconfig.pending()
        if pend:
            import time
            delay = max(1.0, pend.get("deadline", 0) - time.time())
            log.warning("booted with unconfirmed config epoch %s; will revert in "
                        "%.0fs unless confirmed", pend.get("epoch"), delay)
            self._watchdog = asyncio.create_task(self._revert_after(delay,
                                                                    pend.get("epoch")))

    async def stop(self) -> None:
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()

    async def _revert_after(self, delay: float, epoch) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Still pending at the deadline? The apply stranded us — revert.
        pend = wconfig.pending()
        if pend and int(pend.get("epoch", -1)) == int(epoch):
            log.warning("config epoch %s not confirmed in time — reverting", epoch)
            wconfig.revert()
            await self._restart()

    async def _restart(self) -> None:
        if self._worker is not None and self._worker.registry.has("worker.restart"):
            try:
                await self._worker.registry.call("worker.restart")
                return
            except Exception:
                log.exception("restart via worker.restart failed")
        # Fallback: re-exec in place.
        import os, sys
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception:
            log.exception("re-exec failed")

    @capability("config_get")
    def _get(self) -> dict:
        """Return this worker's active config overrides + pending/confirm state."""
        return {"ok": True, **wconfig.current()}

    @capability("config_apply")
    async def _apply(self, settings: dict, epoch: int,
                     confirm_within: float = 120.0, restart: bool = True) -> dict:
        """Apply config overrides and restart under them (commit-confirmed).

        ``settings`` may include ``name``, ``announce_interval``, ``log_level``,
        ``hub``, ``psk``, and ``env`` (a dict of environment overrides — this is
        how you remotely enable a gated cap like ``agent.wake`` via
        ``ROOK_WAKE_CMD`` or the memory vault via ``ROOK_MEMORY_VAULT``). The new
        config is staged as *pending*; the worker restarts and must be confirmed
        (worker.config_confirm) within ``confirm_within`` seconds or it reverts
        to the previous config automatically. Reply is sent before the restart."""
        if not isinstance(settings, dict) or not settings:
            return {"ok": False, "error": "settings must be a non-empty object"}
        merged = wconfig.stage_apply(settings, epoch, confirm_within)
        result = {"ok": True, "staged_epoch": int(epoch),
                  "confirm_within": confirm_within, "restarting": bool(restart),
                  "config": {k: v for k, v in merged.items() if k != "psk"}}
        if restart:
            # Defer the restart so this reply reaches the site first.
            async def _later():
                await asyncio.sleep(1.0)
                await self._restart()
            asyncio.ensure_future(_later())
        return result

    @capability("config_confirm")
    def _confirm(self, epoch: int) -> dict:
        """Confirm a pending config so it commits (the auto-revert watchdog is
        cancelled on the next boot since nothing stays pending). The site calls
        this once it sees the worker healthy on the band under the new epoch."""
        res = wconfig.confirm(epoch)
        if res.get("ok") and self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
        return res

    @capability("config_revert")
    async def _revert(self, restart: bool = True) -> dict:
        """Force a revert to the previous config and restart."""
        res = wconfig.revert()
        if restart:
            async def _later():
                await asyncio.sleep(1.0)
                await self._restart()
            asyncio.ensure_future(_later())
        return {**res, "restarting": bool(restart)}


PLUGIN = ConfigPlugin
