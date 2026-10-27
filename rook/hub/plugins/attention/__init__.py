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
  leaves a phone, at most 140 characters each);
* ``attention.log`` (read): the last 30 days of decisions and reminders, one
  plain line each (what, why, how it was decided);
* ``attention.stream`` (read): the Feed, newest first: the phones' quiet
  feeds, alerts, decisions and work updates (:mod:`.feed`); the dashboard's
  Feed page shows the same (:mod:`.web`);
* ``attention.sorting`` (read): how the person sorted apps (ring me, tell me,
  just list it, hide it) and the family names; the hub pushes them to the
  phones. Only the person changes them, on the Feed page or the phone.

Decision cards queue: they are not emergencies, so they wait quietly in the
app and the phone speaks a short reminder when the room is quiet (settings
``queue`` and ``remind_*``). Urgent alerts still ring at once.

Inert while ``attention.devices`` is empty.
"""
from __future__ import annotations

import asyncio
import contextlib
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
        setting("family", list, [], group="Attention", order=3, label="Family names",
                help="Names the phone treats as family: their messages get a notification of "
                     "their own and show as important in the Feed. Starred contacts count too."),
        setting("push_rules", bool, True, group="Attention", order=4, advanced=True,
                label="Send my sorting to phones",
                help="How you sorted apps on the Feed page or the phone, and the family names, "
                     "go to every phone in front of its own rules. Off: each phone keeps "
                     "its own."),
        setting("feed_phone", bool, True, group="Attention", order=13, label="Phone feed in the Feed",
                help="The Feed page shows what the phone's quiet feed listed: each notification as "
                     "the phone summarized it (at most 140 characters, never the full text), kept "
                     "30 days on the hub. Off: only urgent and family ones."),
        setting("feed_pull_s", int, 60, min=10, max=3600, group="Attention", order=14,
                advanced=True, label="Read the phone feed every (s)"),
        setting("queue", bool, True, group="Attention", order=5, label="Queue decisions",
                help="Decision cards aren't emergencies: each is a normal notification on the "
                     "phone (no pop-up, no takeover on unlock) and the phone reminds you out "
                     "loud as well. Off: every card pops up as it arrives. Urgent alerts always "
                     "ring at once."),
        setting("remind", bool, True, group="Attention", order=6, label="Spoken reminders",
                help="The phone says \"Need your input: ...\" for one waiting card, or \"Hey "
                     "boss, you have 6 decisions pending.\" It listens for a few seconds first "
                     "and speaks only if nobody is talking (nothing is recorded or sent). Needs "
                     "app 0.4.22-beta.3 or later."),
        setting("remind_wait_s", int, 120, min=0, max=3600, group="Attention", order=7,
                advanced=True, label="Wait before announcing a new card (s)",
                help="A card answered quickly elsewhere is never announced, and cards that "
                     "arrive together are announced together."),
        setting("remind_gap_min", int, 15, min=1, max=1440, group="Attention", order=8,
                advanced=True, label="At least this long between reminders (min)"),
        setting("remind_again_min", int, 60, min=0, max=1440, group="Attention", order=9,
                advanced=True, label="Remind again while cards wait (min)",
                help="0: only when a new card arrives."),
        setting("remind_retry_min", int, 5, min=1, max=240, group="Attention", order=10,
                advanced=True, label="Try again after a busy room (min)"),
        setting("quiet_hours", str, "", group="Attention", order=11, advanced=True,
                label="No reminders between",
                help="Local time, like 22:00-07:00. Empty: reminders any time."),
        setting("remind_unchecked", bool, False, group="Attention", order=12, advanced=True,
                label="Remind even when the room can't be checked",
                help="Off: if the phone can't use the microphone to check the room, it stays "
                     "quiet."),
    )
    SKILL = ("### attention (the person's phone)\n"
             "`attention.send {text, title?, urgency: urgent|normal, channel?, explain?}` on "
             "worker `rook` puts an alert on the person's phone (queued while it is offline). "
             "Use it sparingly: only what they must know now. `attention.status` says whether "
             "they saw, opened or unlocked the phone since (no answer is not unseen); "
             "`attention.notices {limit}` lists short summaries of their urgent and family "
             "notifications. Approvals reach the phone by themselves: they queue quietly and the "
             "phone reminds the person out loud when the room is quiet. `attention.log {limit}` "
             "reads the 30-day log of decisions and reminders. `attention.stream {limit, before, "
             "source: phone|alert|decision|work, important}` is the person's Feed (what came in, "
             "newest first); `attention.sorting` says how they sorted apps (only they change "
             "it).\n")

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

    def _svc_settings(self) -> dict:
        out = dict(self.settings.as_dict())
        out["timezone"] = ""
        for ns in ("home", "job"):      # the person's zone, else the jobs' one
            with contextlib.suppress(Exception):
                out["timezone"] = str(self._node.plugin(ns).settings.get("timezone") or "")
            if out["timezone"].strip():
                break
        return out

    def log(self, source: str, outcome: str, what: str, why: str = "", how: str = "",
            ref: str = "") -> None:
        """One line in the 30-day log (other plugins: the voice's decisions)."""
        with contextlib.suppress(Exception):
            self.service.eventlog.add(source, outcome, what, why, how, ref)

    @property
    def service(self) -> AttentionService:
        if self._service is None:
            self._service = AttentionService(
                self.data_dir / "state.json", settings=self._svc_settings,
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

    def knowledge(self) -> Any:
        """The knowledge service, for the Feed's work updates (or None)."""
        if self._node is None:
            return None
        with contextlib.suppress(Exception):
            return getattr(self._node.plugin("knowledge"), "service", None)
        return None

    def relay_rows(self) -> list[tuple[str, str]]:
        """``(device:<worker_id>, "<name> (phone)")`` for the Tokens page."""
        return self.service.relay_rows()

    # -- caps --------------------------------------------------------------------
    @capability("send", risk="write", scope="multi-user")
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

    @capability("status", risk="read", scope="multi-user")
    def status(self) -> dict:
        """Per phone: online, last poll, last event, queued alerts, open cards, and when the person last saw, opened or unlocked it (no answer is not the same as unseen)."""
        return self.service.status()

    @capability("notices", risk="read", tags=("sensitive",), scope="multi-user")
    def notices(self, limit: int = 20) -> dict:
        """Recent short summaries (at most 140 characters) of urgent and family notifications the phones reported, newest first."""
        rows = self.service.notices(limit)
        return {"count": len(rows), "notices": rows}

    @capability("log", risk="read", scope="multi-user")
    def log_read(self, limit: int = 30, since: float = 0) -> dict:
        """The last 30 days of decisions and reminders, newest first: one plain line each (what happened, the summarized reason, how it was decided). limit 1-500; since: epoch seconds."""
        try:
            rows = self.service.eventlog.recent(limit, since)
        except Exception as e:  # noqa: BLE001
            raise CapError(f"the log can't be read: {type(e).__name__}") from None
        local = self.service._local
        return {"count": len(rows), "lines": [self.service.eventlog.line(r, local) for r in rows],
                "rows": [{k: r[k] for k in ("id", "at", "source", "outcome", "what", "why", "how")}
                         for r in rows]}

    @capability("stream", risk="read", tags=("sensitive",), scope="multi-user")
    def stream(self, limit: int = 30, before: float = 0, source: str = "",
               important: bool = False) -> dict:
        """The person's Feed, newest first: the phones' quiet feeds (summaries, never full text), alerts, decisions and finished or blocked tasks, plus what waits on them and how they sorted apps. limit 1-200; before: epoch seconds (page back); source phone|alert|decision|work; important: urgent and family only."""
        if source and source not in ("phone", "alert", "decision", "work"):
            raise CapError("source: phone, alert, decision or work")
        return self.service.stream(limit, before, source, bool(important), self.knowledge())

    @capability("sorting", risk="read", scope="multi-user")
    def sorting(self) -> dict:
        """How the person sorted apps (ring me, tell me, just list it, hide it) and the family names; pushed to every phone in front of its own rules. Only the person changes it (the Feed page or the phone's Attention tab)."""
        return {"sorting": self.service.sorting(), "family": self.service.family(),
                "pushed": bool(self.settings.get("push_rules", True))}


PLUGIN = Attention
