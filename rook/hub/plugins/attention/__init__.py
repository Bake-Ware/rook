"""``attention.*``: the Rook phone app as the one app allowed to interrupt the
person (worker ``rook``; docs/design/attention.md).

The hub puts decision cards (everything the approvals plugin lists as
waiting on the person) and alerts on the phones named in
``attention.devices``, and reads back taps and read signals. Pull only: the
phone never calls the hub; the hub calls the phone's ``attention.post``,
``attention.update`` and ``attention.poll`` (:mod:`.service`). A Yes or No
tapped on a card is relayed through :meth:`rook.hub.relay.Relay.decide` as
``device:<worker_id>``, so it counts only once the person ticked "May relay
my approvals" for that phone on the Tokens page.

* ``attention.send`` (write): an alert for the person's phones, delivered now
  or when a phone is back online;
* ``attention.status`` (read): per phone, online, last poll, last event,
  queued alerts and when the person last saw, opened or unlocked it ("no
  answer" is not "unseen");
* ``attention.notices`` (read): short summaries of urgent or family
  notifications the phones reported (the only notification content that
  leaves a phone, at most 140 characters each).

Inert while ``attention.devices`` is empty.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ....core.plugin import Plugin, capability, place, setting
from ....core.pluginkit import CapError
from .service import CHANNELS, URGENCIES, AttentionService

log = logging.getLogger("rook.hub.plugins.attention")


class Attention(Plugin):
    NAMESPACE = "attention"
    NAME = "attention"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    DEPENDS = ("approvals",)
    SETTINGS = (
        setting("devices", list, [], group="Attention", order=1, label="Phones",
                help="Workers (names or ids) running the Rook app that get decision cards and "
                     "alerts. Empty: nothing is sent. A card's Yes or No counts only once you "
                     "tick \"May relay my approvals\" for the phone on the Tokens page."),
        setting("person", str, "the person", group="Attention", order=2, label="Your name",
                help="Who taps the cards: recorded with each decision (\"approved by Bake via "
                     "phone (phone)\")."),
        setting("poll_secs", int, 10, min=2, max=300, group="Attention", order=3,
                advanced=True, label="Check phones every (s)"),
        setting("push_rules", bool, False, group="Attention", order=4, advanced=True,
                label="Send notification rules to phones",
                help="Not used yet: the hub derives no rules so far."),
    )
    SKILL = ("### attention (the person's phone)\n"
             "`attention.send {text, title?, urgency: urgent|normal, channel?, explain?}` on "
             "worker `rook` puts an alert on the person's phone (queued while it is offline). "
             "Use it sparingly: only what they must know now. `attention.status` says whether "
             "they saw, opened or unlocked the phone since (no answer is not unseen); "
             "`attention.notices {limit}` lists short summaries of their urgent and family "
             "notifications. Approvals reach the phone by themselves.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._service: AttentionService | None = None
        self._task: asyncio.Task | None = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    # -- plumbing --------------------------------------------------------------
    def _relay(self) -> Any:
        ap = self.dependency("approvals")
        if ap is None and self._node is not None:
            ap = self._node.plugin("approvals")
        return getattr(ap, "relay", None)

    @property
    def service(self) -> AttentionService:
        if self._service is None:
            self._service = AttentionService(
                self.data_dir / "state.json", settings=lambda: self.settings.as_dict(),
                client=lambda: getattr(self._node, "client", None), relay=self._relay)
        return self._service

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(2, int(self.settings.get("poll_secs") or 10)))
            if not self.settings.get("devices"):
                continue
            try:
                await self.service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("attention tick failed")

    def relay_rows(self) -> list[tuple[str, str]]:
        """``(device:<worker_id>, "<name> (phone)")`` for the Tokens page."""
        return self.service.relay_rows()

    # -- caps --------------------------------------------------------------------
    @capability("send", risk="write")
    async def send(self, text: str, title: str = "", urgency: str = "normal",
                   channel: str = "digest", explain: str = "") -> dict:
        """Put an alert on the person's phones (attention.devices): text, title?, urgency urgent|normal (urgent rings on the urgent channel), channel digest|family|jobs|urgent, explain? (2-3 sentences). Queued for a phone that is offline; returns its id and whether it was delivered now."""
        from ....core.context import caller_identity
        from ...authz import current_principal
        p = current_principal.get()
        source = (getattr(p, "label", "") or getattr(p, "id", "")
                  or str(caller_identity.get() or ""))
        if urgency not in URGENCIES:
            raise CapError("urgency: urgent or normal")
        if channel not in CHANNELS or channel == "approvals":
            raise CapError("channel: one of digest, family, jobs, urgent")
        try:
            return await self.service.send(text, title=title, urgency=urgency, channel=channel,
                                           explain=explain, source=source)
        except ValueError as e:
            raise CapError(str(e)) from None

    @capability("status", risk="read")
    def status(self) -> dict:
        """Per phone: online, last poll, last event, queued alerts, open cards, and when the person last saw, opened or unlocked it (no answer is not the same as unseen)."""
        return self.service.status()

    @capability("notices", risk="read", tags=("sensitive",))
    def notices(self, limit: int = 20) -> dict:
        """Recent short summaries (at most 140 characters) of urgent and family notifications the phones reported, newest first."""
        rows = self.service.notices(limit)
        return {"count": len(rows), "notices": rows}


PLUGIN = Attention
