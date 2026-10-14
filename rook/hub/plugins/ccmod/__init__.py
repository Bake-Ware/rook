"""``ccmod.*``: Claude Code rules, set on the hub and pushed to workers.

The operator writes rules on the dashboard (Manage > Claude Code) or with these
caps: when a Claude Code hook event (or Idle, or a worker going offline)
happens in a scope, do something (docs/design/ccmod.md, :mod:`.model`).

* ``ccmod.push`` sends each online worker the rules that apply to it
  (``ccmod.apply`` on the worker, which merges Rook's managed hooks into each
  Claude Code installation's settings.json). Workers without ``ccmod.apply``
  (older builds, or no Claude Code) are skipped and shown as such.
  ``ccmod.preview`` asks a worker for the diff it would write.
* Remote actions run here. The hub long-polls ``ccmod.events`` on each worker
  it pushed rules to, checks each event against its own current rules (the
  worker's list of matched rules is not trusted) and posts, notifies or calls
  an allow-listed capability, at most ``ccmod.rate_per_minute`` times a
  minute per rule and worker. Everything it does is in ``ccmod.activity``.
* Rules that run capabilities (or anything above ``write``) are for hub
  administrators only; so are pushes, which write people's settings files.

Caps on worker ``rook``: ``list``/``get``/``history``/``status``/
``activity``/``preview`` (read), ``set``/``delete`` (write; admin for
capability rules), ``push`` (admin).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from ....worker import ccmod_hook as hook
from . import model
from .model import RuleError

log = logging.getLogger("rook.hub.plugins.ccmod")

TICK = 5.0
POLL_WAIT = 20.0
ONLINE_SECS = 120.0
OPERATOR = "user:operator"
UNSUPPORTED = "This worker cannot take Claude Code rules yet (an older build, or no Claude Code on it)."


def _actor() -> str:
    ident = context.caller_identity.get()
    return str(ident) if ident else "system:rook-hub"


def _admin_gate(what: str) -> None:
    from ...authz import require_hub_admin
    refusal = require_hub_admin(what)
    if refusal:
        raise PermissionError(refusal)


class ClaudeCodeRules(Plugin):
    NAMESPACE = "ccmod"
    NAME = "ccmod"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("cap_allowlist", list, default=[], group="Claude Code",
                label="Capabilities rules may run",
                help="Capability names (or prefix.*) a Claude Code rule may call, e.g. notify.send. "
                     "Empty: rules cannot run capabilities."),
        setting("allow_exec", bool, default=False, group="Claude Code",
                label="Allow rules to run code",
                help="Lets rules call allow-listed capabilities that run code or control a machine "
                     "(exec and admin tier, such as shell.exec). Off by default."),
        setting("rate_per_minute", int, default=6, min=1, max=120, group="Claude Code",
                label="Actions per minute per rule",
                help="How often one rule may act for one worker in a minute; more is dropped "
                     "and logged."),
        setting("listen", bool, default=True, group="Claude Code", label="Listen for events",
                help="Collect hook events from the workers the rules were pushed to, so posts, "
                     "notifications and capabilities run. Off: only the local actions (block, "
                     "instructions, status line) work."),
    )
    SKILL = ("### Claude Code rules\n"
             "Hub-managed triggers and actions for Claude Code on the workers (dashboard: Manage > "
             "Claude Code). `rook_call(worker=\"rook\", cap=\"ccmod.list\")` lists them; "
             "`ccmod.status` shows what each worker holds. Changing or pushing rules writes "
             "people's Claude Code settings: ask the user first.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._store: model.Store | None = None
        self._task: asyncio.Task | None = None
        self._pollers: dict[str, asyncio.Task] = {}
        self._online: dict[str, dict] | None = None
        self._recent: dict[tuple, collections.deque] = {}
        self.clock = time.time

    # -- wiring ------------------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self) -> model.Store:
        if self._store is None:
            self._store = model.Store(self.data_dir / "ccmod.json")
        return self._store

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for t in [self._task, *self._pollers.values()]:
            if t is not None:
                t.cancel()
        self._task = None
        self._pollers.clear()

    def _setting(self, name: str, default: Any) -> Any:
        try:
            v = self.settings.get(name)
        except Exception:
            return default
        return default if v is None else v

    def policy(self) -> model.Policy:
        roster = self.roster(include_hub=True)
        return model.Policy(model.env_list(self._setting("cap_allowlist", [])),
                            bool(self._setting("allow_exec", False)),
                            lambda cap: model.tier_from_roster(roster, cap))

    # -- roster ------------------------------------------------------------------------------
    def roster(self, include_hub: bool = False) -> dict[str, dict]:
        client = getattr(self._node, "client", None)
        hub_id = getattr(self._node, "worker_id", None)
        try:
            workers = dict(client.workers) if client is not None else {}
        except Exception:
            workers = {}
        out = {}
        for wid, w in workers.items():
            if not include_hub and (wid == hub_id or w.get("local")):
                continue
            out[wid] = {**w, "worker_id": wid}
        return out

    def online(self) -> dict[str, dict]:
        now = self.clock()
        out = {}
        for wid, w in self.roster().items():
            seen = w.get("last_seen")
            if seen is None or now - float(seen) < ONLINE_SECS:  # no stamp: listed means live
                out[wid] = w
        return out

    def resolve(self, name: str) -> tuple[str, dict]:
        roster = self.roster()
        if name in roster:
            return name, roster[name]
        hits = [(wid, w) for wid, w in roster.items() if w.get("name") == name]
        if len(hits) != 1:
            raise LookupError(f"worker {name!r} is not online" if not hits
                              else f"more than one online worker is named {name!r}")
        return hits[0]

    @staticmethod
    def supported(entry: dict) -> bool:
        return "ccmod.apply" in (entry.get("caps") or ())

    def config_for(self, entry: dict) -> dict:
        return model.worker_config(self.store.rules(), entry, self.store.rev)

    # -- rule edits (caps and the dashboard share these) ------------------------------------------
    def save_rule(self, rule: Any, actor: str, note: str = "", expect_rev=None,
                  dry_run: bool = False, admin: bool = True) -> dict:
        """Check and save a rule. ``admin`` says whether the caller may save
        rules that run capabilities (the dashboard's operator may)."""
        clean = model.validate(rule, self.policy())
        if model.needs_admin(clean) and not admin:
            raise PermissionError("Only an administrator can save rules that run capabilities")
        out = {"rule": clean, "summary": model.describe(clean)}
        if dry_run:
            return {**out, "dry_run": True}
        new = not (isinstance(rule, dict) and rule.get("id"))
        saved = self.store.save(clean, actor, note, expect_rev, new=new)
        return {**out, "rule": saved}

    def delete_rule(self, rid: str, actor: str, admin: bool = True, note: str = "") -> dict:
        cur = self.store.get(rid)
        if cur is None:
            raise KeyError(f"no rule {rid!r}")
        if model.needs_admin(cur) and not admin:
            raise PermissionError("Only an administrator can delete rules that run capabilities")
        return self.store.delete(rid, actor, note)

    # -- push ---------------------------------------------------------------------------------
    async def _call(self, cap: str, args: dict, target: str, timeout: float, identity: str) -> dict:
        client = getattr(self._node, "client", None)
        if client is None:
            raise ConnectionError("the hub has no band client")
        return await client.call(cap=cap, args=args, target=target, timeout=timeout,
                                 identity=identity)

    async def push_one(self, wid: str, entry: dict, actor: str, remove: bool = False) -> dict:
        name = entry.get("name") or wid[:8]
        if not self.supported(entry):
            row = {"worker": name, "ok": False, "skipped": True, "error": UNSUPPORTED}
            self.store.record_push(wid, {"name": name, "ok": False, "skipped": True, "error": UNSUPPORTED})
            return row
        cfg = {"v": 1, "rev": self.store.rev, "sha": "", "rules": []} if remove else self.config_for(entry)
        try:
            reply = await self._call("ccmod.apply", {"config": cfg}, wid, 30.0, actor)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": "no reply from the worker"}
        except Exception as e:  # noqa: BLE001 - a dead band must not end the loop over workers
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        ok = bool(reply.get("ok")) and bool(res.get("ok", True))
        bad = [i for i in res.get("installations") or () if i.get("action") == "error"]
        row = {"name": name, "ok": ok and not bad, "rev": cfg["rev"], "sha": cfg["sha"],
               "rules": len(cfg["rules"]), "error": reply.get("error") or "; ".join(
                   f"{i.get('path')}: {i.get('error')}" for i in bad) or None,
               "installations": [{k: i.get(k) for k in ("path", "action", "error")}
                                 for i in res.get("installations") or ()],
               "note": res.get("note")}
        self.store.record_push(wid, row)
        return {"worker": name, **row}

    async def push(self, worker: str = "", actor: str = "", remove: bool = False) -> dict:
        targets = [self.resolve(worker)] if worker else sorted(
            self.online().items(), key=lambda kv: str(kv[1].get("name") or kv[0]))
        results = [await self.push_one(wid, e, actor or _actor(), remove) for wid, e in targets]
        self._sync_pollers()
        return {"ok": all(r["ok"] or r.get("skipped") for r in results), "rev": self.store.rev,
                "results": results}

    async def preview(self, worker: str, remove: bool = False) -> dict:
        wid, entry = self.resolve(worker)
        cfg = {"v": 1, "rev": self.store.rev, "sha": "", "rules": []} if remove else self.config_for(entry)
        base = {"worker": entry.get("name") or wid, "worker_id": wid, "config": cfg}
        if not self.supported(entry):
            return {**base, "ok": False, "supported": False, "error": UNSUPPORTED}
        try:
            reply = await self._call("ccmod.apply", {"config": cfg, "dry_run": True}, wid, 20.0,
                                     _actor())
        except asyncio.TimeoutError:
            return {**base, "ok": False, "supported": True, "error": "no reply from the worker"}
        if not reply.get("ok"):
            return {**base, "ok": False, "supported": True, "error": reply.get("error")}
        return {**base, "ok": True, "supported": True, **(reply.get("result") or {})}

    def status_rows(self) -> list[dict]:
        pushes = self.store.pushes()
        online = self.online()
        rows = []
        for wid in sorted(set(online) | set(pushes),
                          key=lambda w: str((online.get(w) or pushes.get(w) or {}).get("name") or w)):
            entry = online.get(wid)
            push = pushes.get(wid) or {}
            row = {"id": wid, "name": (entry or {}).get("name") or push.get("name") or wid[:8],
                   "band": (entry or {}).get("band") or "", "online": entry is not None,
                   "supported": self.supported(entry) if entry else None, "push": push or None}
            if entry is not None:
                cfg = self.config_for(entry)
                row["rules"] = len(cfg["rules"])
                row["current"] = bool(push.get("ok")) and push.get("sha") == cfg["sha"]
            rows.append(row)
        return rows

    # -- events from workers ----------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            try:
                self._watch_presence()
                self._sync_pollers()
            except Exception:
                log.exception("ccmod: presence/poller step failed")
            await asyncio.sleep(TICK)

    def _watch_presence(self) -> None:
        now = self.online()
        before, self._online = self._online, now
        if before is None:
            return  # the first look is the baseline, not a change
        for wid in set(now) - set(before):
            self._spawn(self.handle_event(wid, now[wid], {"event": "rook.worker_online", "ts": self.clock()}))
        for wid in set(before) - set(now):
            self._spawn(self.handle_event(wid, before[wid], {"event": "rook.worker_offline", "ts": self.clock()}))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: t.cancelled() or (t.exception() and log.error(
            "ccmod: action failed", exc_info=t.exception())))

    def _sync_pollers(self) -> None:
        """One long poll per online worker that holds forwarding rules."""
        want = set()
        if self._setting("listen", True):
            pushes = self.store.pushes()
            for wid, entry in self.online().items():
                push = pushes.get(wid) or {}
                if self.supported(entry) and push.get("ok") and push.get("rules"):
                    want.add(wid)
        for wid in list(self._pollers):
            if wid not in want or self._pollers[wid].done():
                self._pollers.pop(wid).cancel()
        for wid in want - set(self._pollers):
            self._pollers[wid] = asyncio.create_task(self._poll(wid))

    async def _poll(self, wid: str) -> None:
        boot, cursor, backoff = "", 0, 2.0
        while True:
            entry = self.online().get(wid)
            if entry is None:
                return
            try:
                reply = await self._call("ccmod.events", {"after": cursor, "wait": POLL_WAIT,
                                                          "boot": boot}, wid, POLL_WAIT + 10, "system:ccmod")
                res = reply.get("result") or {}
                if not reply.get("ok"):
                    raise ConnectionError(reply.get("error") or "refused")
                if res.get("boot") != boot:
                    # A restarted worker numbers events from 1 again; this
                    # first answer is from the new boot, so take it whole.
                    boot = str(res.get("boot") or "")
                for ev in res.get("events") or ():
                    if isinstance(ev, dict):
                        await self.handle_event(wid, entry, ev)
                cursor = int(res.get("cursor") or cursor)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("ccmod: polling %s: %s", wid, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _limited(self, rid: str, wid: str) -> bool:
        now = self.clock()
        q = self._recent.setdefault((rid, wid), collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= int(self._setting("rate_per_minute", 6)):
            return True
        q.append(now)
        return False

    async def handle_event(self, wid: str, entry: dict, ev: dict) -> list[dict]:
        """Run the remote actions of every rule that matches ``ev`` (re-checked
        against the hub's own rules). Returns the activity rows written."""
        event = ev.get("event")
        if event not in model.EVENTS:
            return []
        name = str(entry.get("name") or wid[:8])
        ctx = {"worker": name, "band": str(entry.get("band") or ""), "event": event,
               "minutes": str(ev.get("minutes") or "")}
        for k in ("tool", "project", "cwd", "session", "agent", "prompt", "message"):
            ctx[k] = hook.clip(ev.get(k) or "", 500)
        rows = []
        for rule in self.store.rules():
            if not rule.get("enabled") or not model.applies_to(rule, {**entry, "worker_id": wid}):
                continue
            if event == hook.IDLE:
                hit = rule["trigger"]["event"] == hook.IDLE and rule["id"] in (ev.get("matched") or ()) \
                    and str(rule["trigger"].get("minutes")) == ctx["minutes"] \
                    and hook.in_scope(rule["scope"], ctx)
            elif event in model.ROOK_EVENTS:
                hit = rule["trigger"]["event"] == event
            else:
                # The hook's own matcher, minus the tool input it does not send.
                hit = hook.matches(rule, event, ctx) or (
                    rule["id"] in (ev.get("matched") or ()) and rule["trigger"].get("contains")
                    and event in hook.TOOL_EVENTS and hook.matches(
                        {**rule, "trigger": {k: v for k, v in rule["trigger"].items()
                                             if k != "contains"}}, event, ctx))
            if not hit:
                continue
            actions = [a for a in rule["actions"] if a["type"] in model.REMOTE]
            if not actions:
                continue
            if self._limited(rule["id"], wid):
                row = {"worker": name, "event": event, "rule": rule["id"], "rule_name": rule["name"],
                       "action": "-", "ok": False, "detail": "rate limit: skipped"}
                self.store.log(row)
                rows.append(row)
                continue
            for a in actions:
                try:
                    detail = await self._act(a, rule, wid, name, ctx)
                    ok = True
                except Exception as e:  # noqa: BLE001 - logged for the operator
                    detail, ok = str(e) or type(e).__name__, False
                row = {"worker": name, "event": event, "rule": rule["id"], "rule_name": rule["name"],
                       "action": a["type"], "ok": ok, "detail": str(detail)[:300]}
                self.store.log(row)
                rows.append(row)
        return rows

    async def _invoke(self, cap: str, args: dict, identity: str) -> Any:
        node = self._node
        if node is None or not hasattr(node, "invoke"):
            raise LookupError("no hub node")
        return await node.invoke(cap, args, identity)

    async def _room(self, a: dict, sender: str) -> str:
        if a.get("room"):
            return a["room"]
        title = a["room_title"]
        rid = self.store.room_for(title)
        if rid:
            return rid
        res = await self._invoke("chat.write", {"action": "start", "title": title,
                                                "invite": [OPERATOR]}, sender)
        rid = str(res.get("room") or "")
        if not rid:
            raise ValueError("could not create the room")
        self.store.set_room(title, rid)
        return rid

    async def _act(self, a: dict, rule: dict, wid: str, name: str, ctx: dict) -> str:
        sender = f"claude-code@{name}"
        kind = a["type"]
        if kind == "post":
            text = hook.render(a.get("text") or model.DEFAULT_POST, ctx)
            if a.get("huddle"):
                if self._node is None or self._node.plugin("huddle") is None:
                    raise LookupError("huddles need the huddle plugin on the hub")
                await self._invoke("huddle.write", {"action": "say", "project": a["huddle"],
                                                    "text": text}, sender)
                return f"posted in the {a['huddle']} huddle"
            room = await self._room(a, sender)
            await self._invoke("chat.write", {"action": "send", "room": room, "text": text}, sender)
            return f"posted in room {room}"
        if kind == "notify":
            res = await self._invoke("notify.send", {"text": hook.render(a.get("text"), ctx),
                                                     "channel": a.get("channel") or "all"}, sender)
            if isinstance(res, dict) and not res.get("ok"):
                raise RuntimeError(res.get("error") or "no notification channel took it")
            return "notified via " + ", ".join((res or {}).get("sent") or ["?"])
        if kind == "cap":
            # Policy is checked again at run time: the allow-list or the exec
            # switch may have changed since the rule was saved.
            self.policy().check_cap(a["cap"])
            target = wid if a.get("worker") in ("", "same", None) else self.resolve(a["worker"])[0]
            reply = await self._call(a["cap"], dict(a.get("args") or {}), target, 30.0,
                                     f"ccmod:{rule['id']}")
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or f"{a['cap']} failed")
            return f"{a['cap']} on {self.roster().get(target, {}).get('name') or target}: ok"
        raise ValueError(f"not a remote action: {kind}")

    # -- caps ------------------------------------------------------------------------------------
    @capability("list", risk="read")
    def list_(self) -> dict:
        """Every Claude Code rule, with a one-line summary and its risk tier."""
        return {"rev": self.store.rev,
                "rules": [{**r, "summary": model.describe(r)} for r in self.store.rules()]}

    @capability("get", risk="read")
    def get(self, id: str) -> dict:
        """One rule."""
        r = self.store.get(id)
        if r is None:
            raise KeyError(f"no rule {id!r}")
        return {**r, "summary": model.describe(r)}

    @capability("history", risk="read", limit=20)
    def history(self, id: str = "") -> list[dict]:
        """Attributed changes to the rules, newest first (``id``: one rule)."""
        return self.store.history(id, limit=200)

    @capability("activity", risk="read", limit=50)
    def activity(self) -> list[dict]:
        """What the rules did recently: posts, notifications, capability runs."""
        return self.store.activity(limit=300)

    @capability("status", risk="read")
    def status(self) -> dict:
        """Each worker: whether it can take rules, what was last pushed and
        whether that is still current."""
        return {"rev": self.store.rev, "workers": self.status_rows()}

    @capability("preview", risk="read")
    async def preview_cap(self, worker: str, remove: bool = False) -> dict:
        """What a push would write on ``worker``: per settings file, the action
        and a unified diff (nothing is written)."""
        return await self.preview(worker, remove)

    @capability("set", risk="write")
    def set(self, rule: dict, note: str = "", dry_run: bool = False,
            expect_rev: int | None = None) -> dict:
        """Create or replace a rule. ``rule``: {id?, name, enabled, trigger {event,
        matcher?, contains?, minutes?}, scope {kind, target}, actions [...]}. Rules
        that run capabilities need a hub administrator. ``dry_run`` checks only."""
        checked = model.validate(rule, self.policy())
        if model.needs_admin(checked) and not dry_run:
            _admin_gate(f"ccmod.set {checked['id']} (runs capabilities)")
        return self.save_rule(rule, _actor(), note, expect_rev, dry_run)

    @capability("delete", risk="write")
    def delete(self, id: str, note: str = "") -> dict:
        """Delete a rule (push afterwards to take it off the workers)."""
        cur = self.store.get(id)
        if cur is not None and model.needs_admin(cur):
            _admin_gate(f"ccmod.delete {id}")
        return self.delete_rule(id, _actor(), note=note)

    @capability("push", risk="admin")
    async def push_cap(self, worker: str = "", remove: bool = False) -> dict:
        """Send the rules to ``worker`` (default: every online worker).
        ``remove=true`` takes Rook's hooks off instead. Workers that cannot take
        rules are skipped and say so."""
        _admin_gate(f"ccmod.push {worker or 'all'}")
        return await self.push(worker, _actor(), remove)


__all__ = ["ClaudeCodeRules", "RuleError"]
PLUGIN = ClaudeCodeRules
