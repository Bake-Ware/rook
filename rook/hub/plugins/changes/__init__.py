"""``change.*``: change requests (worker ``rook``; :mod:`.model`, :mod:`.runner`;
knowledge change-management-design-20261025, docs/manual/trust.md).

Reads, collecting, audits and routine job runs need no change request: the
journal is their record. A change that could hurt is one CR:

* ``change.submit {data}`` (write): standard ones are checked, run, tested
  and kept or rolled back by the hub; an emergency's standard fix runs at
  once (one FYI to the person), any other fix is one urgent card; a normal
  one gets one huddle round to become standard changes, then one card.
* ``change.reduce {id, into}`` / ``change.irreducible {id, why}`` (write): a
  normal CR's huddle round ends.
* ``change.update {id, data}`` (write): an emergency's what, why and plan, written
  afterwards; what ran (scripts, tests, rollback, machines) never changes.
* ``change.revert {id}`` (write): roll a kept change back ("revert that").
* ``change.catalog {action: list|add, category, words}``: the kinds that go as
  standard. Only the person adds one ("that kind is fine"), by voice or chat
  through the house agent with their words; nothing removes one but them.
* ``change.decide {id, yes}`` (write): a person's answer, without the card.
* ``change.get {id}``, ``change.list {states?, days?}``, ``change.digest``.

The digest (setting ``digest``): what ran and what rolled back, each morning
(daily, the default: agents work overnight) and a weekly review on Mondays,
as a quiet phone notification with a copy to the chat bridges, and every
result as one line in the 30-day log (``attention.log``).
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import logging
import time
from typing import Any

from ....core.plugin import Plugin, capability, place, setting
from .model import CATALOG, OPEN, ChangeError, Store, checklist, clean, missing, stops
from .runner import Runner

log = logging.getLogger("rook.hub.plugins.changes")

#: How long a huddle has to reduce a normal change before it goes to the person.
REDUCE_S = 3600.0
DIGEST_HOUR = 8
LOOP_S = 60.0
OUTCOME = {"kept": "kept", "rolled_back": "rolled back", "failed": "FAILED",
           "refused": "not run", "reverted": "reverted", "declined": "declined"}
REDUCE_MOVES = ("soft delete or quarantine now, a small delete change later", "narrow the scope",
                "stage it (a test hub or one worker first)", "time-box it with an auto-revert",
                "snapshot first")


def _caller():
    from ...authz import current_principal
    return current_principal.get()


def _who(p: Any) -> str:
    return str(getattr(p, "label", "") or getattr(p, "id", "") or "unknown")


def _person(p: Any) -> bool:
    return p is not None and getattr(p, "kind", "") == "human" and getattr(p, "verified", True)


class Changes(Plugin):
    NAMESPACE = "change"
    NAME = "changes"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("digest", str, "daily", choices=("daily", "weekly", "off"), group="Changes",
                label="Change digest",
                help="daily: each morning, what changed overnight and what rolled back, plus a "
                     "weekly review on Mondays. weekly: the review only. Sent as a quiet phone "
                     "notification with a copy to your chat bridges; reply \"revert <name>\" or "
                     "\"that kind is fine\"."),
    )
    SKILL = ("### change requests\n"
             "A change that could hurt is a change request: `rook_call(worker=\"rook\", "
             "cap=\"change.submit\", args={\"data\": {kind, "
             "category, what, why, machines, plan, scripts:[{worker, run, detach?}], "
             "tests:[{worker, run, expect_rc?, expect?}], rollback:{worker, run, dry_run, "
             "point}}})`. standard (routine deploys, reversible config or database changes): the "
             "hub checks it, runs it, tests it and rolls back by itself. emergency (a service is "
             "down): fix standard-ly (rollback, restart, failover) and it runs at once; write the "
             "rest with `change.update` afterwards. normal (risky or permanent): the huddle gets one "
             "round to reduce it to standard changes (soft delete, narrow scope, stage, time-box, "
             "snapshot first; `change.reduce` / `change.irreducible`), then the person gets one "
             "card. `change.get`, `change.list`. Reads and routine job runs need no change "
             "request.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node = None
        self._store: Store | None = None
        self.runner: Runner | None = None
        self._task: asyncio.Task | None = None

    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(str(self.data_dir / "changes.db"))
        return self._store

    def _runner(self) -> Runner:
        if self.runner is None:
            self.runner = Runner(self.store, lambda: self._node, self._told)
        return self.runner

    async def start(self) -> None:
        self._runner().resume()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.runner is not None:
            await self.runner.stop()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("changes: tick failed")

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for row in self.store.list(("reducing",), limit=100):
            if now >= float(row["record"].get("deadline") or 0):
                await self._ask(row, "the huddle's round ended without reducing it")
        await self._digest_due(now)

    # -- plumbing ----------------------------------------------------------------
    def _plugin(self, ns: str) -> Any:
        node = self._node
        return node.plugin(ns) if node is not None and hasattr(node, "plugin") else None

    def _resolve(self, worker: str) -> tuple[str, str] | None:
        """``(worker id, name)`` of a machine on the band, by name or id."""
        from ..jobs.kinds import resolve_worker
        client = getattr(self._node, "client", None)
        try:
            return resolve_worker(dict(getattr(client, "workers", {}) or {}), worker,
                                  "shell.exec", getattr(self._node, "worker_id", ""))
        except ValueError:
            return None

    def _sacred(self, worker: str) -> bool:
        """The trust map keeps ``worker`` for the person, by its band name and
        id. A host only reached over ssh (not on the band) is checked by the
        name used; an IP or an ssh alias Rook doesn't know is not recognised."""
        from ..trust import node_store
        st = node_store(self._node)
        if st is None:
            return False
        found = self._resolve(worker)
        try:
            if found is None:
                return st.rule("shell.exec", "exec", (), worker=worker).tier == "person"
            return st.rule("shell.exec", "exec", (), worker=found[1],
                           worker_id=found[0]).tier == "person"
        except Exception:  # noqa: BLE001 - unsure is sacred
            return True

    def _canonical(self, cr: dict) -> dict:
        """Machines and step workers by their band name (an id or another
        spelling would slip past the trust map); refused when one isn't on the band."""
        names = {}
        for m in cr["machines"]:
            found = self._resolve(m)
            if found is None:
                raise ChangeError(f"machines: {m} is not on the band")
            names[m] = found[1]
        cr["machines"] = list(dict.fromkeys(names.values()))
        for s in cr["scripts"] + cr["tests"] + ([cr["rollback"]] if cr["rollback"] else []):
            s["worker"] = names[s["worker"]]
        return cr

    def _refusal(self, p: Any, machines: list[str]) -> str:
        """Why the author may not run commands on one of ``machines`` ("" when it may)."""
        authz = getattr(getattr(self._node, "client", None), "authz", None)
        roster = dict(getattr(getattr(self._node, "client", None), "workers", {}) or {})
        if authz is None or p is None or _person(p):
            return ""
        for m in machines:
            wid = next((k for k, e in roster.items() if k == m or str(e.get("name") or "")
                        .lower() == m.lower()), None)
            if wid is None:
                return f"{m} is not on the band"
            if authz.check("shell.exec", wid, roster[wid], principal=p).denied:
                return f"you may not run commands on {m}"
        return ""

    def _journal(self, cap: str, args: dict, reply: dict) -> None:
        j = getattr(self._node, "journal", None)
        with contextlib.suppress(Exception):
            if j is not None:
                j.record(cap=cap, worker="rook", identity="system:changes", args=args, reply=reply)

    async def _alert(self, text: str, title: str, urgent: bool = False) -> None:
        att = self._plugin("attention")
        with contextlib.suppress(Exception):
            if att is not None:
                await att.service.send(text, title=title, urgency="urgent" if urgent else "normal",
                                       channel="urgent" if urgent else "digest",
                                       source="changes")
        notify = self._plugin("notify")
        with contextlib.suppress(Exception):
            if notify is not None:
                await notify.send(f"{title}: {text}")

    def _told(self, row: dict, outcome: str, line: str) -> None:
        cr = row["record"]
        what = f"{row['id']}: {cr['what']}"
        att = self._plugin("attention")
        if att is not None:
            att.log("change", "ask_now" if outcome == "failed" else "tell",
                    f"Change {OUTCOME.get(outcome, outcome)}: {what}", cr.get("why") or "",
                    f"{cr['kind']} change: {line}", row["id"])
        self._journal("change.run", {"id": row["id"], "kind": cr["kind"]},
                      {"ok": outcome in ("kept", "reverted"), "result": {"state": row["state"],
                                                                         "line": line}})
        loop = asyncio.get_event_loop()
        if outcome == "failed":
            loop.create_task(self._alert(f"{what} {line}. It needs you.", "Change failed",
                                         urgent=True))
        elif cr["kind"] == "emergency" and outcome in ("kept", "rolled_back"):
            loop.create_task(self._alert(f"{what}: {OUTCOME[outcome]} ({line}).",
                                         "Emergency fix"))

    def _public(self, row: dict) -> dict:
        cr = row["record"]
        return {"id": row["id"], "kind": row["kind"], "category": row["category"],
                "state": row["state"], "what": cr["what"], "why": cr.get("why"),
                "machines": cr["machines"], "author": row["author"],
                "phase": row["phase"], "created": row["created"], "updated": row["updated"],
                **({"checks": cr["checks"]} if cr.get("checks") else {}),
                **({"ask": cr["ask"]} if cr.get("ask") else {}),
                "log": row["log"][-12:]}

    # -- the kinds ---------------------------------------------------------------
    async def _ask(self, row: dict, why: str, urgent: bool = False) -> dict:
        """One card to the person with the change request."""
        cr = row["record"]
        home = self._plugin("home")
        front = getattr(home, "front", None) if getattr(home, "enabled", lambda: True)() else None
        line = f"Change {row['id']}: {cr['what']}?"
        explain = (f"{cr.get('why') or ''} {why}. Yes runs it"
                   + (" and rolls back by itself if its tests fail" if cr.get("rollback") else "")
                   + "; No drops it.")
        if front is not None:
            more = json.dumps({k: cr.get(k) for k in ("machines", "plan", "scripts", "tests",
                                                      "rollback")}, default=str)[:3500]
            a = front.make_ask("change", line, explain, more, action={"change": row["id"]})
            cr["ask"] = a.get("name") or a["id"]
        row = self.store.set(row["id"], state="asked", record=cr)
        if urgent or front is None:     # else the card is the one copy
            await self._alert(f"{line} {explain}", "Emergency: needs you" if urgent else
                              "Change needs you", urgent=urgent)
        return row

    async def _reduce(self, row: dict, problems: list[str]) -> dict:
        """A normal change: one huddle round to reduce it to standard changes."""
        cr = row["record"]
        cr["deadline"] = time.time() + REDUCE_S
        hs = getattr(self._plugin("huddle"), "_svc", None)
        text = (f"Change request {row['id']} (normal): {cr['what']}. Why: {cr.get('why')}. "
                f"Machines: {', '.join(cr['machines'])}. Not standard because: "
                f"{'; '.join(problems) or 'its author says so'}. One round: make it standard "
                f"changes ({'; '.join(REDUCE_MOVES)}) with change.reduce {{id: {row['id']}, "
                f"into: [...]}} on worker rook, or say why it can't be (change.irreducible). Reduce the risk; "
                f"this is not a vote. In an hour it goes to the person as it is.")
        try:
            svc = hs() if hs is not None else None
        except Exception:  # noqa: BLE001 - huddles are off
            svc = None
        if svc is None:
            return await self._ask(row, "nobody is here to reduce it")
        try:
            if cr.get("huddle"):
                out = await svc.continue_episode(cr["huddle"], text, "agent:changes")
            else:
                out = await svc.open_episode(f"Change {row['id']}", None, text, "agent:changes")
            cr["huddle"] = out.get("room") or cr.get("huddle")
        except Exception as e:  # noqa: BLE001 - no room for the round: the person gets it
            return await self._ask(row, f"no huddle could take it ({str(e)[:120]})")
        return self.store.set(row["id"], state="reducing", record=cr)

    # -- caps ----------------------------------------------------------------------
    @capability("submit", risk="write", scope="multi-user")
    async def submit(self, data: dict | None = None) -> dict:
        """A change request: data {kind standard|emergency|normal, category, what, why, machines, plan, scripts [{worker, run, timeout?, detach?}], tests [{worker, run, expect_rc?, expect?}], rollback {worker, run, dry_run, point}, fix? (emergency: rollback|restart|failover), huddle?, task?}."""
        p = _caller()
        cr = self._canonical(clean(data or {}))
        refusal = self._refusal(p, cr["machines"])
        if refusal:
            raise PermissionError(refusal)
        need = missing(cr)
        if cr["kind"] == "standard" and need or cr["kind"] != "standard" and \
                {"machines", "scripts"} & set(need):
            raise ChangeError(f"{cr['kind']} change request without {', '.join(need)}")
        problems = checklist(cr, catalog=self.store.catalog(), sacred=self._sacred)
        cr["checks"] = problems
        row = self.store.add(cr, _who(p), "checking")
        self._journal("change.submit", {"id": row["id"], "kind": cr["kind"]},
                      {"ok": True, "result": {"checks": problems}})
        if cr["kind"] == "emergency":
            if problems:
                row = await self._ask(row, "the fix is not a standard one: " + "; ".join(problems),
                                      urgent=True)
            else:
                self._runner().start(row["id"])
        elif cr["kind"] == "standard" and not problems:
            self._runner().start(row["id"])
        else:
            if cr["kind"] == "standard":
                cr["kind"] = "normal"
                self.store.set(row["id"], kind="normal", record=cr)
                row = self.store.get(row["id"])
            row = await self._reduce(row, problems)
        return {**self._public(self.store.get(row["id"])),
                "note": {"checking": "Checking, then it runs and tests itself; the digest says "
                                     "how it went.",
                         "running": "Running.", "reducing": "One huddle round to make it "
                                                            "standard, then the person.",
                         "asked": "It waits for the person's card."}.get(
                    self.store.get(row["id"])["state"], "")}

    @capability("get", risk="read", scope="multi-user")
    def get(self, id: str) -> dict:
        """One change request with its checks and its step log."""
        return self._public(self.store.get(id))

    @capability("list", risk="read", scope="multi-user")
    def list_(self, states: list | None = None, days: float = 7) -> dict:
        """Change requests updated in the last ``days``, newest first (states: filter)."""
        rows = self.store.list(tuple(states or ()), since=time.time() - float(days) * 86400)
        return {"count": len(rows), "changes": [
            {k: v for k, v in self._public(r).items() if k != "log"} for r in rows]}

    @capability("reduce", risk="write", scope="multi-user")
    def reduce(self, id: str, into: list | None = None, note: str = "") -> dict:
        """A normal change made into standard change requests (their ids): it is closed."""
        row = self.store.get(id)
        if row["state"] != "reducing":
            raise ChangeError(f"{row['id']} is {row['state']}: only the huddle's round reduces "
                              "it (a card waiting for the person is theirs)")
        parts = [self.store.get(i) for i in into or []]
        if not parts or any(r["kind"] != "standard" for r in parts):
            raise ChangeError("into: the standard change requests that replace it")
        cr = row["record"]
        cr["reduced_into"] = [r["id"] for r in parts]
        cr["reduced_note"] = " ".join(str(note or "").split())[:500]
        return self._public(self.store.set(row["id"], state="reduced", record=cr))

    @capability("irreducible", risk="write", scope="multi-user")
    async def irreducible(self, id: str, why: str = "") -> dict:
        """A normal change the huddle can't make standard: one card to the person now."""
        row = self.store.get(id)
        if row["state"] != "reducing":
            raise ChangeError(f"{row['id']} is {row['state']}")
        return self._public(await self._ask(row, " ".join(str(why or "").split())[:300]
                                            or "it can't be made standard"))

    @capability("update", risk="write", scope="multi-user")
    def update(self, id: str, data: dict | None = None) -> dict:
        """Write the words of a change request afterwards (an emergency's what, why, plan); what ran never changes."""
        row = self.store.get(id)
        p = _caller()
        if not _person(p) and _who(p) != row["author"]:
            raise PermissionError("only its author or the person completes a change request")
        if row["state"] in OPEN or row["state"] == "running":
            raise ChangeError(f"{row['id']} is {row['state']}: the record is completed after it "
                              "ran (what was checked or approved doesn't change)")
        cr = row["record"]
        data = {k: v for k, v in (data or {}).items() if k in ("what", "why", "plan")}
        new = {**cr, **{k: v for k, v in clean({**cr, **data}).items()
                        if k in ("what", "why", "plan")}}
        # Only the words are written afterwards: what ran (scripts, tests,
        # rollback, machines) stays exactly what was checked, empty included.
        return self._public(self.store.set(row["id"], record=new))

    @capability("revert", risk="write", scope="multi-user")
    async def revert(self, id: str) -> dict:
        """Roll a kept change back with its own rollback (the person's "revert that")."""
        row = self.store.get(id)
        if row["state"] != "kept":
            raise ChangeError(f"{row['id']} is {row['state']}, not kept")
        cr = row["record"]
        if not (cr.get("rollback") or {}).get("run"):
            raise ChangeError(f"{row['id']} has no rollback to run")
        # The rollback runs as the hub: checked again, as at submit.
        rb = {**cr, "scripts": [], "tests": []}
        problems = stops(rb, self._sacred)
        if problems:
            raise ChangeError(f"{row['id']}'s rollback needs the person: " + "; ".join(problems))
        return self._public(await self._runner().revert(row["id"]))

    @capability("catalog", risk="write", scope="multi-user")
    def catalog(self, action: str = "list", category: str = "", words: str = "") -> dict:
        """The kinds that go as standard changes: list, or add (the person's "that kind is fine", with their words)."""
        if action == "list":
            return {"catalog": self.store.catalog(), "builtin": list(CATALOG)}
        if action != "add":
            raise ChangeError("action: list or add")
        p = _caller()
        words = " ".join(str(words or "").split())[:500]
        if not (_person(p) or self._house(p)) or len(words) < 2:
            raise PermissionError("only the person makes a kind standard (their words, through "
                                  "the house agent or the dashboard); agents never do")
        category = str(category or "").strip().lower()[:40]
        if not category:
            raise ChangeError("category: the kind of change")
        self.store.add_category(category, _who(p), words)
        att = self._plugin("attention")
        if att is not None:
            att.log("change", "decided", f"{category} changes now go as standard", words,
                    f"by {_who(p)}")
        return {"catalog": self.store.catalog()}

    @staticmethod
    def _house(p: Any) -> bool:
        try:
            from ....band_mcp import attribution
            att = attribution.current.get()
        except Exception:  # noqa: BLE001
            return False
        aid = str(getattr(att, "agent_id", "") or "")
        return getattr(att, "kind", "") == "home" and aid.startswith("home:") and \
            "/role:" not in aid

    @capability("decide", risk="write", scope="multi-user")
    async def decide(self, id: str, yes: bool = False) -> dict:
        """A person's answer to a change request that waits for them (the card does the same)."""
        p = _caller()
        if not _person(p):
            raise PermissionError("only the person decides; their card or the dashboard")
        ask, front = self.store.get(id)["record"].get("ask"), getattr(self._plugin("home"), "front", None)
        try:                                    # one record: the card closes, the change runs
            if ask and front is not None:
                return await front.answer(ask, "yes" if yes else "no", by=_who(p))
        except Exception:  # noqa: BLE001 - the card is gone (expired): decide the change itself
            log.info("changes: %s's card %s could not be answered", id, ask, exc_info=True)
        return await self.decided(id, bool(yes), _who(p))

    async def decided(self, ident: str, yes: bool, by: str) -> dict:
        """The person's Yes or No (their card, through the house agent)."""
        row = self.store.get(ident)
        if row["state"] not in ("asked", "reducing"):
            return {"ok": False, "note": f"{row['id']} is already {row['state']}."}
        self.store.note(row["id"], {"phase": "decided", "by": by, "yes": yes})
        if not yes:
            self.store.set(row["id"], state="declined")
            return {"ok": True, "note": "Dropped; nothing ran."}
        self.store.set(row["id"], state="running")
        self._runner().start(row["id"])
        return {"ok": True, "note": f"Running {row['id']}; the digest says how it went."}

    # -- the digest ------------------------------------------------------------------
    def _zone(self) -> Any:
        from zoneinfo import ZoneInfo
        from ..jobs.cron import DEFAULT_TZ
        jobs = self._plugin("job")
        name = str((getattr(jobs, "settings", None) or {}).get("timezone") or DEFAULT_TZ)
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return ZoneInfo(DEFAULT_TZ)

    def digest_text(self, since: float, weekly: bool = False) -> str:
        rows = [r for r in self.store.list(since=since, limit=200) if r["state"] not in OPEN
                or r["state"] == "asked"]
        if not rows:
            return ""
        count: dict[str, int] = {}
        for r in rows:
            count[r["state"]] = count.get(r["state"], 0) + 1
        head = ("Change review, last 7 days: " if weekly else "Changes since yesterday: ") + \
            ", ".join(f"{n} {OUTCOME.get(s, s.replace('_', ' '))}" for s, n in count.items())
        shown = rows if not weekly else [r for r in rows if r["state"] != "kept"]
        lines = [f"- {r['id']} ({r['kind']}, {r['category'] or 'no kind'}): "
                 f"{r['record']['what'][:120]}: {OUTCOME.get(r['state'], r['state'])}"
                 for r in shown[:25]]
        tail = "Reply \"revert <name>\" to undo one, or \"<kind> is fine\" to make a kind standard."
        return "\n".join([head + "."] + lines + [tail])

    async def _digest_due(self, now: float) -> None:
        mode = str((self.settings or {}).get("digest") or "daily")
        if mode == "off":
            return
        local = _dt.datetime.fromtimestamp(now, self._zone())
        if local.hour < DIGEST_HOUR:
            return
        today = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0).timestamp()
        due = []
        if mode == "daily" and self.store.digest_at("daily") < today:
            due.append(("daily", max(self.store.digest_at("daily"), now - 86400)))
        monday = today - local.weekday() * 86400
        if self.store.digest_at("weekly") < monday and local.weekday() == 0:
            due.append(("weekly", now - 7 * 86400))
        for kind, since in due:
            self.store.set_digest(kind, now)
            text = self.digest_text(since, weekly=kind == "weekly")
            if text:
                await self._alert(text, "Change review" if kind == "weekly" else "Changes")
                att = self._plugin("attention")
                if att is not None:
                    att.log("change", "tell", text.split("\n", 1)[0], "", kind)

    @capability("digest", risk="read", scope="multi-user")
    def digest(self, weekly: bool = False) -> dict:
        """What the next digest would say (the last day, or the last 7 days)."""
        return {"text": self.digest_text(time.time() - (7 if weekly else 1) * 86400, weekly)}


PLUGIN = Changes
