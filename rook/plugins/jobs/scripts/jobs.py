"""Jobs on the hub: a trigger fires one call (plugin.yaml says what a job is).

One runtime per hub (``_RT``), made by the ``start`` hook:

- a scheduler loop (every ``tick``) fires ``cron`` and ``every`` jobs. The
  next slot is kept in memory; after a restart it is worked out again from
  the job's last scheduled run, and a slot missed meanwhile runs once
  (``catch_up``) or is skipped.
- the ``on_event`` hook fires ``on`` jobs; ``job.run`` fires any job.
- a run is a ``job.run`` record. Its one call goes through dispatch as the
  job's identity (``ctx.call_as``): its creator (person, or agent for a
  person), ``agent:<persona>``, or the plugin that ships it. A call fired by
  an event gets ``event: {name, payload}`` in its args when its cap takes an
  ``event`` arg.
- one run per job at a time (a trigger that finds it running is skipped).
- a run still open when the hub stops is marked failed at the next start.

Jobs and runs are private records: their creator's person (and owners) see
them. The runtime itself reads every job through an owner-view ctx.
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rook.kit._types import parse_duration
from rook.kit.ctx import Caller, KitError

log = logging.getLogger("rook.plugin.jobs")

TRIGGERS = ("cron", "every", "on", "manual")
SEED_EVERY = 3600.0                 # re-read other plugins' manifest jobs this often (and on worker events)
MAX_RESULT = 64_000
FIELDS = ("name", "doc", "trigger", "call", "worker", "args", "persona", "enabled", "catch_up", "timeout")


# -- cron --------------------------------------------------------------------------

_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}
_NAMES = {**{m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())},
          **{d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}}
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _value(text: str) -> int:
    return _NAMES[text.lower()] if text.lower() in _NAMES else int(text)


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        span, _, step = part.partition("/")
        if span == "*":
            a, b = lo, hi
        elif "-" in span:
            a, b = map(_value, span.split("-", 1))
        else:
            a = _value(span)
            b = hi if step else a
        n = int(step) if step else 1
        if not (lo <= a <= b <= hi) or n < 1:
            raise ValueError(f"{part!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, n))
    return out


class Cron:
    """Five fields: minute hour day-of-month month day-of-week (0 or 7 =
    Sunday), with ``*``, lists, ranges, steps, names and @daily-style
    aliases. Day of month and day of week: either matches when both are
    restricted (classic cron)."""

    def __init__(self, expr: str):
        parts = _ALIASES.get(str(expr).strip().lower(), str(expr)).split()
        if len(parts) != 5:
            raise ValueError("cron takes 5 fields: minute hour day month weekday")
        try:
            self.sets = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
        except (KeyError, ValueError) as e:
            raise ValueError(f"bad cron {expr!r}: {e}") from None
        if 7 in self.sets[4]:
            self.sets[4] = (self.sets[4] - {7}) | {0}
        self.both = parts[2] != "*" and parts[4] != "*"

    def _day(self, t: datetime) -> bool:
        dom, dow = t.day in self.sets[2], t.isoweekday() % 7 in self.sets[4]
        return (dom or dow) if self.both else (dom and dow)

    def next_after(self, ts: float, tz) -> float | None:
        """The first matching minute after ``ts`` (wall clock in ``tz``), or
        None when nothing matches (Feb 30)."""
        t = datetime.fromtimestamp(ts, tz).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        for _ in range(100_000):
            if t.month not in self.sets[3]:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
            elif t.hour not in self.sets[1]:
                t = t.replace(minute=0) + timedelta(hours=1)
            elif t.minute not in self.sets[0]:
                t += timedelta(minutes=1)
            else:
                at = t.replace(tzinfo=tz).timestamp()
                if at > ts:                 # a DST fold can map back before ts
                    return at
                t += timedelta(minutes=1)
        return None


# -- checking a job ------------------------------------------------------------------

def _bad(msg: str) -> KitError:
    return KitError(f"bad args: {msg}")


def trigger_kind(trigger: dict) -> str:
    return next(k for k in trigger if k != "filter")


def check_trigger(t) -> None:
    if not isinstance(t, dict):
        raise _bad("trigger is a map")
    kinds = [k for k in t if k != "filter"]
    if len(kinds) != 1 or kinds[0] not in TRIGGERS:
        raise _bad("a trigger is exactly one of cron, every, on, manual")
    kind, value = kinds[0], t[kinds[0]]
    if "filter" in t and (kind != "on" or not isinstance(t["filter"], dict)):
        raise _bad("filter is a map, and goes with on")
    if kind == "cron":
        try:
            Cron(value)
        except ValueError as e:
            raise _bad(str(e)) from None
    elif kind == "every":
        try:
            if parse_duration(value) < 1:
                raise ValueError
        except ValueError:
            raise _bad("every takes a duration of 1s or more") from None
    elif kind == "on":
        if not (isinstance(value, str) and value):
            raise _bad("on takes an event name or glob")
    elif value is not True:
        raise _bad("manual: true")


def check_job(job: dict, owner: bool) -> None:
    """Raise KitError unless ``job`` (record fields) is runnable and the
    caller may make it: persona jobs and event triggers are for owners
    (events show what happens band-wide)."""
    check_trigger(job.get("trigger"))
    if job.get("timeout"):
        try:
            parse_duration(job["timeout"])
        except ValueError:
            raise _bad("timeout is a duration") from None
    if job.get("run_as") == "persona":
        if not job.get("persona"):
            raise _bad("a persona job names its persona")
        if not owner:
            raise KitError("only an owner makes a job run as a persona")
    if "on" in job["trigger"] and not owner:
        raise KitError("only an owner makes a job that runs on events")


def _fields(args: dict) -> dict:
    """Cap args -> record fields."""
    out = {k: copy.deepcopy(args[k]) for k in FIELDS if args.get(k) is not None}
    if args.get("as") is not None:
        out["run_as"] = args["as"]
    return out


def _matches(flt: dict | None, payload) -> bool:
    for key, want in (flt or {}).items():
        v = payload
        for part in str(key).split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if not (v in want if isinstance(want, list) else v == want):
            return False
    return True


def _as(ctx, caller: Caller):
    c = copy.copy(ctx)
    c.caller = caller
    return c


# -- the runtime -----------------------------------------------------------------------

_RT: "Runtime | None" = None


def _rt() -> "Runtime":
    if _RT is None:
        raise KitError("jobs are not running", retry=True)
    return _RT


class Runtime:
    def __init__(self, ctx):
        self.ctx = ctx                                   # the plugin's own (hooks): seeding
        self.band = ctx.band
        self.sys = _as(ctx, replace(ctx.caller, owner=True))
        s = ctx.settings
        self.tz = _zone(s["timezone"])
        self.tick = max(parse_duration(s["tick"]), 0.05)
        self.timeout = parse_duration(s["timeout"])
        self.next: dict[str, tuple] = {}                 # job id -> (trigger key, due)
        self.running: dict[str, asyncio.Task] = {}       # job id -> its run
        self.tasks: set[asyncio.Task] = set()
        self.seeded = 0.0
        self.closing = False
        self.recover()

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def close(self) -> None:
        self.closing = True
        tasks = list(self.tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def recover(self) -> None:
        """Runs a stopped hub left open, marked failed (before anything new
        starts)."""
        for rec in self.sys.records.run.list(where={"state": "running"}, limit=500):
            self.sys.records.run.update(rec["id"], state="failed", ended=time.time(),
                                        error="interrupted: the hub restarted before it finished")

    # -- reading ---------------------------------------------------------------------

    def jobs(self) -> list[dict]:
        out, off = [], 0
        while True:
            page = self.sys.records.job.list(where={"enabled": True}, limit=500, offset=off, order="created")
            out += page
            if len(page) < 500:
                return out
            off += 500

    def identity(self, job: dict) -> Caller:
        why = f"job {job['name']}"
        if job.get("run_as") == "persona":
            return Caller(agent=f"agent:{job['persona']}", band=self.band, why=why)
        creator = job["creator"] or ""
        if creator.startswith("plugin:"):
            return Caller(plugin=creator[len("plugin:"):], band=self.band, why=why)
        return Caller(person=job["person"], agent=None if creator == job["person"] else creator,
                      band=self.band, why=why)

    @staticmethod
    def who(c: Caller) -> str:
        if c.person:
            return c.person + (f" via {c.agent}" if c.agent else "")
        return c.agent or f"plugin:{c.plugin}"

    # -- the schedule ----------------------------------------------------------------

    async def loop(self) -> None:
        while not self.closing:
            await asyncio.sleep(self.tick)      # first: the rest of the hub's library loads
            try:
                if time.time() - self.seeded >= SEED_EVERY:
                    await self.seed()
                self.schedule(time.time())
            except Exception:
                log.exception("jobs tick failed")

    def schedule(self, now: float) -> None:
        live = set()
        for job in self.jobs():
            kind = trigger_kind(job["trigger"])
            if kind not in ("cron", "every"):
                continue
            jid, key = job["id"], (json.dumps(job["trigger"], sort_keys=True), job["revision"])
            live.add(jid)
            have = self.next.get(jid)
            if have is None or have[0] != key:
                due, missed = self.first_due(job, now)
                self.next[jid] = (key, due)
                if missed is not None and job["catch_up"]:
                    self.fire(job, "catch_up", scheduled=missed)
            elif have[1] is not None and now >= have[1]:
                self.next[jid] = (key, self.after(job, have[1], now))
                self.fire(job, kind, scheduled=have[1])
        for jid in set(self.next) - live:
            del self.next[jid]

    def slot(self, job: dict, t: float) -> float | None:
        trig = job["trigger"]
        if "cron" in trig:
            return Cron(trig["cron"]).next_after(t, self.tz)
        return t + parse_duration(trig["every"])

    def after(self, job: dict, due: float, now: float) -> float | None:
        """The slot after ``due`` that is still ahead of ``now``."""
        if "cron" in job["trigger"]:
            return self.slot(job, max(due, now))
        every = parse_duration(job["trigger"]["every"])
        return due + every * (int((now - due) // every) + 1)

    def first_due(self, job: dict, now: float) -> tuple:
        """(next slot, a slot missed since the last one) for a job the
        scheduler hasn't seen (new, edited, or after a restart)."""
        last = self.sys.records.run.list(where={"job": job["id"]}, order="-scheduled", limit=1)
        base = max([job["updated"], *(r["scheduled"] for r in last if r["scheduled"])])
        due = self.slot(job, base)
        if due is None or due > now:
            return due, None
        return self.after(job, due, now), due

    # -- running -----------------------------------------------------------------------

    def fire(self, job: dict, trigger: str, *, scheduled: float | None = None, event: dict | None = None,
             by: str | None = None) -> str | None:
        """Start a run of ``job`` now: its run id, or None if it is still running."""
        if self.closing:
            return None
        if job["id"] in self.running:
            log.info("job still running: trigger skipped",
                     extra={"fields": {"job": job["name"], "trigger": trigger}})
            return None
        who = self.identity(job)
        rec = _as(self.ctx, who).records.run.create(
            job=job["id"], name=job["name"], trigger=trigger, scheduled=scheduled, by=by,
            as_who=self.who(who), state="running", started=time.time(), event=event)
        task = self.running[job["id"]] = self.spawn(self.execute(job, rec["id"], who, event))
        task.add_done_callback(lambda _: self.running.pop(job["id"], None))
        return rec["id"]

    async def execute(self, job: dict, rid: str, who: Caller, event: dict | None) -> None:
        """The job's one call; a hub stopping meanwhile leaves the run open
        (recover fails it at the next start)."""
        state, result, error = "failed", None, None
        limit = parse_duration(job["timeout"]) if job.get("timeout") else self.timeout
        args = dict(job.get("args") or {})
        try:
            if event and await self.takes_event(job["call"]):
                args["event"] = {"name": event["name"], "payload": event["payload"]}
            result = await asyncio.wait_for(self.ctx.call_as(
                replace(who, why=f"job {job['name']} (run {rid})"), job["call"], args,
                target=job.get("worker") or None), limit)
            state = "ok"
        except asyncio.TimeoutError:
            error = f"timed out after {job.get('timeout') or f'{self.timeout:g}s'}"
        except KitError as e:
            error = e.message
        except Exception as e:
            log.exception("job run failed")
            error = f"{type(e).__name__}: {e}"
        text = json.dumps(result, default=str)
        if len(text) > MAX_RESULT:
            result = {"truncated": text[:MAX_RESULT]}
        self.sys.records.run.update(rid, state=state, ended=time.time(), result=result, error=error)
        log.info("job run", extra={"fields": {"job": job["name"], "run": rid, "state": state}})

    async def takes_event(self, cap: str) -> bool:
        """Whether ``cap`` declares an ``event`` arg (caps refuse args they
        don't declare)."""
        for m in await self.manifests("caps"):
            if cap in m["caps"]:
                return "event" in (m["caps"][cap].get("args") or {})
        return False

    # -- other plugins' jobs ---------------------------------------------------------

    async def manifests(self, section: str = "jobs") -> list[dict]:
        c = _as(self.ctx, replace(self.ctx.caller, why=f"read the plugins' {section}"))
        return (await c.call("hub.manifests", section=section))["manifests"]

    async def seed(self) -> None:
        """Install every plugin's ``enabled: true`` persona job (as that
        plugin); refresh the ones nobody edited when the plugin changes;
        archive the ones a plugin dropped. A person's edits and archives win."""
        self.seeded = time.time()
        offered = {}
        for m in await self.manifests():
            for name, spec in m["jobs"].items():
                if spec.get("enabled") and spec.get("as") == "persona":
                    offered[f"{m['plugin']}/{name}"] = (m, name, spec)
        seeded = [j for archived in (False, True)
                  for j in self.sys.records.job.list(archived=archived, limit=500)
                  if j["source"] and (j["creator"] or "").startswith("plugin:")]
        have = {j["source"]: j for j in seeded}
        for source, (m, name, spec) in offered.items():
            fields = {**template_fields(m, name, spec), "enabled": True}
            j = have.get(source)
            try:
                check_job(fields, owner=True)
                if j is None:
                    if self.name_taken(fields["name"]):
                        fields["name"] = f"{m['plugin']}.{name}"[:64]
                    who = Caller(plugin=m["plugin"], band=self.band)
                    _as(self.ctx, who).records.job.create(**fields)
                elif not j["edited"] and j["source_version"] != fields["source_version"]:
                    fields.pop("name")
                    self.sys.records.job.update(j["id"], revision=j["revision"], **fields)
            except KitError as e:
                log.warning("plugin job not installed", extra={"fields": {"job": source, "error": e.message}})
        for j in seeded:
            if j["source"] not in offered and not j["archived"]:
                self.sys.records.job.archive(j["id"], revision=j["revision"])

    def name_taken(self, name: str, but: str | None = None) -> bool:
        return any(j["id"] != but for archived in (False, True)
                   for j in self.sys.records.job.list(where={"name": name}, archived=archived, limit=2))


def template_fields(m: dict, name: str, spec: dict) -> dict:
    """A manifest job as record fields."""
    out = {"name": name, "doc": spec.get("doc"), "trigger": copy.deepcopy(spec["trigger"]),
           "call": spec["call"], "args": copy.deepcopy(spec.get("args") or {}), "worker": spec.get("worker"),
           "run_as": spec.get("as", "creator"), "persona": spec.get("persona"),
           "catch_up": bool(spec.get("catch_up")), "source": f"{m['plugin']}/{name}",
           "source_version": m["version"]}
    return {k: v for k, v in out.items() if v is not None}


def _zone(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r: schedules use UTC", name)
        return ZoneInfo("UTC")


# -- hooks ------------------------------------------------------------------------------

async def start(ctx):
    global _RT
    _RT = Runtime(ctx)
    _RT.spawn(_RT.loop())


async def stop(ctx):
    global _RT
    if _RT is not None:
        rt, _RT = _RT, None
        await rt.close()


async def on_event(ctx, event, payload):
    rt = _RT
    if rt is None or event.startswith("job."):      # never on our own records: no loops
        return
    if event in ("worker.online", "worker.updated"):
        rt.spawn(rt.seed())
    for job in rt.jobs():
        trig = job["trigger"]
        if "on" in trig and fnmatch.fnmatchcase(event, trig["on"]) and _matches(trig.get("filter"), payload):
            rt.fire(job, "event", event={"name": event, "payload": payload})


# -- caps ---------------------------------------------------------------------------------

def _find(ctx, ident: str) -> dict:
    """A job the caller may see, by id or name."""
    try:
        j = ctx.records.job.get(ident)
    except KitError:
        found = ctx.records.job.list(where={"name": ident}, limit=1)
        if not found:
            raise KitError(f"no job {ident}") from None
        j = found[0]
    if j["archived"]:
        raise KitError(f"job {j['name']} is archived")
    return j


async def create(ctx, **args):
    rt = _rt()
    fields = _fields(args)
    check_job(fields, ctx.caller.owner)
    if rt.name_taken(fields["name"]):
        raise KitError(f"a job is already called {fields['name']}")
    return ctx.records.job.create(**fields)


async def update(ctx, id, revision, **args):
    rt = _rt()
    job = _find(ctx, id)
    changes = _fields(args)
    if changes.get("run_as") == "creator":
        changes["persona"] = None
    check_job({**job, **changes}, ctx.caller.owner)
    if "name" in changes and rt.name_taken(changes["name"], but=job["id"]):
        raise KitError(f"a job is already called {changes['name']}")
    if job["source"]:
        changes["edited"] = True
    return ctx.records.job.update(job["id"], revision=revision, **changes)


async def start_run(ctx, job, wait=False):
    rt = _rt()
    j = _find(ctx, job)
    rid = rt.fire(j, "manual", by=ctx.caller.actor)
    if rid is None:
        raise KitError(f"job {j['name']} is still running", retry=True)
    if wait:
        await asyncio.shield(rt.running[j["id"]])
        return rt.sys.records.run.get(rid)
    return {"run": rid}


async def templates(ctx):
    rt = _rt()
    installed = {j["source"] for j in ctx.records.job.list(limit=500) if j["source"]}
    out = []
    for m in await rt.manifests():
        for name, spec in m["jobs"].items():
            source = f"{m['plugin']}/{name}"
            out.append({"template": source, "doc": spec.get("doc", ""), "trigger": spec["trigger"],
                        "call": spec["call"], "as": spec.get("as", "creator"), "enabled": bool(spec.get("enabled")),
                        "version": m["version"], "installed": source in installed})
    return out


async def install(ctx, template, name=None, enabled=True):
    rt = _rt()
    plugin, _, jname = template.partition("/")
    m = next((m for m in await rt.manifests() if m["plugin"] == plugin), None)
    spec = (m or {}).get("jobs", {}).get(jname)
    if spec is None:
        raise KitError(f"no template {template} (job.templates lists them)")
    fields = {**template_fields(m, jname, spec), "enabled": enabled}
    if name:
        fields["name"] = name
    check_job(fields, ctx.caller.owner)
    if rt.name_taken(fields["name"]):
        raise KitError(f"a job is already called {fields['name']}: give it another name")
    return ctx.records.job.create(**fields)
