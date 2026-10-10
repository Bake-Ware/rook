"""The ``job.read`` / ``job.write`` actions (docs/design/jobs.md 7-8).

Reads: list, get, runs, run_get, next, validate, guardrails_preview,
describe_schema. Writes: create, update, delete, enable, disable, run,
cancel, set_guardrails, settings.

Access (jobs.md 7): a job's ``access.read/edit/run`` hold principal patterns
(``*`` = any authenticated principal, ``token:*``, ``human:<id>``,
``role:operator``, ``human:owner``); new jobs get the hub setting
``job.default_access``. The job's owner and the operator always have full
access. ``list`` leaves out jobs the caller cannot read; ``runs`` /
``run_get`` follow read access.

Editing (update, enable) a job someone else owns hands it to the editor:
its owner becomes the editor and its identity resets to ``creator``, unless
the editor is the owner or the operator. The history row says so.

Times go out as zone-aware ISO strings in the hub zone (setting
``job.timezone``); ``next`` also gives each fire in the trigger's own zone.
Run output is masked again on read, so a secret added after a run was stored
is masked when shown.
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import json
import time
from typing import Any, Callable

from ....band_mcp import secret_mask
from .cron import DEFAULT_TZ, Cron, describe, iso, valid_zone
from .guardrails import (DEFAULT_GUARDRAILS, Guardrails, check_defaults, check_job_block,
                         normalize_defaults)
from .identity import authorize, caller, is_admin, parse_spec
from .kinds import run_notify
from .model import ACCESS_OPS, ValidationError, access_patterns, check, check_access, schema
from .principals import Directory
from .steps import StepContext
from .store import JobStore

READS = ("list", "get", "runs", "run_get", "next", "validate", "guardrails_preview", "describe_schema")
WRITES = ("create", "update", "delete", "enable", "disable", "run", "cancel", "set_guardrails", "settings")
ERRORS = (ValueError, KeyError, TypeError, PermissionError, LookupError)
ALERT_TIMEOUT = 30.0
DEFAULT_ACCESS = {"read": "*", "edit": "*", "run": "*"}


class NotAvailable(ValueError):
    pass


def reply(result: Any) -> str:
    return json.dumps({"ok": True, "result": result}, separators=(",", ":"), ensure_ascii=False, default=str)


def error_reply(error: Exception) -> str:
    out: dict = {"ok": False, "error": str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error),
                 "code": type(error).__name__}
    if isinstance(error, ValidationError):
        out["errors"] = error.errors
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False, default=str)


def route(action: str) -> str:
    return "job.write" if action in WRITES else "job.read"


def matches_principal(pattern: str, who: dict) -> bool:
    """One access pattern against a caller (``owner_info`` shape)."""
    if not who.get("verified", True) or who.get("id") in ("unverified", None) or who.get("kind") == "unverified":
        return False
    if pattern == "*":
        return True
    if pattern.startswith("role:"):
        return who.get("role") == pattern[5:]
    if pattern in (who.get("groups") or ()):
        return True
    return fnmatch.fnmatchcase(str(who.get("id")), pattern)


class JobService:
    def __init__(self, store: JobStore, scheduler: Any, *, settings: Callable[[str], Any],
                 clock: Callable[[], float] = time.time, node: Any = None,
                 setting_names: tuple = (), guardrails: Guardrails | None = None,
                 directory: Callable[[], Directory] | None = None) -> None:
        self.store = store
        self.scheduler = scheduler
        self.settings = settings
        self.clock = clock
        self.node = node
        self.setting_names = setting_names
        self.guardrails = guardrails or Guardrails(settings)
        self._directory = directory or (lambda: Directory.for_node(node))

    def tz(self) -> str:
        tz = self.settings("timezone") or DEFAULT_TZ
        return tz if valid_zone(tz) else DEFAULT_TZ

    def directory(self) -> Directory:
        return self._directory()

    # -- dispatch -------------------------------------------------------------------
    async def dispatch(self, write: bool, action: str, rid: str | None, query: str, data: dict | None) -> Any:
        action = (action or "").strip().lower()
        if write and action not in WRITES:
            raise ValueError(f"{action!r} is not a write; job.write takes {', '.join(WRITES)}"
                             if action in READS else f"Actions: {', '.join(READS + WRITES)}")
        if not write and action not in READS:
            raise ValueError(f"{action!r} is a write; use job.write" if action in WRITES
                             else f"Actions: {', '.join(READS + WRITES)}")
        if data is not None and not isinstance(data, dict):
            raise TypeError("data must be an object")
        fn = getattr(self, "a_" + action)
        out = fn(rid, query or "", data or {})
        if asyncio.iscoroutine(out):
            out = await out
        return out

    def _job(self, ref: str | None) -> dict:
        if not ref:
            raise ValueError("id is required (a job id or name)")
        row = self.store.get_job(ref)
        if row is None:
            raise KeyError(f"no job {ref!r}")
        return row

    # -- access ---------------------------------------------------------------------------
    def can(self, row: dict, op: str, who: dict | None = None) -> bool:
        """Whether ``who`` (default: the caller) may ``op`` (read, edit, run)
        ``row``. The owner and the operator always may."""
        who = who or caller()
        if is_admin(who) or who.get("id") == row["owner"]:
            return True
        pats = access_patterns(((row["definition"] or {}).get("access") or {}).get(op, "*"))
        return any(matches_principal(p, who) for p in pats)

    def need(self, row: dict, op: str, who: dict | None = None) -> None:
        who = who or caller()
        if not self.can(row, op, who):
            raise PermissionError(f"denied: {who.get('id')} may not {op} job {row['name']!r} "
                                  f"(access.{op} of the job)")

    def default_access(self) -> dict:
        v = self.settings("default_access")
        if not isinstance(v, dict) or check_access(v):
            return dict(DEFAULT_ACCESS)
        return {op: v.get(op, "*") for op in ACCESS_OPS}

    # -- views --------------------------------------------------------------------------
    def _roster(self) -> dict:
        return self.guardrails._roster()

    def job_view(self, row: dict, full: bool = False, *, roster: dict | None = None,
                 who: dict | None = None) -> dict:
        tz = self.tz()
        d = row["definition"]
        trigs = self.store.triggers(row["id"])
        upcoming = [t["next_at"] for t in trigs if t["next_at"] is not None and not t["done"]]
        last = self.store.runs(row["id"], limit=1)
        out = {"id": row["id"], "name": row["name"], "description": d.get("description", ""),
               "enabled": row["enabled"], "owner": row["owner"], "revision": row["revision"],
               "triggers": [self._trigger_label(t) for t in d.get("triggers") or []],
               "next": iso(min(upcoming), tz) if upcoming and row["enabled"] else None,
               "last_run": ({"id": last[0]["id"], "state": last[0]["state"],
                             "finished": iso(last[0]["finished"], tz)} if last else None),
               "updated": iso(row["updated"], tz)}
        if row.get("paused_reason"):
            out["paused_reason"] = row["paused_reason"]
        ident = d.get("identity") or {}
        out["identity"] = ident.get("mode") or "creator"
        blocks = self.guardrails.scan(d, roster=roster)
        out["blocked_by_guardrail"] = bool(blocks)
        if blocks:
            out["guardrail_blocks"] = blocks
        who = who or caller()
        out["can"] = {op: self.can(row, op, who) for op in ("edit", "run")}
        if full:
            out["created"] = iso(row["created"], tz)
            out["updated_by"] = row.get("updated_by")
            out["counts"] = self.store.counts(row["id"])
            out["definition"] = d
            out["history"] = [{**h, "at": iso(h["at"], tz)} for h in self.store.history(row["id"])]
        return out

    @staticmethod
    def _trigger_label(t: dict) -> str:
        k = t.get("kind")
        if k == "cron":
            return f"cron {t.get('expr')}" + (f" ({t['tz']})" if t.get("tz") else "")
        if k == "at":
            return f"at {t.get('when')}"
        if k == "after":
            return f"after {t.get('every')} on {t.get('on', 'success')}"
        return str(k)

    def run_view(self, run: dict, steps: bool = True) -> dict:
        tz = self.tz()
        out = {"id": run["id"], "job_id": run["job_id"], "job": run["job_name"], "trigger": run["trigger"],
               "missed": run["missed"], "state": run["state"]}
        for k in ("scheduled", "created", "started", "finished"):
            if run.get(k) is not None:
                out[k] = iso(run[k], tz)
        for k in ("identity_used", "error"):
            if run.get(k):
                out[k] = run[k]
        if run.get("executions"):
            out["executions"] = run["executions"]
        if steps:
            out["steps"] = secret_mask.scrub(self._step_times(run.get("steps") or {}, tz))
            if run.get("vars"):
                out["vars"] = secret_mask.scrub(run["vars"])
            if run.get("alerts"):
                out["alerts"] = secret_mask.scrub(run["alerts"])
        return out

    @staticmethod
    def _step_times(steps: dict, tz: str) -> dict:
        out = {}
        for sid, rec in steps.items():
            rec = dict(rec)
            for k in ("started", "finished"):
                if isinstance(rec.get(k), (int, float)):
                    rec[k] = iso(rec[k], tz)
            out[sid] = rec
        return out

    def _readable(self, who: dict) -> list[dict]:
        return [r for r in self.store.list_jobs() if self.can(r, "read", who)]

    # -- reads ----------------------------------------------------------------------------
    def a_list(self, rid, query, data) -> dict:
        who = caller()
        rows = self._readable(who)
        q = (query or data.get("query") or "").lower()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in (r["definition"].get("description") or "").lower()]
        if "enabled" in data:
            rows = [r for r in rows if r["enabled"] == bool(data["enabled"])]
        roster = self._roster()
        limit = max(1, min(int(data.get("limit") or 50), 500))
        views = [self.job_view(r, roster=roster, who=who) for r in rows]
        if isinstance(data.get("blocked"), bool):
            views = [v for v in views if v["blocked_by_guardrail"] == data["blocked"]]
        return {"jobs": views[:limit], "total": len(views), "timezone": self.tz()}

    def a_get(self, rid, query, data) -> dict:
        row = self._job(rid)
        self.need(row, "read")
        return self.job_view(row, full=True)

    def a_runs(self, rid, query, data) -> dict:
        who = caller()
        jid = None
        if rid:
            row = self._job(rid)
            self.need(row, "read", who)
            jid = row["id"]
        states = data.get("states") or data.get("state") or (query or None)
        if isinstance(states, str):
            states = [s.strip() for s in states.split(",") if s.strip()]
        since = None
        if data.get("since"):
            from .cron import parse_instant
            since = parse_instant(data["since"])
        limit = int(data.get("limit") or 20)
        missed = data.get("missed") if isinstance(data.get("missed"), bool) else None
        if jid is None and not is_admin(who):
            readable = {r["id"] for r in self._readable(who)}
            runs = [r for r in self.store.runs(None, states, 500, since, missed) if r["job_id"] in readable]
            runs = runs[:max(1, min(limit, 500))]
        else:
            runs = self.store.runs(jid, states, limit, since, missed)
        return {"runs": [self.run_view(r, steps=bool(data.get("steps"))) for r in runs]}

    def a_run_get(self, rid, query, data) -> dict:
        if not rid:
            raise ValueError("id is required (a run id)")
        run = self.store.get_run(rid)
        if run is None:
            raise KeyError(f"no run {rid!r}")
        self._need_run_job(run, "read")
        return self.run_view(run)

    def _need_run_job(self, run: dict, op: str) -> None:
        who = caller()
        row = self.store.get_job(run["job_id"])
        if row is None:
            if not is_admin(who):
                raise PermissionError(f"denied: the job of run {run['id']} was deleted (operator only)")
            return
        self.need(row, op, who)

    def a_next(self, rid, query, data) -> dict:
        tz = self.tz()
        count = max(1, min(int(data.get("count") or 5), 20))
        now = self.clock()
        if not rid and isinstance(data.get("trigger"), dict):
            return self._next_draft(data["trigger"], count, now, tz)
        if not rid:
            who = caller()
            readable = {r["id"]: r["name"] for r in self._readable(who)}
            rows = [t for t in self.store.triggers() if t["next_at"] is not None and not t["done"]
                    and t["job_id"] in readable]
            out = [{"job_id": t["job_id"], "job": readable[t["job_id"]], "kind": t["kind"],
                    "at": iso(t["next_at"], tz)} for t in rows[: max(count, 20)]]
            return {"upcoming": out, "timezone": tz}
        row = self._job(rid)
        self.need(row, "read")
        stored = {t["idx"]: t for t in self.store.triggers(row["id"])}
        out = []
        for i, spec in enumerate(row["definition"].get("triggers") or []):
            item: dict = {"trigger": self._trigger_label(spec)}
            ttz = spec.get("tz") or tz
            st = stored.get(i)
            if spec["kind"] == "cron":
                c = Cron(spec["expr"])
                item["reads"] = describe(spec["expr"])
                times, t = [], now
                for _ in range(count):
                    t = c.next_fire(t, ttz)
                    if t is None:
                        break
                    times.append(t)
            elif st is not None and st["next_at"] is not None and not st["done"]:
                times = [st["next_at"]]
            else:
                times = []
            item["zone"] = ttz
            item["next"] = [{"local": iso(t, ttz), "hub": iso(t, tz)} for t in times]
            out.append(item)
        return {"id": row["id"], "name": row["name"], "enabled": row["enabled"], "triggers": out,
                "timezone": tz}

    def _next_draft(self, spec: dict, count: int, now: float, tz: str) -> dict:
        """``next`` for an unsaved trigger (``data.trigger``): the editor's
        cron helper previews fire times before the job is saved."""
        ttz = spec.get("tz") or tz
        if not valid_zone(ttz):
            raise ValueError(f"unknown time zone {ttz!r}")
        item: dict = {"trigger": self._trigger_label(spec), "zone": ttz, "timezone": tz}
        times: list = []
        if spec.get("kind") == "cron":
            c = Cron(str(spec.get("expr") or ""))
            item["reads"] = describe(c.expr)
            t = now
            while len(times) < count and (t := c.next_fire(t, ttz)) is not None:
                times.append(t)
        elif spec.get("kind") == "at":
            from .cron import parse_instant
            when = parse_instant(spec.get("when"))
            times = [when] if when > now else []
        item["next"] = [{"local": iso(t, ttz), "hub": iso(t, tz)} for t in times]
        return item

    def a_validate(self, rid, query, data) -> dict:
        doc = data.get("job") if isinstance(data.get("job"), dict) else data
        try:
            job, warns = check(doc, now=self.clock())
        except ValidationError as e:
            return {"valid": False, "errors": e.errors}
        return {"valid": True, "errors": [], "warnings": warns + self.guardrails.warnings(job)}

    def a_describe_schema(self, rid, query, data) -> dict:
        return schema()

    # -- guardrails ------------------------------------------------------------------------
    def _proposed_defaults(self, data: dict) -> dict:
        if data.get("reset") is True:
            return copy.deepcopy(DEFAULT_GUARDRAILS)
        doc = data.get("defaults") if isinstance(data.get("defaults"), dict) else \
            {k: data[k] for k in ("deny", "allow") if k in data}
        if not doc:
            raise ValueError("data.defaults is required: {deny: [...], allow: [...]} (or data.reset: true)")
        errs = check_defaults(doc)
        if errs:
            raise ValidationError(errs)
        return normalize_defaults(doc)

    def a_guardrails_preview(self, rid, query, data) -> dict:
        """What a guardrail change would block. With ``id``: ``data.guardrails``
        is the job's proposed ``guardrails`` block. Without: ``data.defaults``
        (``{deny, allow}``) is the proposed hub default list; only jobs that
        inherit it are affected."""
        who = caller()
        roster = self._roster()
        if rid:
            row = self._job(rid)
            self.need(row, "read", who)
            g = data.get("guardrails")
            errs = check_job_block(g) if g is not None else ["data.guardrails is required"]
            if errs:
                raise ValidationError(errs)
            new = {**row["definition"], "guardrails": {"inherit": True, "allow": [], "deny": [], **g}}
            pairs = [(row, self.guardrails.scan(row["definition"], roster=roster),
                      self.guardrails.scan(new, roster=roster))]
            proposed, scope = new["guardrails"], "job"
        else:
            proposed, scope = self._proposed_defaults(data), "defaults"
            current = self.guardrails.defaults()
            pairs = [(r, self.guardrails.scan(r["definition"], defaults=current, roster=roster),
                      self.guardrails.scan(r["definition"], defaults=proposed, roster=roster))
                     for r in self._readable(who)]
        return self._preview(scope, proposed, pairs)

    @staticmethod
    def _preview(scope: str, proposed: dict, pairs: list) -> dict:
        def key(b: dict) -> tuple:
            return (b["step"], b["cap"], b["target"])

        newly, unblocked, blocked, jobs = [], [], [], []
        for row, before, after in pairs:
            tag = {"job_id": row["id"], "job": row["name"]}
            was = {key(b) for b in before}
            now = {key(b) for b in after}
            new_here = [{**tag, **b} for b in after if key(b) not in was]
            newly += new_here
            unblocked += [{**tag, **b} for b in before if key(b) not in now]
            blocked += [{**tag, **b} for b in after]
            if new_here:
                jobs.append({"id": row["id"], "name": row["name"], "enabled": row["enabled"],
                             "steps": sorted({b["step"] for b in new_here})})
        return {"scope": scope, "proposed": proposed, "newly_blocked": newly, "unblocked": unblocked,
                "blocked": blocked, "jobs_newly_blocked": jobs, "checked": len(pairs)}

    def a_set_guardrails(self, rid, query, data) -> dict:
        """With ``id``: set that job's ``guardrails`` block (``data.guardrails``;
        edit access, ``allow`` is operator only). Without: save the hub default
        list (``data.defaults`` or ``data.reset: true``; operator only) and
        return the preview of what it newly blocks."""
        if rid:
            if not isinstance(data.get("guardrails"), dict):
                raise ValueError("data.guardrails is required: {inherit, deny, allow}")
            patch = {"guardrails": data["guardrails"]}
            if data.get("revision") is not None:
                patch["revision"] = data["revision"]
            return self.a_update(rid, "", patch)
        from ...authz import require_hub_admin
        refusal = require_hub_admin("changing the default job guardrails")
        if refusal:
            raise PermissionError(refusal)
        proposed = self._proposed_defaults(data)
        preview = self.a_guardrails_preview(None, "", {"defaults": proposed})
        svc = getattr(self.node, "settings", None)
        if svc is None:
            raise LookupError("the settings service is not running on this hub")
        who = caller()
        svc.set("job.guardrails", proposed, actor=who["id"], source="job.write")
        now = self.clock()
        for j in preview["jobs_newly_blocked"]:
            self.store.add_history(j["id"], now, who["id"], "guardrail_blocked",
                                   {"steps": j["steps"], "by": "default guardrails"})
        return {"defaults": self.guardrails.defaults(), "preview": preview}

    # -- writes --------------------------------------------------------------------------
    def _clean(self, data: dict) -> dict:
        doc = data.get("job") if isinstance(data.get("job"), dict) else dict(data)
        doc = {k: v for k, v in doc.items() if k not in ("id", "owner", "revision")}
        return doc

    def _guard_block(self, job: dict, old: dict | None, who: dict) -> None:
        """``guardrails.allow`` is the operator's (``old`` is the job's
        previous ``guardrails`` block). A job that stops inheriting keeps a
        copy of the defaults as they are now (``base``), which only the
        operator can change."""
        g = job["guardrails"]
        old = old or {}
        admin = is_admin(who)
        if not admin and list(g.get("allow") or []) != list(old.get("allow") or []):
            raise PermissionError("denied: only the operator may set a job's guardrails.allow "
                                  "(it overrides the default deny list)")
        if g.get("inherit", True):
            g.pop("base", None)
        elif not (admin and g.get("base") is not None):
            keep = old.get("base") if old.get("inherit", True) is False else None
            g["base"] = keep if keep is not None else self.guardrails.defaults()

    def _warn(self, job: dict) -> list[str]:
        return self.guardrails.warnings(job)

    def a_create(self, rid, query, data) -> dict:
        who = caller()
        doc = self._clean(data)
        if "access" not in doc:
            doc["access"] = self.default_access()
        job, warns = check(doc, now=self.clock())
        self._guard_block(job, None, who)
        authorize(job.get("identity"), who, self.directory())
        row = self.store.create_job(job, who, self.clock(), self.tz())
        out = self.job_view(row, full=True, who=who)
        warns = warns + self._warn(row["definition"])
        if warns:
            out["warnings"] = warns
        return out

    def _takeover(self, row: dict, who: dict) -> bool:
        """Whether a change by ``who`` hands the job to them (they are neither
        its owner nor the operator)."""
        return not (is_admin(who) or who.get("id") == row["owner"])

    def a_update(self, rid, query, data) -> dict:
        who = caller()
        row = self._job(rid)
        self.need(row, "edit", who)
        expect = data.get("revision")
        patch = self._clean(data)
        old = row["definition"]
        merged = {k: v for k, v in old.items() if k not in ("id", "owner")}
        merged.update(patch)
        takeover = self._takeover(row, who)
        detail: dict = {"fields": sorted(patch)}
        if takeover:
            # The edit-resets-identity rule (fixed): the editor cannot borrow the
            # owner's identity, keys or the operator's guardrail allowances.
            if "identity" not in patch:
                merged["identity"] = {"mode": "creator"}
            g = dict(merged.get("guardrails") or {})
            g["allow"] = []
            merged["guardrails"] = g
            detail["identity_reset"] = {"from": row["owner"], "to": who["id"]}
        job, warns = check(merged, now=self.clock())
        old_g = dict(old.get("guardrails") or {})
        if takeover:
            old_g["allow"] = []
        self._guard_block(job, old_g, who)
        authorize(job.get("identity"), who, self.directory(),
                  previous=None if takeover else old.get("identity"))
        row = self.store.update_job(row["id"], job, who["id"], self.clock(), self.tz(),
                                    expect_revision=int(expect) if expect is not None else None,
                                    owner_info=who if takeover else None, detail=detail)
        out = self.job_view(row, full=True, who=who)
        warns = warns + self._warn(row["definition"])
        if warns:
            out["warnings"] = warns
        if takeover:
            out["identity_reset"] = detail["identity_reset"]
        return out

    def a_delete(self, rid, query, data) -> dict:
        row = self._job(rid)
        self.need(row, "edit")
        for run in self.store.runs(row["id"], ["running"], 500):
            self.scheduler.cancel_local(run["id"])
        return self.store.delete_job(row["id"])

    def a_enable(self, rid, query, data) -> dict:
        who = caller()
        row = self._job(rid)
        self.need(row, "edit", who)
        if not self._takeover(row, who):
            return self.job_view(self.store.set_enabled(row["id"], True, who["id"], self.clock(), self.tz()),
                                 who=who)
        d = {**row["definition"], "identity": {"mode": "creator"},
             "guardrails": {**(row["definition"].get("guardrails") or {}), "allow": []}}
        reset = {"from": row["owner"], "to": who["id"]}
        out = self.job_view(self.store.set_enabled(row["id"], True, who["id"], self.clock(), self.tz(),
                                                   definition=d, owner_info=who,
                                                   detail={"identity_reset": reset}), who=who)
        out["identity_reset"] = reset
        return out

    def a_disable(self, rid, query, data) -> dict:
        who = caller()
        row = self._job(rid)
        self.need(row, "edit", who)
        reason = str(data.get("reason") or "disabled")[:200]
        return self.job_view(self.store.set_enabled(row["id"], False, who["id"], self.clock(), self.tz(),
                                                    reason=reason), who=who)

    def a_run(self, rid, query, data) -> dict:
        row = self._job(rid)
        self.need(row, "run")
        if not row["enabled"]:
            raise ValueError(f"job {row['name']!r} is disabled; enable it first")
        vars_ = data.get("vars") or {}
        if not isinstance(vars_, dict):
            raise TypeError("data.vars must be an object")
        run = self.store.enqueue(row, "manual", self.clock(), vars=vars_)
        self.scheduler.wake()
        return self.run_view(run, steps=False)

    def a_cancel(self, rid, query, data) -> dict:
        if not rid:
            raise ValueError("id is required (a run id, or a job id/name to cancel all its runs)")
        targets = []
        run = self.store.get_run(rid)
        if run is not None:
            self._need_run_job(run, "run")
            targets = [rid]
        else:
            row = self._job(rid)
            self.need(row, "run")
            targets = [r["id"] for r in self.store.runs(row["id"], ["queued", "due", "running"], 500)]
        out = []
        for r in targets:
            run = self.store.cancel(r, self.clock())
            if run["state"] == "running":
                self.scheduler.cancel_local(r)
            out.append({"id": r, "state": run["state"], "cancel_requested": run["state"] == "running"})
        return {"cancelled": out}

    def a_settings(self, rid, query, data) -> dict:
        if data:
            from ...authz import require_hub_admin
            refusal = require_hub_admin("changing job settings")
            if refusal:
                raise PermissionError(refusal)
            svc = getattr(self.node, "settings", None)
            if svc is None:
                raise LookupError("the settings service is not running on this hub")
            unknown = [k for k in data if k not in self.setting_names]
            if unknown:
                raise ValueError(f"unknown job setting(s) {unknown}; settings: {', '.join(self.setting_names)}")
            if "timezone" in data and not valid_zone(str(data["timezone"])):
                raise ValueError(f"unknown time zone {data['timezone']!r}")
            errs = []
            if "guardrails" in data:
                errs += check_defaults(data["guardrails"])
            if "default_access" in data:
                errs += [e.replace("access", "default_access", 1) for e in check_access(data["default_access"])]
            if data.get("default_fallback"):
                try:
                    parse_spec(data["default_fallback"])
                except ValueError as e:
                    errs.append(f"default_fallback: {e}")
            if errs:
                raise ValidationError(errs)
            for k, v in data.items():
                svc.set(f"job.{k}", v, actor=caller()["id"], source="job.write")
        return {"settings": {k: self.settings(k) for k in self.setting_names}}

    # -- alerts --------------------------------------------------------------------------
    async def alerts(self, job: dict, run: dict, identity: Any, state: str) -> list | None:
        """``alerts.on_success`` / ``alerts.on_failure`` notify specs, after
        the run (failure covers hang, blocked and interrupted)."""
        key = "on_success" if state == "success" else ("on_failure" if state != "cancelled" else None)
        specs = ((job.get("alerts") or {}).get(key) or []) if key else []
        out = []
        for spec in specs:
            ctx = StepContext(job=job, run={**run, "state": state}, step_id=f"alert:{key}", identity=identity,
                              runtime=self.scheduler.executor.runtime, settings=self.settings,
                              timeout=ALERT_TIMEOUT)
            try:
                res = await asyncio.wait_for(run_notify(ctx, spec), ALERT_TIMEOUT)
                out.append({"via": spec.get("via", "notify"), "state": res.state,
                            **({"error": res.error} if res.error else {})})
            except Exception as e:  # noqa: BLE001 - an alert failing never changes the run
                out.append({"via": spec.get("via", "notify"), "state": "failure", "error": str(e) or type(e).__name__})
        return out or None
