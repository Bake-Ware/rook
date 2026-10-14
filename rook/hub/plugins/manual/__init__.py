"""``manual.*``: the Rook user manual, on worker ``rook``.

The manual ships with every release (docs/manual in the repository,
``rook/_manual`` in the wheel; see :mod:`.bundle`). This plugin serves it to
the dashboard's Help page and to agents, and keeps a copy in the knowledge
base (:mod:`.sync`):

* on a new hub, the first time the knowledge plugin is loaded and its store
  is empty, the manual is imported as the "Rook manual" pages (setting
  ``auto_seed``);
* when the hub runs a release whose manual differs from the imported copy,
  the dashboard asks the operator (setting ``prompt_updates``): Update, Not
  now (a day) or Never for this version, with the pages added, changed and
  removed; pages edited in the knowledge base are only replaced when the
  operator ticks them.

Caps: ``manual.status``, ``manual.list``, ``manual.get`` (read) and
``manual.update`` (admin: update, snooze, decline). The dashboard reaches the
same through ``/manual/account-api`` (:mod:`.web`).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ....core.plugin import Plugin, capability, place, setting
from ....core.pluginkit import CapError
from . import bundle as manual_bundle
from .sync import ManualSync

log = logging.getLogger("rook.hub.plugins.manual")

#: Seconds between checks for a knowledge base to seed while none was seeded.
SEED_EVERY = 60.0


def rook_version() -> str:
    from .... import __version__
    return __version__


def build_label() -> str:
    release = os.environ.get("ROOK_RELEASE", "").strip()
    if release:
        return release
    try:
        from ....worker import _build_info
        return str(_build_info.VERSION)
    except Exception:
        return ""


class _CachedBundle:
    """The bundle, re-read only when a page file changes (cheap stat)."""

    def __init__(self) -> None:
        self._sig = None
        self._bundle = None

    def __call__(self) -> manual_bundle.Bundle:
        d = manual_bundle.find_dir()
        sig = None
        if d is not None:
            sig = (str(d), tuple(sorted((p.name, p.stat().st_mtime_ns) for p in d.glob("*.md"))))
        if self._bundle is None or sig != self._sig:
            self._bundle, self._sig = manual_bundle.load(d), sig
        return self._bundle


class Manual(Plugin):
    NAMESPACE = "manual"
    NAME = "manual"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("auto_seed", bool, default=True, group="General", order=1,
                label="Copy the manual on a new hub",
                help="Import the user manual into an empty knowledge base as the \"Rook manual\" "
                     "pages. A knowledge base that already has pages is asked first."),
        setting("prompt_updates", bool, default=True, group="General", order=2,
                label="Ask after updates",
                help="When an update changes the manual, the dashboard asks whether to update the "
                     "knowledge base copy. Off: never ask (Help still shows the new manual)."),
    )
    GUIDANCE = {
        "tool:rook_knowledge": (
            "The Rook user manual is in this wiki: get page rook-manual for its contents (pages "
            "manual-<topic>, tag rook-manual), or search it first for how a Rook feature works. "
            "Without the wiki: rook_call(worker=\"rook\", cap=\"manual.get\", args={\"id\": \"<topic>\"})."),
    }
    SKILL = ("### manual\n"
             "The Rook user manual (plain-language help for every feature). In the knowledge wiki it "
             "is the page `rook-manual` and its children `manual-<topic>`; `manual.list` and "
             "`manual.get(id)` on worker `rook` read it even when the wiki is off. Check it before "
             "explaining a Rook feature to a person.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._seeder: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.sync: ManualSync | None = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    def available(self) -> bool:
        self.sync = ManualSync(_CachedBundle(), self.data_dir / "state.json",
                               rook_version(), build_label())
        return True

    # -- helpers ---------------------------------------------------------------
    def knowledge(self):
        """The knowledge service when the knowledge plugin is loaded, else None."""
        node = self._node
        if node is None:
            return None
        plugin = node.plugin("knowledge")
        return getattr(plugin, "service", None) if plugin is not None else None

    def _sync(self) -> ManualSync:
        if self.sync is None:
            self.available()
        return self.sync

    async def seed_once(self) -> dict | None:
        service = self.knowledge()
        if service is None:
            return None
        async with self._lock:
            return await self._sync().seed_if_fresh(
                service, auto_seed=bool(self.settings.get("auto_seed", True)))

    async def _seed_loop(self) -> None:
        await asyncio.sleep(2)
        while not self._sync().state().get("imported"):
            try:
                result = await self.seed_once()
                if result is not None:
                    log.info("manual: imported %d pages into the knowledge base", len(result["written"]))
            except Exception:
                log.exception("manual: seeding the knowledge base failed")
            await asyncio.sleep(SEED_EVERY)

    async def start(self) -> None:
        if self._seeder is None:
            self._seeder = asyncio.get_running_loop().create_task(self._seed_loop())

    async def stop(self) -> None:
        task, self._seeder = self._seeder, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- operations shared by the caps and the account API --------------------------
    def status_view(self) -> dict:
        return self._sync().status(self.knowledge(),
                                   prompt_enabled=bool(self.settings.get("prompt_updates", True)))

    async def do_update(self, action: str = "update", overwrite: list | None = None,
                        who: str = "") -> dict:
        sync = self._sync()
        if action == "snooze":
            return sync.snooze(24.0, who)
        if action == "decline":
            return sync.decline(who)
        if action != "update":
            raise ValueError("action is update, snooze or decline")
        service = self.knowledge()
        if service is None:
            raise ValueError("The knowledge base is off: switch on Knowledge and tasks first")
        async with self._lock:
            result = await sync.apply(service, overwrite=[str(i) for i in overwrite or []],
                                      reason="update", who=who)
        return {**result, "status": self.status_view()}

    # -- caps ----------------------------------------------------------------------
    @capability("status", risk="read")
    def status(self) -> dict:
        """The bundled manual and its knowledge copy: version, page count,
        what an update would add, change and remove (``diff``), and whether
        the dashboard should ask about it (``prompt``)."""
        return self.status_view()

    @capability("list", risk="read")
    def list_pages(self) -> dict:
        """The manual's table of contents: id, title, since, deprecated, and
        whether the page is read from the knowledge copy."""
        return self._sync().pages(self.knowledge())

    @capability("get", risk="read")
    def get(self, id: str, source: str = "auto") -> dict:
        """One manual page by id (``getting-started``; ``manual-`` prefix
        optional): title and Markdown body. ``source`` auto (the knowledge
        copy when imported, else the bundle) or bundle."""
        pid = id[len(manual_bundle.SLUG_PREFIX):] if id.startswith(manual_bundle.SLUG_PREFIX) else id
        try:
            return self._sync().page(pid, self.knowledge(), source=source)
        except ValueError as e:
            raise CapError(str(e)) from None

    @capability("update", risk="admin")
    async def update(self, action: str = "update", overwrite: list | None = None) -> dict:
        """Update the knowledge copy (``action`` update; ``overwrite`` = page
        ids whose local edits may be replaced), put the question off for a day
        (snooze) or for this manual version (decline). Operators only."""
        from ...authz import require_hub_admin
        denied = require_hub_admin("manual.update")
        if denied:
            raise CapError(denied)
        from ...node import HubNode
        try:
            return await self.do_update(action, overwrite, HubNode._caller())
        except ValueError as e:
            raise CapError(str(e)) from None


PLUGIN = Manual
