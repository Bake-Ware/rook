"""``updates.*``: hub-owned updates for the hub, workers, apps and firmware.

The hub checks GitHub releases (Rook and the telesthete relay), verifies
what it downloads against each release's SHA256SUMS, and keeps it cached.
Manage > Updates (rook/web/updates.js, API in :mod:`.web`) shows what every
member of the band runs against what is available and applies it:

* **hub**: through ``rook hub deploy`` (dead-man rollback) or by re-running
  the release's installer; a ``rook hub`` install the hub cannot switch
  itself gets the signed release prepared and the command to run; anything
  else is explained (:mod:`.hubself`);
* **workers**: a staged rollout of a build kept in the worker feed, canaries
  first, stopping at the first failed health gate (:mod:`.rollout`,
  rook/hub/update_feed.py);
* **Android app**: publishes a release APK through the hub's ``/apk`` feed
  and asks the apps to check it (they install with Android's consent);
* **firmware**: shown, flashed by hand (no firmware image ships in releases).

A release that raises a compat floor is *breaking*: the wizard names the
devices it would cut off and needs them updated or an explicit
acknowledgement first. Mode ``off`` / ``notify`` (default) / ``auto``; auto
works only inside the maintenance window, installs only a stable patch
release whose build provenance verifies, and never a breaking release (or
one crossing a breaking or unknown release). docs/operations/updates.md is
the user guide.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ....core.context import caller_identity
from ....core.plugin import Plugin, capability, place, setting
from ....core.pluginkit import CapError

log = logging.getLogger("rook.hub.plugins.updates")

TICK_SECS = 60.0


def _bundle_paths() -> tuple[Path, Path] | None:
    from ....remote.worker_bundle import MANIFEST, PYZ, WorkerBundle
    b = WorkerBundle(lambda: "")
    m, p = b.path(MANIFEST), b.path(PYZ)
    return (m, p) if m and p else None


class Updates(Plugin):
    NAMESPACE = "updates"
    NAME = "updates"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("mode", str, default="notify", choices=("off", "notify", "auto"),
                env="ROOK_UPDATES_MODE", group="Updates", label="Updates",
                help="off: never check. notify: check and tell you (dashboard, chat). auto: also "
                     "install updates inside the maintenance window (hub: verified patch "
                     "releases only); breaking updates always wait for you."),
        setting("channel", str, default="stable", choices=("stable", "prerelease"),
                group="Updates", label="Release channel",
                help="prerelease also offers test releases (release candidates)."),
        setting("window", str, default="02:00-05:00", group="Updates",
                pattern=r"^$|^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", label="Maintenance window",
                help="Hub local time, like 02:00-05:00. Auto mode only installs inside it. "
                     "Empty: any time."),
        setting("canary", str, default="", group="Rollout", label="Canary workers",
                help="Comma-separated worker names updated first in every rollout. Empty: the "
                     "first selected worker that is online."),
        setting("batch", int, default=2, min=1, max=20, group="Rollout",
                label="Workers at a time", help="After the canaries, how many workers update "
                                                "together."),
        setting("gate_secs", int, default=300, min=60, max=3600, group="Rollout", advanced=True,
                label="Health check window (s)",
                help="How long a worker has to come back on the new build and answer the hub "
                     "before the rollout stops."),
        setting("settle_secs", int, default=30, min=0, max=600, group="Rollout", advanced=True,
                label="Settle time (s)", help="How long a worker must stay on the new build."),
        setting("notify_chat", bool, default=True, group="Updates",
                label="Announce new releases in chat",
                help="Post to Telegram/Discord (notify.send) when a new release appears."),
        setting("check_hours", int, default=6, min=1, max=168, group="Source", advanced=True,
                label="Check every (hours)"),
        setting("repo", str, default="Bake-Ware/rook", group="Source", advanced=True,
                pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", label="Rook repository"),
        setting("relay_repo", str, default="Bake-Ware/telesthete", group="Source", advanced=True,
                pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", label="Relay repository"),
        setting("api_base", str, default="", env="ROOK_UPDATES_API", group="Source",
                advanced=True, label="GitHub API URL",
                help="Empty: https://api.github.com. For GitHub Enterprise or a mirror."),
        setting("token", str, secret=True, env="ROOK_UPDATES_GITHUB_TOKEN", group="Source",
                advanced=True, label="GitHub token",
                help="Optional; raises the API rate limit. Read-only access is enough."),
        setting("verify_attestations", bool, default=False, group="Source", advanced=True,
                label="Check build provenance",
                help="Also check build provenance (`gh attestation verify`, scoped to the "
                     "release workflow and tag) before a hub update you start; it is refused "
                     "if the check fails or the gh CLI is missing. Auto mode always checks it. "
                     "SHA256SUMS is always checked."),
        setting("auto_min_age_hours", int, default=24, min=1, max=720, group="Updates",
                advanced=True, label="Auto: wait after a release (hours)",
                help="Auto mode installs a hub release only once it has been a stable release "
                     "this long (and only a patch release of the version the hub runs)."),
    )
    SKILL = ("### updates\n"
             "`updates.status` on worker `rook`: hub version vs the newest release, workers "
             "behind, the running rollout. Applying updates (`updates.apply_hub`, "
             "`updates.prepare_hub`, `updates.rollout`) is for operators: ask the person who "
             "runs the hub first.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self.service = None
        self._task: asyncio.Task | None = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    # -- lifecycle -------------------------------------------------------------
    def _make_service(self):
        from ...update_feed import updates_dir
        from .service import UpdateService
        return UpdateService(
            updates_dir(), settings=lambda: self.settings.as_dict(reveal_secrets=True),
            client=lambda: getattr(self._node, "client", None),
            notify=self._notify, bundle_paths=_bundle_paths)

    async def start(self) -> None:
        self.service = self._make_service()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.service is not None:
            await self.service.stop()

    async def _loop(self) -> None:
        await asyncio.sleep(5.0)
        while True:
            try:
                await self.service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update service tick failed")
            await asyncio.sleep(TICK_SECS)

    async def _notify(self, text: str) -> dict:
        """notify.send (every chat integration); its reply, or why none ran."""
        node = self._node
        if node is None:
            return {"ok": False, "error": "no hub node"}
        for p in node.host.plugins:
            if getattr(p, "NAMESPACE", "") == "notify" and hasattr(p, "send"):
                return await p.send(text)
        return {"ok": False, "error": "the notify plugin is off"}

    # -- caps --------------------------------------------------------------------
    def _svc(self):
        if self.service is None:
            raise CapError("the update service is not running")
        return self.service

    @staticmethod
    def _admin(what: str) -> str:
        from ...authz import require_hub_admin
        refusal = require_hub_admin(what)
        if refusal:
            raise CapError(refusal)
        return str(caller_identity.get() or "system:rook-hub")

    @capability("status", risk="read", scope="multi-user")
    def status(self) -> dict:
        """Hub version against the newest release, how many workers are
        behind, the running rollout, and the mode/channel."""
        o = self._svc().overview()
        behind = [m["name"] for m in o["members"] if m.get("state") == "behind"]
        ro = o.get("rollout") or {}
        return {"mode": o["mode"], "channel": o["channel"],
                "hub": o["hub"]["current"], "latest": o["hub"]["latest"],
                "hub_update_available": o["hub"]["update_available"],
                "can_self_update": bool(o["hub"]["install"].get("can_apply")),
                "workers_behind": behind, "feed": o["feed"],
                "rollout": {k: ro.get(k) for k in ("id", "status", "build", "reason")} if ro else None,
                "check_error": o["check"]["error"] or None}

    @capability("history", risk="read", scope="multi-user")
    def history(self, n: int = 30) -> list:
        """Recent update events: hub updates, rollouts, rollbacks, APK publishes."""
        return self._svc().history(max(1, min(int(n), 200)))

    @capability("check", risk="admin", scope="multi-user")
    async def check(self) -> dict:
        """Check GitHub for new releases now."""
        self._admin("updates.check")
        return await self._svc().check(force=True)

    @capability("apply_hub", risk="admin", tags=("destructive",), scope="multi-user")
    async def apply_hub(self, tag: str) -> dict:
        """Update the hub to release ``tag`` (restarts it). Refused for a
        breaking release until the devices it cuts off are updated or the
        operator acknowledged it on Manage > Updates."""
        from .service import Refused
        who = self._admin("updates.apply_hub")
        try:
            return await self._svc().apply_hub(str(tag), who)
        except Refused as e:
            raise CapError(str(e)) from None

    @capability("prepare_hub", risk="admin", scope="multi-user")
    async def prepare_hub(self, tag: str) -> dict:
        """For a `rook hub` install the hub cannot switch itself: download
        and verify release ``tag``, leave a signed hub release in the updates
        directory and return the `rook hub deploy` command to run on the hub
        machine. Starts nothing. Same breaking-update acknowledgement as
        ``apply_hub``."""
        from .service import Refused
        who = self._admin("updates.prepare_hub")
        try:
            return await self._svc().prepare_hub(str(tag), who)
        except Refused as e:
            raise CapError(str(e)) from None

    @capability("rollout", risk="admin", tags=("destructive",), scope="multi-user")
    async def rollout(self, workers: list | None = None, build: int = 0) -> dict:
        """Staged worker rollout of ``build`` (default: the newest the hub has)
        to ``workers`` (ids or names; default all), canary first."""
        from .service import Refused
        who = self._admin("updates.rollout")
        svc = self._svc()
        sel: dict = {"mode": "all"}
        if workers:
            wanted = {str(w).lower() for w in workers}
            ids = [m["worker_id"] for m in svc.members()
                   if m["worker_id"] in wanted or (m.get("name") or "").lower() in wanted]
            sel = {"mode": "ids", "value": ids}
        try:
            return await svc.start_rollout(sel, who, build=int(build) or None)
        except Refused as e:
            raise CapError(str(e)) from None


PLUGIN = Updates
