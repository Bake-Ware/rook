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
   delivered yet (a phone that was offline gets them when it is back).

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
CARD_TTL = 7 * 24 * 3600       # the phone's own safety net; the hub sends gone sooner
ALERTS_KEPT = 200
ID_MAX, LINE_MAX, EXPLAIN_MAX, SUMMARY_MAX = 64, 140, 600, 140
URGENCIES = ("urgent", "normal")
CHANNELS = ("urgent", "family", "approvals", "digest", "jobs")
SIGNALS = ("seen", "opened", "dismissed", "acked", "unlocked")
PRIORITIES = ("urgent", "family", "normal")
TAPPED = {"approve": "Tapped Yes", "deny": "Tapped No"}


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
    """The ``attention.post`` args for one waiting item (rook/hub/relay.py)."""
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
    created = float(item.get("created") or time.time())
    return {"id": str(item["id"]), "kind": item.get("kind") or "hold",
            "line": short(item.get("line") or f"{who} wants to {what}", LINE_MAX),
            "explain": short(explain, EXPLAIN_MAX), "urgency": "normal",
            "channel": "approvals",
            "actions": ["yes", "no", "explain"] if item.get("relayable", True) else ["explain"],
            "source": short(item.get("name") or "approvals", 64),
            "expires_at": int(created + CARD_TTL)}


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
                 relay: Callable[[], Any], clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._settings, self._client, self._relay, self.clock = settings, client, relay, clock
        self.lock = asyncio.Lock()
        self.state = self._load()

    # -- state ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        for key, empty in (("devices", {}), ("alerts", []), ("signals", []), ("notices", [])):
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
            self._ring("signals", {"worker_id": wid, "device": dev["name"], "type": kind,
                                   **({"id": ident} if ident else {}), "ts": ts,
                                   "received": self.clock()})
            if kind == "acked" and ident:
                for a in self.state["alerts"]:
                    if a["id"] == ident:
                        a.setdefault("acked", {})[wid] = ts
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
            if old is not None and not again:
                continue
            card = card_for(it)
            try:
                await self.call("attention.post", card, wid)
            except Exception as e:
                log.info("attention: post %s to %s: %s", ident, dev["name"], e)
                continue
            posted[ident] = {"kind": card["kind"], "at": self.clock(),
                             "actions": card["actions"]}
            n_new += 1
        for ident in [i for i in posted if i not in items]:
            row = posted[ident]
            if row.get("closed") or await self._update(wid, ident, "gone",
                                                       "Decided or expired elsewhere."):
                del posted[ident]
                n_gone += 1
        return {"posted": n_new, "gone": n_gone}

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
        return {"person": self.person(), "devices": out, "recent_alerts": list(reversed(alerts)),
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


def _when(ts: Any) -> dict | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return {"at": iso(ts), "ago_s": max(0, int(time.time() - ts))}
