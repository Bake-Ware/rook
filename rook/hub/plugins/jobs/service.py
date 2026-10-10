"""The ``job.read`` / ``job.write`` actions (docs/design/jobs.md 8).

Reads: list, get, runs, run_get, next, validate, guardrails_preview,
describe_schema. Writes: create, update, delete, enable, disable, run,
cancel, set_guardrails, settings. ``guardrails_preview`` and
``set_guardrails`` belong to the guardrails workstream and answer "not
available yet" until it lands.

Times go out as zone-aware ISO strings in the hub zone (setting
``job.timezone``); ``next`` also gives each fire in the trigger's own zone.
Run output is masked again on read, so a secret added after a run was stored
is masked when shown.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from ....band_mcp import secret_mask
from .cron import DEFAULT_TZ, Cron, describe, iso, valid_zone
from .identity import caller
from .kinds import run_notify
from .model import ValidationError, check, schema
from .steps import StepContext
from .store import JobStore

READS = ("list", "get", "runs", "run_get", "next", "validate", "guardrails_preview", "describe_schema")
WRITES = ("create", "update", "delete", "enable", "disable", "run", "cancel", "set_guardrails", "settings")
ERRORS = (ValueError, KeyError, TypeError, PermissionError, LookupError)
ALERT_TIMEOUT = 30.0


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


class JobService:
    def __init__(self, store: JobStore, scheduler: Any, *, settings: Callable[[str], Any],
                 clock: Callable[[], float] = time.time, node: Any = None,
                 setting_names: tuple = ()) -> None:
        self.store = store
        self.scheduler = scheduler
        self.settings = settings
        self.clock = clock
        self.node = node
        self.setting_names = setting_names

    def tz(self) -> str:
        tz = self.settings("timezone") or DEFAULT_TZ
        return tz if valid_zone(tz) else DEFAULT_TZ

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

    # -- views --------------------------------------------------------------------------
    def job_view(self, row: dict, full: bool = False) -> dict:
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
        if full:
            out["created"] = iso(row["created"], tz)
            out["updated_by"] = row.get("updated_by")
            out["counts"] = self.store.counts(row["id"])
            out["definition"] = d
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

    # -- reads ----------------------------------------------------------------------------
    def a_list(self, rid, query, data) -> dict:
        rows = self.store.list_jobs()
        q = (query or data.get("query") or "").lower()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in (r["definition"].get("description") or "").lower()]
        if "enabled" in data:
            rows = [r for r in rows if r["enabled"] == bool(data["enabled"])]
        limit = max(1, min(int(data.get("limit") or 50), 500))
        return {"jobs": [self.job_view(r) for r in rows[:limit]], "total": len(rows), "timezone": self.tz()}

    def a_get(self, rid, query, data) -> dict:
        return self.job_view(self._job(rid), full=True)

    def a_runs(self, rid, query, data) -> dict:
        jid = self._job(rid)["id"] if rid else None
        states = data.get("states") or data.get("state") or (query or None)
        if isinstance(states, str):
            states = [s.strip() for s in states.split(",") if s.strip()]
        since = None
        if data.get("since"):
            from .cron import parse_instant
            since = parse_instant(data["since"])
        runs = self.store.runs(jid, states, int(data.get("limit") or 20), since,
                               data.get("missed") if isinstance(data.get("missed"), bool) else None)
        return {"runs": [self.run_view(r, steps=bool(data.get("steps"))) for r in runs]}

    def a_run_get(self, rid, query, data) -> dict:
        if not rid:
            raise ValueError("id is required (a run id)")
        run = self.store.get_run(rid)
        if run is None:
            raise KeyError(f"no run {rid!r}")
        return self.run_view(run)

    def a_next(self, rid, query, data) -> dict:
        tz = self.tz()
        count = max(1, min(int(data.get("count") or 5), 20))
        now = self.clock()
        if not rid:
            rows = [t for t in self.store.triggers() if t["next_at"] is not None and not t["done"]]
            names = {}
            out = []
            for t in rows[: max(count, 20)]:
                if t["job_id"] not in names:
                    j = self.store.get_job(t["job_id"])
                    names[t["job_id"]] = j["name"] if j else t["job_id"]
                out.append({"job_id": t["job_id"], "job": names[t["job_id"]], "kind": t["kind"],
                            "at": iso(t["next_at"], tz)})
            return {"upcoming": out, "timezone": tz}
        row = self._job(rid)
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

    def a_validate(self, rid, query, data) -> dict:
        doc = data.get("job") if isinstance(data.get("job"), dict) else data
        try:
            _job, warns = check(doc, now=self.clock())
        except ValidationError as e:
            return {"valid": False, "errors": e.errors}
        return {"valid": True, "errors": [], "warnings": warns}

    def a_guardrails_preview(self, rid, query, data):
        raise NotAvailable("guardrails_preview is not available yet: job guardrails arrive with the "
                           "identity and guardrails update (every step is allowed until then)")

    def a_describe_schema(self, rid, query, data) -> dict:
        return schema()

    # -- writes --------------------------------------------------------------------------
    def _clean(self, data: dict) -> dict:
        doc = data.get("job") if isinstance(data.get("job"), dict) else dict(data)
        doc = {k: v for k, v in doc.items() if k not in ("id", "owner", "revision")}
        return doc

    def a_create(self, rid, query, data) -> dict:
        job, warns = check(self._clean(data), now=self.clock())
        row = self.store.create_job(job, caller(), self.clock(), self.tz())
        out = self.job_view(row, full=True)
        if warns:
            out["warnings"] = warns
        return out

    def a_update(self, rid, query, data) -> dict:
        row = self._job(rid)
        expect = data.get("revision")
        patch = self._clean(data)
        merged = {k: v for k, v in row["definition"].items() if k not in ("id", "owner")}
        merged.update(patch)
        job, warns = check(merged, now=self.clock())
        row = self.store.update_job(row["id"], job, caller()["id"], self.clock(), self.tz(),
                                    expect_revision=int(expect) if expect is not None else None)
        out = self.job_view(row, full=True)
        if warns:
            out["warnings"] = warns
        return out

    def a_delete(self, rid, query, data) -> dict:
        row = self._job(rid)
        for run in self.store.runs(row["id"], ["running"], 500):
            self.scheduler.cancel_local(run["id"])
        return self.store.delete_job(row["id"])

    def a_enable(self, rid, query, data) -> dict:
        row = self._job(rid)
        return self.job_view(self.store.set_enabled(row["id"], True, caller()["id"], self.clock(), self.tz()))

    def a_disable(self, rid, query, data) -> dict:
        row = self._job(rid)
        reason = str(data.get("reason") or "disabled")[:200]
        return self.job_view(self.store.set_enabled(row["id"], False, caller()["id"], self.clock(), self.tz(),
                                                    reason=reason))

    def a_run(self, rid, query, data) -> dict:
        row = self._job(rid)
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
        if self.store.get_run(rid) is not None:
            targets = [rid]
        else:
            row = self._job(rid)
            targets = [r["id"] for r in self.store.runs(row["id"], ["queued", "due", "running"], 500)]
        out = []
        for r in targets:
            run = self.store.cancel(r, self.clock())
            if run["state"] == "running":
                self.scheduler.cancel_local(r)
            out.append({"id": r, "state": run["state"], "cancel_requested": run["state"] == "running"})
        return {"cancelled": out}

    def a_set_guardrails(self, rid, query, data):
        raise NotAvailable("set_guardrails is not available yet: job guardrails arrive with the "
                           "identity and guardrails update")

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
