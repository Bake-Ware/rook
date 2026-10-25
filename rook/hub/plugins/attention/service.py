"""The attention service: what the hub puts on the person's phones and what it
reads back (docs/design/attention.md).

The phone never calls the hub. Each tick, for every configured phone that is
online and announces ``attention.post``, the hub

1. **polls** ``attention.poll {since}``: a tap on Yes/No becomes
   :meth:`rook.hub.relay.Relay.decide` with ``relayer="device:<worker_id>"``
   (the consent gate: it works only once the person ticked "May relay my
   approvals" for the phone on the Tokens page), and the outcome goes back as
   ``attention.update`` done/failed. Seen, opened, dismissed, acked and
   unlocked signals and notice summaries go into rings (last 500);
2. **syncs cards**: every item the approvals plugin lists (held calls, held
   job calls, huddle cards, pinned questions) is posted once as a decision
   card; dashboard-only items get only an Explain button; an item that is no
   longer waiting gets ``attention.update`` gone;
3. **delivers alerts** queued by ``attention.send`` that it has not
   delivered yet (a phone that was offline gets them when it is back);
4. **reminds** (``attention.remind`` on the phone): with ``attention.queue``
   on, a decision card is not an emergency, so it lands quietly in the app
   (the digest channel: a normal notification, no heads-up or takeover on unlock) and the
   phone says one short line instead, when the room is quiet ("Need your
   input: Deploy hub update?", or "Hey boss, you have 6 decisions pending."),
   paced by the ``remind_*`` settings. The phone checks the room itself
   before it speaks and reports back with a ``reminded`` event. Urgent
   alerts still ring at once.

Every card queued, reminder spoken or skipped, and decision seen here goes
into the 30-day log (:mod:`rook.hub.eventlog`) as one plain line.

State (what was posted where, the last event seq per phone, the alert queue
and the rings) lives in ``state.json`` in the plugin's data dir, so a hub
restart neither re-rings every card nor replays old taps.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from ....core import names

log = logging.getLogger("rook.hub.plugins.attention")

IDENTITY = "system:rook-hub-attention"
ONLINE_SECS = 90.0
CALL_TIMEOUT = 10.0
RING_MAX = 500
ALERT_TTL = 24 * 3600          # an alert no phone took in a day is dropped
# A decision card never expires on the phone (Bake, 2026-10-23: no expiry of
# any kind): 0 is "never" to the app; the hub withdraws it with gone.
CARD_EXPIRES = 0
ALERTS_KEPT = 200
ID_MAX, LINE_MAX, EXPLAIN_MAX, SUMMARY_MAX, BODY_MAX = 64, 140, 600, 140, 600
URGENCIES = ("urgent", "normal")
CHANNELS = ("urgent", "family", "approvals", "digest", "jobs")
SIGNALS = ("seen", "opened", "dismissed", "acked", "unlocked")
PRIORITIES = ("urgent", "family", "normal")
TAPPED = {"approve": "Tapped Yes", "deny": "Tapped No"}
#: The channel a queued decision card uses: no heads-up and no takeover on unlock.
#: Since app 0.4.22-beta.6 a decision on it is a normal notification (the app's
#: "Waiting decisions" channel: status bar, shade, one sound) and is posted again on
#: unlock if it left the shade; older apps show it in the low, silent digest section.
QUEUE_CHANNEL = "digest"
#: Outcomes that mean the phone can't remind at all (its switch is off, it can't check
#: the room, it fails or never answers): after FALLBACK_AFTER of them in a row, cards pop
#: up again (approvals channel) until a reminder is spoken, so none sits unnoticed.
CANT_REMIND = ("off", "unchecked", "error", "lost")
FALLBACK_AFTER = 2
#: How long a reminder may stay unanswered by the phone before it counts as lost.
REMIND_LOST_S = 180.0
REMIND_TEXT_MAX = 300
#: What the phone reports after attention.remind (the app's contract).
REMIND_OUTCOMES = ("spoken", "busy", "call", "voice_session", "mic_in_use", "media",
                   "speaking", "unchecked", "off", "dnd", "error")
NOT_SPOKEN = {"busy": "someone was talking", "call": "you were on a call",
              "voice_session": "you were talking to the voice", "mic_in_use":
              "another app was using the microphone", "media": "something was playing",
              "speaking": "the phone was already speaking", "unchecked":
              "the phone couldn't check the room", "off": "spoken reminders are off in the app",
              "dnd": "the phone was on Do Not Disturb",
              "error": "the phone couldn't speak it", "lost": "the phone didn't answer"}


def short(text: Any, limit: int) -> str:
    """One line of at most ``limit`` characters (an ellipsis when cut): the
    phone's fields have hard limits, so no truncation marker here."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_text(frm: Any) -> str:
    if not isinstance(frm, dict):
        return ""
    parts = []
    if frm.get("job"):
        parts.append(f"job {frm['job']}")
    if frm.get("role"):
        parts.append(f"role {frm['role']}")
    if frm.get("huddle"):
        parts.append(f"huddle {frm.get('huddle_title') or frm['huddle']}")
    return ", ".join(parts)


def card_for(item: dict) -> dict:
    """The ``attention.post`` args for one waiting item (rook/hub/relay.py):
    the plain title as the line, For / Who / I want to / Why / Risk as the
    body (always shown), the technical detail behind Explain
    (rook/hub/plain.py)."""
    p = item.get("plain")
    if p:
        explain = str(p.get("explain") or "")
        if not item.get("relayable", True):
            where = "Decisions" if item.get("kind") == "house" else "Approvals"
            explain = (f"{item.get('why_not') or 'Dashboard only'}: decide it on the "
                       f"dashboard's {where} page. " + explain)
        from ...plain import lines
        return {"id": str(item["id"]), "kind": item.get("kind") or "hold",
                "line": short(p.get("title") or "A decision for you", LINE_MAX),
                "body": "\n".join(short(x, 200) for x in lines(p))[:BODY_MAX],
                "explain": short(explain, EXPLAIN_MAX), "urgency": "normal",
                "channel": "approvals",
                "actions": ["yes", "no", "explain"] if item.get("relayable", True) else ["explain"],
                "source": short(item.get("name") or "approvals", 64),
                "expires_at": CARD_EXPIRES}
    who, what = item.get("who") or "an agent", str(item.get("what") or "").rstrip(".")
    frm = _from_text(item.get("from"))
    explain = (f"{who} asked to {what}. "
               f"Risk: {item.get('risk') or 'unknown'}"
               + (f", from {frm}" if frm else "")
               + f", waiting {item.get('age') or 'a moment'}.")
    if not item.get("relayable", True):
        explain += (f" This one is {item.get('why_not') or 'dashboard only'}: decide it on the "
                    "dashboard's Approvals page.")
    if item.get("line"):
        # The house agent's own one-line ask (front door): its line and its
        # plain explanation, not a description of a call.
        explain = str(item.get("explain") or item["line"])
        if not item.get("relayable", True):
            explain += " Answer it on the dashboard's Decisions page."
    return {"id": str(item["id"]), "kind": item.get("kind") or "hold",
            "line": short(item.get("line") or f"{who} wants to {what}", LINE_MAX),
            "explain": short(explain, EXPLAIN_MAX), "urgency": "normal",
            "channel": "approvals",
            "actions": ["yes", "no", "explain"] if item.get("relayable", True) else ["explain"],
            "source": short(item.get("name") or "approvals", 64),
            "expires_at": CARD_EXPIRES}


def speakable(item: dict) -> str:
    """A waiting item's title as it may be spoken or logged (rook/hub/relay.py
    ``summary``): a held call's from its cap and machine only, never its
    arguments; a question's or house ask's own plain title."""
    from ...plain import safe_title
    p = item.get("plain") or {}
    if item.get("cap") and item.get("kind") not in ("ask", "house"):
        title = safe_title(item["cap"], str(item.get("worker") or ""), hub=bool(item.get("hub")))
    else:
        title = p.get("title") or item.get("line") or "a question for you"
    return short(title, 120)


def without_body(card: dict) -> dict:
    """A card for a phone whose app predates ``body`` (0.4.21 and older): the
    title and what it is for as the line, the body ahead of the detail under
    Explain."""
    body = str(card.get("body") or "")
    out = {k: v for k, v in card.items() if k != "body"}
    if not body:
        return out
    first = body.split("\n", 1)[0]
    if first.startswith("For: ") and first != "For: no task":
        out["line"] = short(f"{card['line']} ({first[5:]})", LINE_MAX)
    out["explain"] = short(" ".join(body.split("\n")) + " Details: " + str(card.get("explain") or ""),
                           EXPLAIN_MAX)
    if "explain" not in out.get("actions", []) and out.get("actions"):
        out["actions"] = list(out["actions"]) + ["explain"]
    return out


def no_body(error: Exception) -> bool:
    """An older app refusing the ``body`` argument."""
    text = str(error)
    return "body" in text and ("unexpected" in text or "bad args" in text)


def alert_post(alert: dict) -> dict:
    line = alert["text"] if not alert.get("title") else f"{alert['title']}: {alert['text']}"
    return {"id": alert["id"], "kind": "alert", "line": short(line, LINE_MAX),
            "explain": short(alert.get("explain") or alert["text"], EXPLAIN_MAX),
            "urgency": alert["urgency"], "channel": alert["channel"], "actions": ["ack"],
            "source": short(alert.get("source") or "rook", 64),
            "expires_at": int(alert["created"] + ALERT_TTL)}


def online(w: dict, now: float | None = None) -> bool:
    return ((now or time.time()) - float(w.get("last_seen") or 0)) < ONLINE_SECS


class AttentionService:
    """``settings()`` gives ``{devices, person, poll_secs, push_rules}``;
    ``client()`` the hub's band client; ``relay()`` the approvals plugin's
    :class:`rook.hub.relay.Relay` (or None)."""

    def __init__(self, path: Path, settings: Callable[[], dict], client: Callable[[], Any],
                 relay: Callable[[], Any], clock: Callable[[], float] = time.time,
                 trust: Callable[[], Any] | None = None, log: Any = None) -> None:
        self.path = Path(path)
        self._settings, self._client, self._relay, self.clock = settings, client, relay, clock
        self._trust = trust or (lambda: None)
        if log is None:
            from ...eventlog import EventLog
            log = EventLog(self.path.parent / "event_log.db", clock=clock)
        self.eventlog = log
        self.lock = asyncio.Lock()
        self.state = self._load()

    # -- state ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        for key, empty in (("devices", {}), ("alerts", []), ("signals", []), ("notices", []),
                           ("remind", {})):
            if not isinstance(data.get(key), type(empty)):
                data[key] = empty
        return data

    def save(self) -> None:
        from ....core.db import atomic_write_json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.state, 0o600, indent=1)

    def _dev(self, wid: str) -> dict:
        d = self.state["devices"].setdefault(wid, {})
        d.setdefault("posted", {})
        d.setdefault("last_seq", 0)
        d.setdefault("signals", {})
        return d

    def _ring(self, key: str, row: dict) -> None:
        ring = self.state[key]
        ring.append(row)
        del ring[:-RING_MAX]

    # -- devices ------------------------------------------------------------------
    def settings(self) -> dict:
        try:
            return dict(self._settings() or {})
        except Exception:
            return {}

    def person(self) -> str:
        return short(self.settings().get("person") or "the person", 120)

    def roster(self) -> dict:
        c = self._client()
        with contextlib.suppress(Exception):
            return dict(getattr(c, "workers", {}) or {}) if c is not None else {}
        return {}

    def configured(self) -> list[dict]:
        """``attention.devices`` resolved against the roster: ``{entry,
        worker_id, name, online, attention}``; worker_id is None for an entry
        no worker on the band matches."""
        roster, now = self.roster(), self.clock()
        by_name: dict[str, list[str]] = {}
        for wid, w in roster.items():
            by_name.setdefault(str(w.get("name") or "").lower(), []).append(wid)
        out, seen = [], set()
        for entry in self.settings().get("devices") or []:
            entry = str(entry or "").strip()
            if not entry:
                continue
            hits = [entry] if entry in roster else by_name.get(entry.lower(), [])
            wid = hits[0] if len(hits) == 1 else None
            if wid is not None and wid in seen:
                continue
            w = roster.get(wid) or {}
            caps = w.get("caps") or []
            if wid:
                seen.add(wid)
            out.append({"entry": entry, "worker_id": wid, "name": str(w.get("name") or entry),
                        "online": bool(wid) and online(w, now),
                        "attention": "attention.post" in caps,
                        "rules": "attention.rules_set" in caps,
                        **({"ambiguous": True} if len(hits) > 1 else {})})
        return out

    def reachable(self) -> list[dict]:
        return [d for d in self.configured() if d["worker_id"] and d["online"] and d["attention"]]

    @staticmethod
    def principal(wid: str) -> str:
        return f"device:{wid}"

    @staticmethod
    def label(dev: dict) -> str:
        return f"{dev['name']} (phone)"

    async def call(self, cap: str, args: dict, wid: str) -> dict:
        """One call to a phone as the hub itself; the result dict, or raises."""
        from ...authz import system
        c = self._client()
        if c is None:
            raise RuntimeError("the hub has no band client")
        reply = await c.call(cap=cap, args=args, target=wid, timeout=CALL_TIMEOUT,
                             identity=IDENTITY, principal=system(IDENTITY))
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise RuntimeError(str((reply or {}).get("error") or f"{cap} failed"))
        res = reply.get("result")
        if isinstance(res, dict) and res.get("ok") is False:
            raise RuntimeError(str(res.get("error") or f"{cap} refused"))
        return res if isinstance(res, dict) else {}

    # -- the tick ------------------------------------------------------------------
    async def tick(self) -> dict:
        """One round over every reachable phone: poll, sync cards, deliver
        alerts. Inert (no calls at all) while ``attention.devices`` is empty."""
        done: dict = {}
        if not self.settings().get("devices"):
            return done
        async with self.lock:
            self._expire_alerts()
            for dev in self.reachable():
                wid = dev["worker_id"]
                try:
                    done[dev["name"]] = {"events": await self._poll(dev),
                                         **await self._sync_cards(dev),
                                         "alerts": await self._deliver(dev)}
                    said = await self._remind(dev)
                    if said:
                        done[dev["name"]]["remind"] = said
                    if self.settings().get("push_rules") and dev["rules"]:
                        await self.push_rules(dev)
                except Exception as e:     # one phone failing never stops the others
                    log.info("attention: %s: %s", dev["name"], e)
                    done[dev["name"]] = {"error": str(e)}
                finally:
                    self._dev(wid)["last_contact"] = self.clock()
            self.save()
        return done

    async def _poll(self, dev: dict) -> int:
        wid = dev["worker_id"]
        d = self._dev(wid)
        res = await self.call("attention.poll", {"since": int(d["last_seq"])}, wid)
        d["last_poll"] = self.clock()
        if res.get("reset") is True:
            self._restarted(d)             # the phone's log is behind us: app data cleared
        events = [e for e in (res.get("events") or []) if isinstance(e, dict)
                  and type(e.get("seq")) is int]
        events.sort(key=lambda e: e["seq"])
        n = 0
        for ev in events:
            if ev["seq"] <= d["last_seq"]:
                continue
            try:
                await self._event(dev, ev)
            except Exception:
                log.exception("attention: event %s from %s", ev.get("type"), dev["name"])
            d["last_seq"] = ev["seq"]
            d["last_event"] = {"type": str(ev.get("type") or ""), "seq": ev["seq"],
                               "ts": self._ts(ev)}
            n += 1
            self.save()                    # a hub crash mid-batch never replays a tap
        last = res.get("last")
        if type(last) is int and not events:
            if last < d["last_seq"]:
                self._restarted(d)         # the app's counter restarted (reinstalled)
            elif last > d["last_seq"]:
                d["last_seq"] = last
        return n

    @staticmethod
    def _restarted(d: dict) -> None:
        """The phone lost its log (app data cleared, reinstalled): read it from
        the start, and post its open cards again since it lost those too."""
        d["last_seq"] = 0
        d["posted"] = {k: v for k, v in d["posted"].items() if v.get("closed")}

    def _ts(self, ev: dict) -> float:
        ts = ev.get("ts")
        return float(ts) if isinstance(ts, (int, float)) and ts > 0 else self.clock()

    async def _event(self, dev: dict, ev: dict) -> None:
        kind, wid = ev.get("type"), dev["worker_id"]
        ts = self._ts(ev)
        if kind == "decision":
            await self._decision(dev, ev, ts)
        elif kind in SIGNALS:
            ident = short(ev.get("id"), ID_MAX) if ev.get("id") else ""
            self._dev(wid)["signals"][kind] = ts
            # When the hub heard it, on its own clock (reminder pacing compares these).
            self._dev(wid).setdefault("heard", {})[kind] = self.clock()
            self._ring("signals", {"worker_id": wid, "device": dev["name"], "type": kind,
                                   **({"id": ident} if ident else {}), "ts": ts,
                                   "received": self.clock()})
            if kind == "acked" and ident:
                for a in self.state["alerts"]:
                    if a["id"] == ident:
                        a.setdefault("acked", {})[wid] = ts
        elif kind == "reminded":
            self._reminded(dev, ev, ts)
        elif kind == "notice":
            pri = ev.get("priority") if ev.get("priority") in PRIORITIES else "normal"
            self._ring("notices", {"worker_id": wid, "device": dev["name"],
                                   "app": short(ev.get("app"), 80),
                                   "category": short(ev.get("category"), 40),
                                   "priority": pri, "summary": short(ev.get("summary"), SUMMARY_MAX),
                                   "ts": ts, "received": self.clock()})

    async def _decision(self, dev: dict, ev: dict, ts: float) -> None:
        from ...relay import RelayError
        wid, item = dev["worker_id"], str(ev.get("id") or "")
        posted = self._dev(wid)["posted"].get(item)
        decision = ev.get("decision")
        if decision not in TAPPED:
            return
        if posted is None or posted.get("closed"):
            # Only a card this hub put on this phone, still open, is decided
            # from it: a tap on anything else changes nothing.
            await self._update(wid, item, "gone", "This is no longer waiting.")
            return
        relay = self._relay()
        principal = self.principal(wid)
        if relay is None:
            msg = "Not done: approvals are unavailable on the hub. Decide on the dashboard."
        elif not relay.may_relay(principal):
            msg = (f"Not done: {self.label(dev)} may not relay your approvals yet. Turn on "
                   f"\"May relay my approvals\" for {self.label(dev)} on the dashboard's Tokens "
                   "page, or decide on the Approvals page.")
        else:
            said = {"who": self.person(),
                    "quote": f"{TAPPED[decision]} on the decision card on {dev['name']}",
                    "session": f"{principal} card {item} {iso(ts)}"}
            try:
                out = await relay.decide(item, decision, said, relayer=principal,
                                         relayer_label=self.label(dev),
                                         reason=short(ev.get("reason"), 500))
            except (PermissionError, RelayError) as e:
                msg = f"Not done: {e}"
                if "may not relay" in str(e):
                    msg += (f" Turn on \"May relay my approvals\" for {self.label(dev)} on the "
                            "dashboard's Tokens page.")
            except Exception as e:  # noqa: BLE001
                log.exception("attention: relaying %s from %s", item, dev["name"])
                msg = f"Not done: the hub could not record it ({type(e).__name__})."
            else:
                posted.update(closed=True, state="done", decided=ts)
                self.log("decided", f"{'Approved' if decision == 'approve' else 'Declined'}: "
                         f"{out.get('name') or item}", "your answer on the decision card",
                         how=f"{self.person()} tapped {'Yes' if decision == 'approve' else 'No'} "
                             f"on {dev['name']}", ref=item)
                await self._update(wid, item, "done", short(out.get("recorded") or "Done.",
                                                            EXPLAIN_MAX))
                return
        posted.update(state="failed", failed=ts, relayable_when_failed=bool(
            relay is not None and relay.may_relay(principal)))
        await self._update(wid, item, "failed", short(msg, EXPLAIN_MAX))

    async def _update(self, wid: str, item: str, state: str, message: str) -> bool:
        try:
            await self.call("attention.update", {"id": item, "state": state,
                                                 "message": message}, wid)
            return True
        except Exception as e:
            log.info("attention: update %s %s on %s: %s", item, state, wid, e)
            return False

    async def _sync_cards(self, dev: dict) -> dict:
        wid = dev["worker_id"]
        relay = self._relay()
        if relay is None:
            return {"posted": 0, "gone": 0}
        posted = self._dev(wid)["posted"]
        items = {str(i["id"]): i for i in relay.items()}
        may = relay.may_relay(self.principal(wid))
        n_new = n_gone = 0
        for ident, it in items.items():
            if len(ident) > ID_MAX:
                continue
            old = posted.get(ident)
            # A tap refused because the phone could not relay yet is offered
            # again once the person turned relaying on for it.
            again = (old is not None and old.get("state") == "failed" and may
                     and not old.get("relayable_when_failed"))
            # Reminders stopped working: a quietly queued card is posted again on the
            # approvals channel (the same id: no new ring, it takes over on unlock).
            again = again or (old is not None and old.get("queued") and not old.get("closed")
                              and not self.queued_on(dev))
            if old is not None and not again:
                continue
            card = card_for(it)
            queued = self.queued_on(dev)
            if queued:
                card["channel"] = QUEUE_CHANNEL
            try:
                await self._post_card(dev, card)
            except Exception as e:
                log.info("attention: post %s to %s: %s", ident, dev["name"], e)
                continue
            posted[ident] = {"kind": card["kind"], "at": self.clock(),
                             "actions": card["actions"], **({"queued": True} if queued else {})}
            n_new += 1
            if not again:
                self.log("ask", f"Card: {speakable(it)}",
                         "not an emergency, so it waits quietly in the app" if queued
                         else "a decision for you", how=f"on {dev['name']}", ref=ident)
        for ident in [i for i in posted if i not in items]:
            row = posted[ident]
            if row.get("closed") or await self._update(wid, ident, "gone",
                                                       "No longer waiting on you."):
                if not row.get("closed"):
                    self.log("decided", "A card is no longer waiting",
                             "it was answered elsewhere or withdrawn", how="seen by the hub",
                             ref=ident)
                del posted[ident]
                n_gone += 1
        return {"posted": n_new, "gone": n_gone}

    async def _post_card(self, dev: dict, card: dict) -> None:
        """``attention.post`` with the body; a phone whose app predates it
        gets the card folded into line + Explain (remembered per app
        version, so an updated app gets bodies again)."""
        wid = dev["worker_id"]
        d = self._dev(wid)
        w = self.roster().get(wid) or {}
        version = str(w.get("version") or w.get("build") or "")
        if "body" in card and d.get("no_body") is not None and d.get("no_body") == version:
            await self.call("attention.post", without_body(card), wid)
            return
        try:
            await self.call("attention.post", card, wid)
        except Exception as e:  # noqa: BLE001
            if "body" not in card or not no_body(e):
                raise
            d["no_body"] = version
            await self.call("attention.post", without_body(card), wid)

    # -- the queue and spoken reminders ------------------------------------------------
    def queueing(self) -> bool:
        """Decision cards wait quietly (no heads-up or takeover) and reminders bring
        them up: ``attention.queue`` (on by default)."""
        return self.settings().get("queue", True) is not False

    def reminding(self) -> bool:
        return self.queueing() and self.settings().get("remind", True) is not False

    def queued_on(self, dev: dict) -> bool:
        """Whether a card for this phone queues quietly: the queue is on and the phone
        can remind (its app has ``attention.remind``), or the person turned spoken
        reminders off and wants the quiet queue alone. A phone whose app can't
        remind yet still gets cards that pop up, so nothing goes unnoticed."""
        if not self.queueing():
            return False
        if not self.reminding():
            return True
        if self.state["remind"].get("fallback"):
            return False
        caps = (self.roster().get(dev["worker_id"]) or {}).get("caps") or []
        return "attention.remind" in caps

    def log(self, outcome: str, what: str, why: str = "", how: str = "",
            ref: str = "") -> None:
        if self.eventlog is None:
            return
        with contextlib.suppress(Exception):
            self.eventlog.add("attention", outcome, what, why, how, ref)

    def waiting(self) -> list[dict]:
        """Every decision waiting on the person, oldest first: ``{id, created,
        title}``. The approvals list (held calls, job holds, huddle cards and
        questions, house asks) plus pending trust-map proposals. Titles are the
        speakable kind (rook/hub/relay.py ``summary``): a held call's title comes
        from its cap and machine only, never its arguments."""
        out: list[dict] = []
        relay = self._relay()
        if relay is not None:
            with contextlib.suppress(Exception):
                for it in relay.items():
                    out.append({"id": str(it["id"]), "created": float(it.get("created") or 0),
                                "title": speakable(it)})
        trust = None
        with contextlib.suppress(Exception):
            trust = self._trust()
        if trust is not None:
            with contextlib.suppress(Exception):
                for prop in trust.current().get("proposals") or []:
                    if prop.get("state") == "pending":
                        out.append({"id": str(prop["id"]), "created": float(prop.get("at") or 0),
                                    "title": "a change to the trust map"})
        out.sort(key=lambda i: i["created"])
        return out

    @staticmethod
    def reminder_text(items: list[dict]) -> str:
        """One card: "Need your input: <its title>"; several: "Hey boss, you have 6
        decisions pending." (Bake's own words, 2026-10-23)."""
        if len(items) == 1:
            title = str(items[0]["title"]).strip().rstrip(".")
            return short(f"Need your input: {title}" + ("" if title.endswith("?") else "."),
                         REMIND_TEXT_MAX)
        return f"Hey boss, you have {len(items)} decisions pending."

    def _local(self, ts: float) -> datetime.datetime:
        tz = str(self.settings().get("timezone") or "").strip()
        if tz:
            with contextlib.suppress(Exception):
                from zoneinfo import ZoneInfo
                return datetime.datetime.fromtimestamp(ts, ZoneInfo(tz))
        return datetime.datetime.fromtimestamp(ts).astimezone()

    def quiet_hours(self, ts: float | None = None) -> bool:
        """Inside ``attention.quiet_hours`` ("22:00-07:00", local time; empty: never)."""
        spec = str(self.settings().get("quiet_hours") or "").strip()
        win = parse_hours(spec)
        if win is None:
            return False
        now = self._local(self.clock() if ts is None else ts)
        minute = now.hour * 60 + now.minute
        start, end = win
        return start <= minute < end if start <= end else (minute >= start or minute < end)

    def speaker(self) -> dict | None:
        """The one phone that speaks reminders: the first configured phone that is
        reachable and has ``attention.remind`` (one voice, never a chorus)."""
        roster = self.roster()
        for dev in self.reachable():
            if "attention.remind" in ((roster.get(dev["worker_id"]) or {}).get("caps") or []):
                return dev
        return None

    def _minutes(self, key: str, default: float) -> float:
        try:
            return max(0.0, float(self.settings().get(key, default)))
        except (TypeError, ValueError):
            return default

    async def _remind(self, dev: dict) -> dict | None:
        """One reminder, when one is due, on the speaking phone. The phone checks
        that the room is quiet first and reports a ``reminded`` event."""
        st = self.state["remind"]
        cfg = self.settings()
        if not self.reminding():
            return None
        sp = self.speaker()
        if sp is None or sp["worker_id"] != dev["worker_id"]:
            return None
        now = self.clock()
        items = self.waiting()
        if not items:
            st.pop("announced", None)
            st.pop("skipped", None)
            return None
        inflight = st.get("inflight")
        if inflight:
            if now - float(inflight.get("at") or 0) < REMIND_LOST_S:
                return None
            self._skipped("lost", inflight)     # the phone never answered
            st.pop("inflight", None)
        if self.quiet_hours(now):
            return None
        if now - float(st.get("spoken_at") or 0) < self._minutes("remind_gap_min", 15) * 60:
            return None
        if now - float(st.get("attempt_at") or 0) < self._minutes("remind_retry_min", 5) * 60:
            return None
        wait = self._minutes("remind_wait_s", 120)
        ready = [i for i in items if now - i["created"] >= wait]
        if not ready:
            return None
        announced = set(st.get("announced") or ())
        again = self._minutes("remind_again_min", 60)
        spoken_at = float(st.get("spoken_at") or 0)
        # A repeat (nothing new) only once the phone was unlocked since the last one:
        # never the same line again and again to an empty or sleeping room.
        used = float((self._dev(dev["worker_id"]).get("heard") or {}).get("unlocked") or 0)
        due = any(i["id"] not in announced for i in ready) or (
            again > 0 and now - spoken_at >= again * 60 and used > spoken_at)
        if not due:
            return None
        text = self.reminder_text(items)
        rid = "r" + secrets.token_hex(5)
        st["attempt_at"] = now
        try:
            res = await self.call("attention.remind", {
                "id": rid, "text": text, "sample_ms": 3000,
                "speak_unchecked": bool(cfg.get("remind_unchecked", False))}, dev["worker_id"])
        except Exception as e:  # noqa: BLE001
            log.info("attention: remind on %s: %s", dev["name"], e)
            return {"error": str(e)[:200]}
        if res.get("accepted") is False:
            # The app says why: reason "off" (its switch), or busy: another reminder of its
            # own is still running (the phone is speaking, not the room).
            why = str(res.get("reason") or ("speaking" if res.get("busy") else "busy"))
            self._skipped(why if why in REMIND_OUTCOMES and why != "spoken" else "busy",
                          {"text": text})
            return {"accepted": False}
        st["inflight"] = {"id": rid, "at": now, "text": text, "ids": [i["id"] for i in items],
                          "device": dev["name"]}
        return {"id": rid, "text": text}

    def _reminded(self, dev: dict, ev: dict, ts: float) -> None:
        st = self.state["remind"]
        inflight = st.get("inflight") or {}
        if not inflight or str(ev.get("id") or "") != inflight.get("id"):
            return
        st.pop("inflight", None)
        outcome = ev.get("outcome") if ev.get("outcome") in REMIND_OUTCOMES else "error"
        # Pacing runs on the hub's clock, never the phone's (it may be off).
        now = self.clock()
        if outcome == "spoken":
            st["spoken_at"] = now
            st["last_outcome"] = "spoken"
            st["announced"] = list(inflight.get("ids") or ())
            st.pop("skipped", None)
            st.pop("cant", None)
            if st.pop("fallback", None):
                self.log("reminded", "Spoken reminders work again",
                         "a reminder was spoken, so new cards wait quietly again",
                         how=f"on {dev['name']}")
            n = len(inflight.get("ids") or ())
            via = {"server": "in the house voice", "device": "in the phone's own voice"}.get(
                str(ev.get("via") or ""), "")
            self.log("reminded", f"Reminded you out loud: {inflight.get('text')}",
                     f"{n} decision{'s' if n != 1 else ''} waiting and the room was quiet",
                     how=f"spoken on {dev['name']}" + (f" {via}" if via else ""))
            return
        st["attempt_at"] = now
        self._skipped(outcome, inflight)

    def _skipped(self, outcome: str, inflight: dict) -> None:
        """A reminder that wasn't spoken: logged once per reason until one is spoken."""
        st = self.state["remind"]
        st["last_outcome"] = outcome
        st["cant"] = int(st.get("cant") or 0) + 1 if outcome in CANT_REMIND else 0
        if st["cant"] >= FALLBACK_AFTER and not st.get("fallback"):
            st["fallback"] = True
            self.log("skipped", "Cards pop up again",
                     f"the phone can't remind you ({NOT_SPOKEN.get(outcome, outcome)}), so "
                     "waiting cards show the usual way until a reminder is spoken")
        if st.get("skipped") == outcome:
            return
        st["skipped"] = outcome
        retry = int(self._minutes("remind_retry_min", 5))
        self.log("skipped", "Didn't remind you yet",
                 f"{NOT_SPOKEN.get(outcome, outcome)}; "
                 + ("turn Spoken reminders on in the app's Attention tab" if outcome == "off"
                    else f"trying again in {retry} min"))

    # -- alerts ---------------------------------------------------------------------
    def _expire_alerts(self) -> None:
        now = self.clock()
        for a in self.state["alerts"]:
            if not a.get("expired") and now - a["created"] > ALERT_TTL:
                a["expired"] = now
        del self.state["alerts"][:-ALERTS_KEPT]

    def pending(self, wid: str) -> list[dict]:
        return [a for a in self.state["alerts"]
                if not a.get("expired") and wid not in (a.get("delivered") or {})]

    async def _deliver(self, dev: dict) -> int:
        wid, n = dev["worker_id"], 0
        for a in self.pending(wid):
            try:
                await self.call("attention.post", alert_post(a), wid)
            except Exception as e:
                log.info("attention: alert %s to %s: %s", a["id"], dev["name"], e)
                break
            a.setdefault("delivered", {})[wid] = self.clock()
            n += 1
        return n

    async def send(self, text: str, title: str = "", urgency: str = "normal",
                   channel: str = "digest", explain: str = "", source: str = "") -> dict:
        """Queue an alert for every configured phone and deliver it now where
        one is reachable."""
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("text: what to tell the person")
        if urgency not in URGENCIES:
            raise ValueError("urgency: urgent or normal")
        channel = "urgent" if urgency == "urgent" else (channel or "digest")
        if channel not in CHANNELS:
            raise ValueError("channel: one of " + ", ".join(CHANNELS))
        if not self.configured():
            raise ValueError("no phone is set up for attention: set attention.devices "
                             "(Settings > Attention) to the person's phone")
        async with self.lock:
            taken = {a["id"] for a in self.state["alerts"]}
            ident = names.generate()
            while ident in taken:
                ident = f"{names.generate()}-{secrets.token_hex(2)}"
            alert = {"id": ident, "text": short(text, 2000), "title": short(title, 80),
                     "urgency": urgency, "channel": channel, "explain": short(explain, EXPLAIN_MAX),
                     "source": short(source, 64), "created": self.clock(), "delivered": {}}
            self.state["alerts"].append(alert)
            delivered = []
            for dev in self.reachable():
                with contextlib.suppress(Exception):
                    await self.call("attention.post", alert_post(alert), dev["worker_id"])
                    alert["delivered"][dev["worker_id"]] = self.clock()
                    delivered.append(dev["name"])
            self._expire_alerts()
            self.save()
        waiting = [d["name"] for d in self.configured()
                   if not d["worker_id"] or d["worker_id"] not in alert["delivered"]]
        return {"id": ident, "delivered_now": bool(delivered), "delivered": delivered,
                "queued_for": waiting}

    # -- rules (stub) -----------------------------------------------------------------
    def rules(self) -> list:
        """Classification preferences for the phone's notice filter. A stub:
        they will come from the trust map; nothing is derived yet."""
        return []

    async def push_rules(self, dev: dict) -> bool:
        """``attention.rules_set`` once per change, only with
        ``attention.push_rules`` on (the tick checks)."""
        rules = self.rules()
        digest = json.dumps(rules, sort_keys=True)
        d = self._dev(dev["worker_id"])
        if d.get("rules_sent") == digest:
            return False
        await self.call("attention.rules_set", {"rules": rules}, dev["worker_id"])
        d["rules_sent"] = digest
        return True

    # -- reading ------------------------------------------------------------------------
    def status(self) -> dict:
        relay = self._relay()
        out = []
        for dev in self.configured():
            wid = dev["worker_id"]
            d = self.state["devices"].get(wid) or {} if wid else {}
            sig = d.get("signals") or {}
            recent = [s for s in self.state["signals"] if s["worker_id"] == wid][-10:]
            row = {"device": dev["name"], "worker_id": wid, "online": dev["online"],
                   "has_attention": dev["attention"],
                   "relay_enabled": bool(wid and relay is not None
                                         and relay.may_relay(self.principal(wid))),
                   "last_poll": _when(d.get("last_poll")),
                   "last_event": ({**d["last_event"], "ts": _when(d["last_event"]["ts"])}
                                  if d.get("last_event") else None),
                   "pending_alerts": len(self.pending(wid)) if wid else
                   len([a for a in self.state["alerts"] if not a.get("expired")]),
                   "open_cards": len([p for p in (d.get("posted") or {}).values()
                                      if not p.get("closed")]),
                   "last_seen": {k: _when(sig.get(k)) for k in SIGNALS if sig.get(k)},
                   "recent_signals": [{**s, "ts": _when(s["ts"])} for s in reversed(recent)]}
            if not wid:
                row["note"] = ("several workers have this name: list its worker id" if
                               dev.get("ambiguous") else "no worker on the band has this name or id")
            out.append(row)
        alerts = [{"id": a["id"], "text": short(a["text"], 140), "urgency": a["urgency"],
                   "created": _when(a["created"]),
                   "delivered": {self._name(w): _when(t) for w, t in (a.get("delivered") or {}).items()},
                   **({"acked": {self._name(w): _when(t) for w, t in a["acked"].items()}}
                      if a.get("acked") else {}),
                   **({"expired": True} if a.get("expired") else {})}
                  for a in self.state["alerts"][-10:]]
        st = self.state.get("remind") or {}
        sp = self.speaker()
        reminders = {"queue": self.queueing(), "on": self.reminding(),
                     "speaker": sp["name"] if sp else None,
                     "last_spoken": _when(st.get("spoken_at")),
                     "last_try": _when(st.get("attempt_at")),
                     **({"last_outcome": st["last_outcome"]} if st.get("last_outcome") else {}),
                     **({"waiting_on_phone": True} if st.get("inflight") else {}),
                     **({"popping_up": "the phone can't remind, so cards pop up again"}
                        if st.get("fallback") else {}),
                     "quiet_hours_now": self.quiet_hours()}
        if not sp and reminders["on"]:
            reminders["note"] = ("no reachable phone has attention.remind (app 0.4.22-beta.3 or "
                                 "later): cards pop up as before and nothing is spoken")
        return {"person": self.person(), "devices": out, "recent_alerts": list(reversed(alerts)),
                "reminders": reminders,
                "note": "No answer does not mean unseen: last_seen and recent_signals say whether "
                        "the person saw, opened or unlocked the phone."}

    def _name(self, wid: str) -> str:
        return str((self.roster().get(wid) or {}).get("name") or wid)

    def notices(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit or 20), RING_MAX))
        return [{**n, "ts": _when(n["ts"])} for n in reversed(self.state["notices"][-limit:])]

    def relay_rows(self) -> list[tuple[str, str]]:
        """``(principal, label)`` of every configured phone on the band, for the
        Tokens page's "May relay my approvals"."""
        return [(self.principal(d["worker_id"]), self.label(d)) for d in self.configured()
                if d["worker_id"]]


def parse_hours(spec: str) -> tuple[int, int] | None:
    """``"22:00-07:00"`` as minutes after midnight ``(1320, 420)``; None when empty
    or not that shape (an unreadable setting never silences anything)."""
    import re
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(spec or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    start, end = h1 * 60 + m1, min(h2 * 60 + m2, 1440)
    return None if start == end else (start, end)


def _when(ts: Any) -> dict | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return {"at": iso(ts), "ago_s": max(0, int(time.time() - ts))}
