"""Change records on the hub (castling-changes; plugin.yaml says what one is).

One runtime per hub (``_RT``), made by the ``start`` hook.

``change.submit`` checks the record against the ceremony its project's trust
level asks for (:func:`ceremony`) and that its machines are on the band and
none is sacred, then:

- no scripts: the record is the change (``noted``);
- standard: runs it now (:meth:`Runtime.run`): checks, scripts, tests. A
  failed check changes nothing (``refused``); a failed script or test runs
  the rollback (``rolled_back``, or ``failed`` when that breaks too or there
  is none). A script that never ran (dispatch refused it) or is ``atomic``
  (it undoes itself, like ``core.plugin.install``) changes nothing when it is
  the first to fail.
- risky, or a sacred project: ``asked``. The plugin calls ``change.apply``
  as itself; apply is a hard stop, so dispatch holds it and an owner gets
  the card. Approving runs that call, which starts the change; refusing ends
  it as ``declined`` (the ``hold.decided`` hook).

Every step is a cap call through dispatch as the change's author (the person,
or the agent acting for them), with the change's why and id, on the worker
the step names; the plugin's own rights are never lent to it. A step that
comes back held stops the change where it is: that call may still run once
someone answers it, so nothing is rolled back under it.

After a hub restart (holds live in memory, runs in tasks): an ``asked``
change is asked again, and a ``running`` one is rolled back when a script
had run (it may have stopped halfway), else ``refused``.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from dataclasses import replace

from rook.kit._types import parse_duration
from rook.kit.ctx import Caller, KitError

log = logging.getLogger("rook.plugin.change")

ME = "system:plugin.change"        # the plugin at dispatch: the only caller change.apply runs for
STEPS_MAX = 20
TEXT_MIN = 8                       # risk and plan: a real sentence, not "n/a"
RECORD_MAX = 64_000                # bytes of a whole record's steps
RESULT_MAX = 4000                  # bytes of a step's result kept in the log
LOG_MAX = 200
WHY_MAX = 500                      # spec: a why is at most 500 characters
RETRY_EVERY, RETRY_FOR = 0.5, 60.0  # after a start: the hub serves our caps once its start-up ends
POLL = 2.0                         # how often a step looks for its worker to come back
TERMINAL = ("noted", "kept", "rolled_back", "failed", "refused", "declined", "reverted")
STEP_KEYS = {"worker", "cap", "args"}
_CAP = re.compile(r"[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+")
_HOLD = re.compile(r"\(hold (h_[0-9a-f]+)\)")
# Errors dispatch gives before a call reaches its worker: the call never ran.
_NEVER_RAN = ("denied", "unknown worker", "unknown capability", "unknown appliance", "bad args",
              "bad request", "no single worker", "held:", "secret:")


# -- checking a record -------------------------------------------------------------

def _bad(msg: str) -> KitError:
    return KitError(f"bad args: {msg}")


def _text(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad(f"{field} is text")
    return " ".join(value.split()) if field not in ("plan",) else value.strip()


def _step(raw, field: str, kind: str) -> dict:
    keys = STEP_KEYS | ({"atomic"} if kind == "script" else {"expect"} if kind == "test" else set())
    if not isinstance(raw, dict):
        raise _bad(f"{field}: {{worker, cap, args?}}")
    extra = sorted(set(raw) - keys)
    if extra:
        raise _bad(f"{field}: unknown {', '.join(extra)} (a step is {', '.join(sorted(keys))})")
    worker, cap, args = raw.get("worker"), raw.get("cap"), raw.get("args")
    if not (isinstance(worker, str) and 0 < len(worker.strip()) <= 128):
        raise _bad(f"{field}.worker: the worker it runs on")
    if not (isinstance(cap, str) and _CAP.fullmatch(cap)):
        raise _bad(f"{field}.cap: a cap name such as shell.exec")
    if args is not None and not isinstance(args, dict):
        raise _bad(f"{field}.args: a map")
    out = {"worker": worker.strip(), "cap": cap, "args": copy.deepcopy(args or {})}
    if raw.get("atomic"):
        out["atomic"] = True
    if raw.get("expect") is not None:
        if not isinstance(raw["expect"], str) or not raw["expect"] or len(raw["expect"]) > 500:
            raise _bad(f"{field}.expect: text the result must contain (at most 500 characters)")
        out["expect"] = raw["expect"]
    return out


def clean(args: dict) -> dict:
    """The record's fields from submit's args; raises KitError."""
    out = {"what": _text(args.get("what"), "what"), "why": _text(args.get("why"), "why"),
           "kind": args.get("kind") or "standard"}
    if not out["what"] or not out["why"]:
        raise _bad("what and why say what changes and why")
    if len(out["why"]) > WHY_MAX - 40:
        raise _bad(f"why: at most {WHY_MAX - 40} characters (it goes on every step's call)")
    for f in ("category", "project", "task", "risk", "plan"):
        v = _text(args.get(f), f)
        if v:
            out[f] = v
    for f, kind in (("checks", "check"), ("scripts", "script"), ("tests", "test")):
        steps = args.get(f) or []
        if not isinstance(steps, list) or len(steps) > STEPS_MAX:
            raise _bad(f"{f}: a list of at most {STEPS_MAX} steps")
        out[f] = [_step(s, f"{f}[{i}]", kind) for i, s in enumerate(steps)]
    out["rollback"] = _step(args["rollback"], "rollback", "rollback") if args.get("rollback") else None
    every = out["checks"] + out["scripts"] + out["tests"] + ([out["rollback"]] if out["rollback"] else [])
    if (out["checks"] or out["tests"] or out["rollback"]) and not out["scripts"]:
        raise _bad("checks, tests and rollback go with scripts")
    if len(json.dumps(every, default=str)) > RECORD_MAX:
        raise _bad(f"the steps are over {RECORD_MAX // 1000} kB")
    out["machines"] = list(dict.fromkeys(s["worker"] for s in every))
    out["caps"] = list(dict.fromkeys(s["cap"] for s in every))
    return out


def ceremony(rec: dict, level: str) -> list[str]:
    """What a record still lacks at ``level`` (empty: enough). ship-it: what
    and why. normal and sacred: also risk and plan; with scripts, tests
    (unless every script is atomic: it checks and undoes itself) and a
    rollback (unless the change is risky: some have no way back)."""
    if level == "ship-it":
        return []
    out = [f for f in ("risk", "plan") if len(rec.get(f) or "") < TEXT_MIN]
    if rec["scripts"]:
        if not rec["tests"] and not all(s.get("atomic") for s in rec["scripts"]):
            out.append("tests")
        if not rec["rollback"] and rec["kind"] != "risky":
            out.append("rollback")
    return out


def _never_ran(error: str) -> bool:
    return error.startswith(_NEVER_RAN)


def _clip(result):
    text = json.dumps(result, default=str)
    return result if len(text) <= RESULT_MAX else {"clipped": text[:RESULT_MAX]}


def _as(ctx, caller: Caller):
    c = copy.copy(ctx)
    c.caller = caller
    return c


# -- the runtime -----------------------------------------------------------------------

_RT: "Runtime | None" = None


def _rt() -> "Runtime":
    if _RT is None:
        raise KitError("change records are not running", retry=True)
    return _RT


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx                                     # the plugin's own (hooks)
        self.band = ctx.band
        self.sys = _as(ctx, Caller(band=ctx.band, owner=True, plugin="change"))
        self.rejoin = parse_duration(ctx.settings["rejoin"])
        self.tasks: set[asyncio.Task] = set()
        self.runs: dict[str, asyncio.Task] = {}

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # -- records ------------------------------------------------------------------------

    def get(self, cid: str) -> dict:
        return self.sys.records.change.get(cid)

    def update(self, cid: str, **fields) -> dict:
        return self.sys.records.change.update(cid, revision=self.get(cid)["revision"], **fields)

    def note(self, cid: str, entry: dict, done: bool = False) -> dict:
        """Log a step as it starts (``ok`` None), then its outcome over that
        line (``done``): a restart can tell a script that started."""
        log_ = self.get(cid)["log"] or []
        if done and log_ and log_[-1].get("ok") is None:
            log_ = log_[:-1]
        return self.update(cid, log=log_[-(LOG_MAX - 1):] + [entry])

    def end(self, cid: str, state: str, outcome: str, **fields) -> dict:
        rec = self.update(cid, state=state, outcome=outcome[:1000], ended=time.time(), **fields)
        log.info("change ended", extra={"fields": {"change": cid, "state": state}})
        if state in ("failed", "rolled_back"):
            self.spawn(self.tell(rec))
        return rec

    def me(self, why: str) -> Caller:
        return Caller(band=self.band, plugin="change", why=why[:WHY_MAX])

    async def tell(self, rec: dict) -> None:
        text = f"{rec['what']}: {rec['state'].replace('_', ' ')}. {rec['outcome']} (change {rec['id']})"
        try:
            await self.ctx.call_as(self.me(f"Tell the owners how change {rec['id']} ended"), "notify.send",
                                   {"title": "A change did not stick", "text": text})
        except KitError as e:
            log.warning("change notice not sent", extra={"fields": {"change": rec["id"], "error": e.message}})

    # -- what a record may be ---------------------------------------------------------------

    async def level(self, project: str | None) -> str:
        """The project's trust level (project.trust); normal without a project
        or a level. A project that doesn't exist is refused."""
        if not project:
            return "normal"
        me = self.me(f"Read project {project}'s trust level for a change record")
        try:
            try:
                p = await self.ctx.call_as(me, "project.get", {"id": project})
            except KitError:
                found = await self.ctx.call_as(me, "project.list", {"where": {"slug": project}, "limit": 1})
                if not found:
                    raise KitError(f"no project {project}") from None
                p = found[0]
            trust = await self.ctx.call_as(me, "project.trust.list", {"where": {"project": p["id"]}, "limit": 1})
        except KitError as e:
            raise KitError(f"project {project}: {e.message}") from None
        return trust[0]["level"] if trust else "normal"

    async def workers(self) -> list[dict]:
        out = await self.ctx.call_as(self.me("Look up the machines a change runs on"), "hub.workers", {})
        return out.get("workers") or [] if isinstance(out, dict) else []

    async def check_machines(self, machines: list[str]) -> None:
        if not machines:
            return
        known = await self.workers()
        for m in machines:
            w = next((w for w in known if m in (w.get("id"), w.get("name"))), None)
            if w is None:
                raise KitError(f"{m} is not on the band")
            if w.get("sacred"):
                raise KitError(f"{m} is sacred: every change there waits for the person, call by call. "
                               "Make it by hand (each call holds for them), or ask them first.")

    async def online(self, worker: str) -> bool:
        """Wait (up to the ``rejoin`` setting) for ``worker`` to be on the band:
        a deploy may restart it, a hub restart drops everyone for a moment."""
        deadline = time.monotonic() + self.rejoin
        while True:
            try:
                if any(worker in (w.get("id"), w.get("name")) and (w.get("online") or w.get("hub"))
                       for w in await self.workers()):
                    return True
            except KitError:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL)

    # -- asking -----------------------------------------------------------------------------

    async def ask(self, rec: dict) -> dict:
        """Hold change.apply for an owner: the one card a risky change gets."""
        cid = rec["id"]
        why = f"Change {rec['what']}: {rec['why']}"
        try:
            await self.ctx.call_as(self.me(why), "change.apply", {"id": cid})
        except KitError as e:
            held = _HOLD.search(e.message)
            if held:
                return self.update(cid, hold=held.group(1))
            if e.retry or e.message.startswith(("unknown capability", "no single worker")):
                raise
            return self.end(cid, "refused", f"nothing changed: the card could not be made ({e.message})")
        return self.get(cid)       # no hold: a hard stop always holds, so only a test hub gets here

    # -- running ----------------------------------------------------------------------------

    def start(self, cid: str, cut: bool = False) -> asyncio.Task:
        live = self.runs.get(cid)
        if live is not None and not live.done():
            return live
        t = self.runs[cid] = self.spawn(self._guarded(cid, cut))
        t.add_done_callback(lambda _: self.runs.pop(cid, None))
        return t

    async def _guarded(self, cid: str, cut: bool) -> None:
        try:
            if cut:
                await self.recover_run(cid)
            else:
                await self.run(cid)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # a broken run says so, never stays open
            log.exception("change run broke", extra={"fields": {"change": cid}})
            self.end(cid, "failed", f"the run broke ({type(e).__name__}: {e})"[:300])

    def author(self, rec: dict) -> Caller:
        """Who the steps run as: the person who submitted it, or the agent
        acting for them (dispatch checks their rights, as they are now)."""
        creator, person = rec.get("creator") or "", rec.get("person")
        if creator.startswith("plugin:") and not person:
            return Caller(band=self.band, plugin=creator[7:])
        agent = creator if creator and creator != person else None
        return Caller(person=person, agent=agent, band=self.band)

    async def step(self, rec: dict, who: Caller, phase: str, i: int, s: dict) -> dict:
        entry = {"phase": phase, "step": i, "worker": s["worker"], "cap": s["cap"], "at": time.time()}
        why = f"{rec['why']} [change {rec['id']}]"
        self.note(rec["id"], {**entry, "ok": None})
        if not await self.online(s["worker"]):
            entry.update(ok=False, ran=False, error=f"{s['worker']} is not on the band "
                                                    f"(waited {self.rejoin:g}s)")
        else:
            try:
                res = await self.ctx.call_as(replace(who, why=why[:WHY_MAX]), s["cap"], s["args"], target=s["worker"])
            except KitError as e:
                held = _HOLD.search(e.message)
                entry.update(ok=False, ran=not _never_ran(e.message), error=e.message[:1000])
                if held:
                    entry["hold"] = held.group(1)
            else:
                ok = not (isinstance(res, dict) and res.get("ok") is False)
                entry.update(ok=ok, ran=True, result=_clip(res))
                if not ok:
                    entry["error"] = "its result says ok: false"
                elif s.get("expect") and s["expect"] not in (res if isinstance(res, str) else json.dumps(res, default=str)):
                    entry.update(ok=False, error=f"the result does not contain {s['expect']!r}")
        self.note(rec["id"], entry, done=True)
        return entry

    async def run(self, cid: str) -> dict:
        rec = self.update(cid, state="running")
        who = self.author(rec)
        for i, s in enumerate(rec["checks"] or []):
            got = await self.step(rec, who, "check", i, s)
            if not got["ok"]:
                return self.end(cid, "refused", f"nothing changed: check {i + 1} on {s['worker']} failed "
                                                f"({got['error']})")
        changed = False
        for i, s in enumerate(rec["scripts"]):
            got = await self.step(rec, who, "script", i, s)
            if got["ok"]:
                changed = True
                continue
            where = f"script {i + 1} on {s['worker']} failed ({got['error']})"
            if got.get("hold"):
                return self.stuck(rec, i, got)
            if not changed and (not got["ran"] or s.get("atomic")):
                return self.end(cid, "refused", f"nothing changed: {where}")
            return await self.rollback(rec, who, where)
        for i, s in enumerate(rec["tests"] or []):
            got = await self.step(rec, who, "test", i, s)
            if not got["ok"]:
                return await self.rollback(rec, who, f"test {i + 1} on {s['worker']} failed ({got['error']})")
        return self.end(cid, "kept", "tests passed" if rec["tests"] else "done")

    def stuck(self, rec: dict, i: int, got: dict) -> dict:
        return self.end(rec["id"], "failed", f"script {i + 1} waits for a person as hold {got['hold']}: the "
                                             "change stopped there and nothing was rolled back, since that call "
                                             "may still run. Answer the hold, then finish or revert by hand.")

    async def rollback(self, rec: dict, who: Caller, why: str) -> dict:
        rb = rec.get("rollback")
        if not rb:
            return self.end(rec["id"], "failed", f"{why}; it has no rollback")
        got = await self.step(rec, who, "rollback", 0, rb)
        if not got["ok"]:
            return self.end(rec["id"], "failed", f"{why}; the rollback failed too ({got['error']})")
        for i, s in enumerate(rec["tests"] or []):     # how things stand now, for the record
            await self.step(rec, who, "after", i, s)
        return self.end(rec["id"], "rolled_back", why)

    # -- after a restart --------------------------------------------------------------------

    async def recover(self) -> None:
        """Ask again for ``asked`` changes (holds don't outlive the hub) and
        settle ``running`` ones a restart cut off."""
        for rec in self.sys.records.change.list(where={"state": "running"}, limit=500):
            self.start(rec["id"], cut=True)
        for rec in self.sys.records.change.list(where={"state": "asked"}, limit=500):
            deadline = time.monotonic() + RETRY_FOR
            while True:
                try:
                    await self.ask(rec)
                    break
                except KitError as e:
                    if time.monotonic() >= deadline:
                        log.warning("change not asked again", extra={"fields": {"change": rec["id"],
                                                                                "error": e.message}})
                        break
                    await asyncio.sleep(RETRY_EVERY)

    async def recover_run(self, cid: str) -> dict:
        rec = self.get(cid)
        if not any(e.get("phase") == "script" for e in rec["log"] or []):
            return self.end(cid, "refused", "nothing changed: a hub restart cut it off before its scripts")
        return await self.rollback(rec, self.author(rec), "a hub restart cut it off while it ran")


# -- hooks ----------------------------------------------------------------------------------

async def start(ctx):
    global _RT
    _RT = Runtime(ctx)
    _RT.spawn(_later(_RT))


async def _later(rt: Runtime) -> None:
    await asyncio.sleep(RETRY_EVERY)     # the hub serves change.* once its start-up is through
    await rt.recover()


async def stop(ctx):
    global _RT
    if _RT is not None:
        rt, _RT = _RT, None
        await rt.close()


async def on_hold(ctx, event, payload):
    """A person answered a change's card: refused ends it; approved ran
    change.apply already, so only who decided is written down."""
    rt = _RT
    if rt is None or not isinstance(payload, dict) or not payload.get("hold"):
        return
    found = rt.sys.records.change.list(where={"hold": payload["hold"]}, limit=1)
    if not found:
        return
    rec, by = found[0], str(payload.get("by") or "")
    if payload.get("decision") == "denied" and rec["state"] == "asked":
        rt.end(rec["id"], "declined", f"declined by {by or 'the person'}", by=by or None)
    elif payload.get("decision") == "approved":
        rt.update(rec["id"], by=by or None)


# -- caps -----------------------------------------------------------------------------------

async def submit(ctx, wait=False, **args):
    rt = _rt()
    fields = clean(args)
    level = await rt.level(fields.get("project"))
    lacks = ceremony(fields, level)
    if lacks:
        raise KitError(f"the record needs {', '.join(lacks)} (the project is {level}): "
                       "write what could break, the plan, how it is tested and how to undo it")
    await rt.check_machines(fields["machines"])
    asks = bool(fields["scripts"]) and (fields["kind"] == "risky" or level == "sacred")
    state = "noted" if not fields["scripts"] else "asked" if asks else "running"
    rec = ctx.records.change.create(**{k: v for k, v in fields.items() if v is not None}, level=level, state=state,
                                    log=[])
    if state == "noted":
        return rt.update(rec["id"], ended=time.time(), outcome="recorded")
    if state == "asked":
        return await rt.ask(rec)
    run = rt.start(rec["id"])
    if wait:
        await asyncio.shield(run)
    return rt.get(rec["id"])


async def apply(ctx, id):
    rt = _rt()
    if ctx.caller.agent != ME or ctx.caller.person:
        raise KitError("only the change plugin applies a change: answer its card instead")
    rec = rt.get(id)
    if rec["state"] != "asked":
        raise KitError(f"change {id} is {rec['state']}, not waiting for a card")
    rt.start(id)
    return {"id": id, "state": "running"}


async def revert(ctx, id):
    rt = _rt()
    rec = ctx.records.change.get(id)
    if not (ctx.caller.owner or (ctx.caller.person and ctx.caller.person == rec["person"])):
        raise KitError("only its author or an owner reverts a change")
    if rec["state"] != "kept":
        raise KitError(f"change {id} is {rec['state']}: only a kept change is reverted")
    if not rec.get("rollback"):
        raise KitError(f"change {id} has no rollback: undo it by hand, with a change record of its own")
    who = Caller(person=ctx.caller.person, agent=ctx.caller.agent, band=rt.band)
    got = await rt.step(rec, who, "revert", 0, rec["rollback"])
    if not got["ok"]:
        raise KitError(f"the rollback failed ({got['error']}); change {id} is still kept")
    return rt.end(id, "reverted", f"reverted by {ctx.caller.actor}", by=ctx.caller.actor)
