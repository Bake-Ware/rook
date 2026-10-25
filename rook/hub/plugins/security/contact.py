"""Telling the operator about held calls, and approving by voice
(advanced security; ``docs/design/permissions.md`` 3.11). Moved here from
rook/hub/approval_contact.py: with the plugin off a hold reaches the person
only as a card on their phone (the attention plugin) and on the dashboard.

``approvals.contact`` lists channels, tried in order for each new hold:

* ``telegram``: the hub's ``notify.send`` (every running Telegram or Discord
  integration);
* ``notify``: ``notify.post`` on ``approvals.contact_worker`` (a phone
  notification);
* ``voice``: ``voice.speak`` on that worker (spoken, no answer);
* ``ask``: ``voice.speak`` with ``reply`` on that worker. The phone asks one
  short question and listens. A clear yes approves that one hold, a clear no
  denies it; anything else, no answer, or a phone that is offline leaves it
  pending and moves on to the next channel.

Messages are coalesced: at most one per channel per
``approvals.notify_every_secs``; holds created in between are summarised in
one deferred message. Questions never overlap: while one is out, new holds
wait their turn and are asked about one at a time (each question names one
call, so a yes can only ever approve the hold it named).

Everything here runs in background tasks as the hub (``system:approvals``):
a failure is logged and never reaches the held call, and no agent-reachable
tool, cap or MCP surface can make the hub ask or decide. Messages never carry
call arguments, so they never carry secrets.

Voice approval is weaker than the dashboard: anyone near the phone can answer,
and a band PSK holder could in principle forge the phone's reply
(permissions 6.3). It is opt-in (only the ``ask`` channel does it) and capped
by ``approvals.voice_max_tier``; admin calls to the hub itself (policy,
settings, tokens, secrets) always need the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ....core import authz as core_authz

log = logging.getLogger("rook.hub.approval_contact")

#: Seconds the phone listens for an answer after the question.
REPLY_SECS = 15
#: Seconds a question may take end to end (speech + listening + transcription).
ASK_SECS = 60
SEND_SECS = 20

AFFIRMATIVE = ("yes", "approve", "approved", "allow", "do it", "go ahead", "ok", "okay")
NEGATIVE = ("no", "nope", "deny", "denied", "reject", "dont", "do not", "stop", "cancel", "never")
#: Words that keep a reply from approving (but do not deny on their own).
HESITANT = NEGATIVE + ("not", "wait", "hold on", "later")


def _norm(text: str) -> str:
    t = str(text or "").lower().replace("’", "").replace("'", "")
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", t).split()) + " "


def voice_verdict(text: str) -> str:
    """``approve`` only for a clear affirmative with no negation in it,
    ``deny`` for a clear negative, else ``unclear``."""
    t = _norm(text)
    has = lambda phrase: f" {phrase} " in t  # noqa: E731
    yes = any(has(p) for p in AFFIRMATIVE)
    if yes and not any(has(p) for p in HESITANT):
        return "approve"
    if not yes and any(has(p) for p in NEGATIVE):
        return "deny"
    return "unclear"


def voice_eligible(hold: dict, cfg: Any) -> bool:
    """Whether a spoken yes may approve this hold (``approvals.voice_max_tier``;
    never an admin call to the hub itself)."""
    top = str(getattr(cfg, "voice_max_tier", "off") or "off")
    if top == "off" or core_authz.norm_tier(top) is None:
        return False
    tier = str(hold.get("tier") or "exec")
    if core_authz.tier_rank(tier) > core_authz.tier_rank(top):
        return False
    if hold.get("is_hub") and core_authz.tier_rank(tier) >= core_authz.tier_rank("admin"):
        return False
    return True


def _who(hold: dict) -> str:
    return str(hold.get("principal_label") or hold.get("principal") or "an agent")


def _where(hold: dict) -> str:
    return str(hold.get("target_name") or hold.get("target_id") or "the band")


def _plain(hold: dict) -> dict:
    """The ask with a title made from the cap and machine only: this text
    goes to Telegram, notify and voice, so it never carries the args."""
    from ...plain import hold_item, outside
    try:
        return outside(hold_item(hold))
    except Exception:  # noqa: BLE001 - fall back to the bare facts
        return {"title": f"{hold.get('cap')} on {_where(hold)}", "who": _who(hold),
                "for": "no task", "risk": f"tier {hold.get('tier') or 'exec'}."}


def message(holds: list[dict], url: str = "") -> str:
    """The short text sent to the operator: the plain title, what it is for,
    who asks and the risk (rook/hub/plain.py); never the args themselves."""
    tail = f"Approve on the dashboard: {url}" if url else "Approve on the dashboard."
    if len(holds) == 1:
        p = _plain(holds[0])
        return (f"Rook: {p['title']}. For: {p['for']}. Who: {p['who']}. Risk: {p['risk']} "
                f"{tail}")
    items = "; ".join(f"{p['title']} ({p['who']}, for {p['for']})"
                      for p in (_plain(h) for h in holds[:5]))
    more = f"; and {len(holds) - 5} more" if len(holds) > 5 else ""
    return f"Rook: {len(holds)} asks are waiting for you: {items}{more}. {tail}"


def question(hold: dict) -> str:
    """The spoken question: title, who, and the risk level."""
    p = _plain(hold)
    return f"Rook: {p['who']} asks to {p['title'][:1].lower() + p['title'][1:]}. Risk: {p['risk']} Approve?"


class Contact:
    def __init__(self, approvals: Any) -> None:
        self.approvals = approvals
        self.client: Any = None
        self._tasks: set = set()
        self._last: dict[str, float] = {}
        self._later: dict[str, list] = {}
        self._flush: dict[str, asyncio.Task] = {}
        self._ask_lock: asyncio.Lock | None = None

    # -- entry -------------------------------------------------------------
    def hold_created(self, hold: dict) -> None:
        """A new hold: contact the operator in the background."""
        cfg = self.approvals.config()
        if self.client is None or not cfg.contact:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver(hold, cfg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, hold: dict, cfg: Any) -> None:
        try:
            channels = list(cfg.contact)
            for ch in channels:
                if ch == "ask":
                    if not voice_eligible(hold, cfg):
                        continue
                    outcome = await self.ask(hold, cfg)
                    if outcome in ("approved", "denied", "gone"):
                        return
                    continue
                if await self.text(ch, hold, cfg):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never reaches the held call
            log.exception("approvals: contacting the operator failed")

    # -- plumbing ----------------------------------------------------------------
    def _hub_id(self) -> str | None:
        local = getattr(self.client, "_local", None)
        if local is not None:
            return local.worker_id
        for wid, w in (getattr(self.client, "workers", {}) or {}).items():
            if "is_hub" in (w.get("roles") or ()):
                return wid
        return None

    def _phone(self, cfg: Any) -> tuple[str | None, str]:
        name = cfg.contact_worker
        if not name:
            return None, ""
        roster = getattr(self.client, "workers", {}) or {}
        if name in roster:
            return name, str(roster[name].get("name") or name)
        hits = [wid for wid, w in roster.items() if str(w.get("name") or "").lower() == name.lower()]
        return (hits[0], name) if len(hits) == 1 else (None, name)

    async def _call(self, cap: str, args: dict, target: str, timeout: float) -> dict:
        from ...authz import system
        reply = await self.client.call(cap=cap, args=args, target=target, timeout=timeout,
                                       identity="system:approvals",
                                       principal=system("approvals"))
        return reply if isinstance(reply, dict) else {"ok": False}

    @staticmethod
    def _ok(reply: dict) -> bool:
        res = reply.get("result")
        return bool(reply.get("ok")) and not (isinstance(res, dict) and res.get("ok") is False)

    async def send(self, channel: str, text: str, cfg: Any) -> bool:
        """One message over one channel; True when it was taken."""
        from ..jobs.kinds import notify_call
        if channel == "telegram":
            target = self._hub_id()
            cap, args = notify_call("telegram", text, channel="all")
        else:
            target, _name = self._phone(cfg)
            cap, args = notify_call(channel, text, title="Rook approval")
        if target is None:
            log.info("approvals: %s channel has no live worker", channel)
            return False
        try:
            reply = await self._call(cap, args, target, SEND_SECS)
        except Exception as e:  # noqa: BLE001
            log.info("approvals: %s message failed: %s", channel, e)
            return False
        if not self._ok(reply):
            log.info("approvals: %s message refused: %s", channel,
                     reply.get("error") or (reply.get("result") or {}).get("error"))
            return False
        return True

    async def text(self, channel: str, hold: dict, cfg: Any) -> bool:
        """A message about ``hold``, coalesced per channel."""
        every = max(1.0, float(cfg.notify_every_secs))
        now = time.monotonic()
        last = self._last.get(channel)
        if last is not None and now - last < every:
            self._later.setdefault(channel, []).append(hold["id"])
            if channel not in self._flush:
                task = asyncio.get_running_loop().create_task(
                    self._flush_later(channel, last + every - now, cfg))
                self._flush[channel] = task
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return True
        self._last[channel] = now
        ids = [hold["id"], *self._later.pop(channel, [])]
        holds = self._pending(ids) or [hold]
        return await self.send(channel, message(holds, self._url(cfg)), cfg)

    async def _flush_later(self, channel: str, delay: float, cfg: Any) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            ids = self._later.pop(channel, [])
            holds = self._pending(ids)
            if holds:
                self._last[channel] = time.monotonic()
                await self.send(channel, message(holds, self._url(cfg)), cfg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("approvals: deferred message failed")
        finally:
            self._flush.pop(channel, None)

    def _pending(self, ids: list) -> list[dict]:
        out = []
        for hid in dict.fromkeys(ids):
            try:
                h = self.approvals.queue.get(hid)
            except Exception:  # noqa: BLE001
                h = None
            if h is not None and h.get("state") == "pending":
                out.append(h)
        return out

    @staticmethod
    def _url(cfg: Any) -> str:
        return (cfg.dashboard_url + "/#approvals") if cfg.dashboard_url else ""

    # -- voice ---------------------------------------------------------------------
    async def ask(self, hold: dict, cfg: Any) -> str:
        """Ask about one hold on the phone. ``approved`` / ``denied`` (decided
        by voice), ``gone`` (no longer pending), ``unclear``, ``unanswered``
        or ``offline``. One question at a time."""
        from ..jobs.kinds import ask_args, heard
        if self._ask_lock is None:
            self._ask_lock = asyncio.Lock()
        async with self._ask_lock:
            current = self.approvals.queue.get(hold["id"])
            if current is None or current.get("state") != "pending":
                return "gone"
            wid, name = self._phone(cfg)
            if wid is None:
                log.info("approvals: phone %r is not on the band; not asking", cfg.contact_worker)
                return "offline"
            try:
                reply = await self._call("voice.speak", ask_args(question(current), ASK_SECS,
                                                                 REPLY_SECS),
                                         wid, ASK_SECS + REPLY_SECS)
            except Exception as e:  # noqa: BLE001
                log.info("approvals: asking on %s failed: %s", name, e)
                return "offline"
            if not self._ok(reply):
                return "offline"
            text, state, _via = heard(reply.get("result"))
            if not text:
                return "unanswered"
            verdict = voice_verdict(text)
            by = f"voice:{name}"
            if verdict == "unclear":
                self.approvals._journal("approval.voice", _where(current), by,
                                        {"ok": True, "hold": current["id"], "decision": "unclear",
                                         "heard": text[:300]})
                return "unclear"
            from ...approvals import HoldError
            try:
                self.approvals.decide(current["id"], verdict == "approve", by, scope="call",
                                      note=f"heard: {text[:200]}", heard=text)
            except HoldError:
                return "gone"  # decided on the dashboard meanwhile
            return "approved" if verdict == "approve" else "denied"
