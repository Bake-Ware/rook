"""Runs one job run: walks the step graph to the end.

* A step runs with its timeout (a timeout is ``hang``), retries (on failure
  or hang, before branching), then the success rules; the guardrail check
  (:func:`.guardrails.check_step`) comes first and a refusal is ``blocked``.
* Every step listed under the outcome key runs next, in parallel when there
  are several. A branch may point back to an earlier step to loop, bounded by
  the per-run cap on step executions (setting ``max_step_executions``, 100).
* A ``join`` collects arrivals on its incoming branches. ``all`` fires when
  every incoming branch arrived, ``any`` on the first, ``{"at_least": N}`` on
  the Nth; an expression fires once every branch arrived. When nothing else
  is running, a waiting join settles: an expression is evaluated, a count
  that was not reached is ``failure``. A join fires once per run.
* The run's state is the worst final step state, ``blocked`` > ``hang`` >
  ``failure`` > ``success``, ignoring steps with ``allow_failure``.

Step outputs are masked (known vault values and the values the step used
become ``{{secret:name}}``) and truncated before they are stored.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from .cron import parse_duration
from .expr import compile_expr, evaluate
from .guardrails import Blocked, GuardedRuntime, check_step
from .model import incoming
from .steps import StepContext, StepResult, apply_success_rules, step_kind

log = logging.getLogger("rook.hub.plugins.jobs.executor")

OUTPUT_MAX = 4000
ERROR_MAX = 500
DEFAULT_MAX_EXECUTIONS = 100
_WORST = ("blocked", "hang", "failure")


def clip(value: Any, limit: int = OUTPUT_MAX) -> Any:
    """Keep small outputs as they are; larger ones as truncated text."""
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + f"…[truncated {len(text) - limit} chars]"


def view(records: dict, steps: dict) -> dict:
    """The ``steps`` scope join expressions see: every step id, with ``ok``,
    ``state`` and ``exit_code`` (``None`` for a step that has not run)."""
    out = {}
    for sid in steps:
        r = records.get(sid) or {}
        out[sid] = {"ok": r.get("state") == "success", "state": r.get("state"),
                    "exit_code": r.get("exit_code"), "attempts": r.get("attempts", 0)}
    return out


class RunCancelled(Exception):
    pass


class Executor:
    def __init__(self, runtime: Any, *, settings: Callable[[str], Any] = lambda _k: None) -> None:
        self.runtime = runtime
        self.settings = settings

    def _max_executions(self) -> int:
        try:
            return int(self.settings("max_step_executions") or DEFAULT_MAX_EXECUTIONS)
        except (TypeError, ValueError):
            return DEFAULT_MAX_EXECUTIONS

    async def execute(self, job: dict, run: dict, identity: Any,
                      on_progress: Callable[[dict, int], None] | None = None) -> dict:
        """Walk the graph; returns ``{state, steps, executions, error}``.
        ``on_progress(records, executions)`` is called after each step."""
        g = _Graph(self, job, run, identity, on_progress)
        return await g.go()

    # -- one step ---------------------------------------------------------------
    async def run_step(self, job: dict, run: dict, sid: str, identity: Any, records: dict) -> tuple[StepResult, dict]:
        step = job["steps"][sid]
        rt = self.runtime
        rec = {"state": "running", "started": rt.clock(), "attempts": 0}
        verdict = check_step(job, step, identity)
        used: dict = {}
        if not verdict.allow:
            res = StepResult("blocked", error=verdict.reason or "blocked by a guardrail",
                             extra={"rule": verdict.rule} if verdict.rule else {})
            return res, self._record(rec, res, used)
        kind = step_kind(step.get("kind"))
        timeout = parse_duration(step.get("timeout") or "5m")
        retry = step.get("retry") or {}
        tries = 1 + int(retry.get("max", 0) or 0)
        delay = parse_duration(retry.get("delay") or 0)
        res = StepResult("failure", error="not run")
        guarded = GuardedRuntime(rt, job, identity)  # each call checked with its resolved worker
        for attempt in range(1, tries + 1):
            rec["attempts"] = attempt
            ctx = StepContext(job=job, run=run, step_id=sid, identity=identity, runtime=guarded,
                              settings=self.settings, timeout=timeout, attempt=attempt,
                              records=records, used=used)
            try:
                if kind is None:
                    raise LookupError(f"step kind {step.get('kind')!r} is not available on this hub")
                res = await asyncio.wait_for(kind.run(ctx, step), timeout)
            except asyncio.TimeoutError:
                res = StepResult("hang", error=f"no result within {timeout:g}s")
            except asyncio.CancelledError:
                raise
            except Blocked as e:  # a hub tool's inner call was refused by a guardrail
                res = StepResult("blocked", error=str(e), extra={"rule": e.verdict.rule})
            except Exception as e:  # noqa: BLE001 - a handler error is the step's failure
                res = StepResult("failure", error=f"{type(e).__name__}: {e}")
            res = apply_success_rules(step, res)
            if res.state in ("success", "blocked") or attempt == tries:
                break
            if delay:
                await rt.sleep(delay)
        return res, self._record(rec, res, used)

    def _record(self, rec: dict, res: StepResult, used: dict) -> dict:
        rt = self.runtime
        rec["finished"] = rt.clock()
        rec["state"] = res.state
        if res.exit_code is not None:
            rec["exit_code"] = res.exit_code
        if res.output is not None:
            rec["output"] = clip(rt.mask(res.output, used))
        if res.error:
            rec["error"] = clip(rt.mask(res.error, used), ERROR_MAX)
        for k, v in (res.extra or {}).items():
            rec[k] = clip(rt.mask(v, used), ERROR_MAX)
        return rec


class _Graph:
    """Graph state for one run."""

    def __init__(self, ex: Executor, job: dict, run: dict, identity: Any,
                 on_progress: Callable[[dict, int], None] | None) -> None:
        self.ex = ex
        self.on_progress = on_progress
        self.job = job
        self.run = run
        self.identity = identity
        self.steps: dict = job["steps"]
        self.incoming = incoming(self.steps)
        self.records: dict = {}
        self.executions = 0
        self.limit = ex._max_executions()
        self.error: str | None = None
        self.tasks: dict[asyncio.Task, str] = {}
        self.joins: dict[str, dict] = {}   # join id -> {"arrived": set, "fired": bool}

    def _progress(self) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(self.records, self.executions)
            except Exception:
                log.exception("jobs: saving run progress failed")

    def _start(self, sid: str) -> None:
        if self.error:
            return
        self.executions += 1
        if self.executions > self.limit:
            self.error = f"stopped: more than {self.limit} step executions in one run"
            return
        prev = self.records.get(sid) or {}
        self.records[sid] = {"state": "running", "started": self.ex.runtime.clock(),
                             "runs": prev.get("runs", 0) + 1}
        task = asyncio.ensure_future(self.ex.run_step(self.job, self.run, sid, self.identity, self.records))
        self.tasks[task] = sid

    def _activate(self, sid: str, src: str | None) -> None:
        step = self.steps[sid]
        if step.get("kind") == "join":
            j = self.joins.setdefault(sid, {"arrived": set(), "fired": False})
            if j["fired"]:
                return
            if src is not None:
                j["arrived"].add(src)
            self.records[sid] = {"state": "waiting", "arrived": sorted(j["arrived"])}
            if self._join_ready(sid, final=False):
                self._fire_join(sid)
            return
        self._start(sid)

    def _join_ready(self, sid: str, final: bool) -> bool:
        cond = self.steps[sid].get("condition", "all")
        arrived, total = len(self.joins[sid]["arrived"]), len(self.incoming[sid])
        if cond == "any":
            return arrived >= 1
        if isinstance(cond, dict):
            return arrived >= int(cond["at_least"]) or final
        return arrived >= total or final  # "all" and expressions

    def _join_outcome(self, sid: str) -> StepResult:
        cond = self.steps[sid].get("condition", "all")
        arrived, total = len(self.joins[sid]["arrived"]), len(self.incoming[sid])
        detail = {"arrived": sorted(self.joins[sid]["arrived"])}
        if cond == "any":
            ok = arrived >= 1
        elif cond == "all":
            ok = arrived >= total
        elif isinstance(cond, dict):
            ok = arrived >= int(cond["at_least"])
        else:
            scope = {"steps": view(self.records, self.steps),
                     "run": {"id": self.run.get("id"), "missed": bool(self.run.get("missed")),
                             "trigger": self.run.get("trigger")},
                     "vars": {**(self.job.get("vars") or {}), **(self.run.get("vars") or {})},
                     "job": {"id": self.job.get("id"), "name": self.job.get("name")}}
            try:
                ok = evaluate(compile_expr(cond), scope)
            except ValueError as e:
                return StepResult("failure", output=detail, error=f"condition: {e}")
        return StepResult("success" if ok else "failure", output=detail,
                          error=None if ok else f"join condition not met ({arrived}/{total} arrived)")

    def _fire_join(self, sid: str) -> None:
        self.joins[sid]["fired"] = True
        self.executions += 1
        if self.executions > self.limit:
            self.error = f"stopped: more than {self.limit} step executions in one run"
            return
        now = self.ex.runtime.clock()
        res = self._join_outcome(sid)
        rec = {"state": res.state, "started": now, "finished": now, "attempts": 1, "output": res.output}
        if res.error:
            rec["error"] = res.error
        self._done(sid, res, rec)

    def _done(self, sid: str, res: StepResult, rec: dict) -> None:
        rec["runs"] = (self.records.get(sid) or {}).get("runs", 1)
        self.records[sid] = rec
        self._progress()
        outcome = "hang" if res.state == "hang" else ("success" if res.state == "success" else "failure")
        if res.state == "blocked":
            return  # a blocked step ends its branch
        for nxt in (self.steps[sid].get("on") or {}).get(outcome, []):
            self._activate(nxt, sid)

    def _settle_joins(self) -> None:
        for sid, j in self.joins.items():
            if not j["fired"] and j["arrived"]:
                self._fire_join(sid)
                return  # firing may start tasks; settle the rest later

    async def go(self) -> dict:
        try:
            self._activate(self.job["entry"], None)
            while True:
                if not self.tasks:
                    pending = [s for s, j in self.joins.items() if not j["fired"] and j["arrived"]]
                    if not pending or self.error:
                        break
                    self._settle_joins()
                    continue
                done, _ = await asyncio.wait(list(self.tasks), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    sid = self.tasks.pop(task)
                    res, rec = task.result()
                    self._done(sid, res, rec)
        except asyncio.CancelledError:
            await self._stop_tasks()
            for sid, rec in self.records.items():
                if rec.get("state") in ("running", "waiting"):
                    rec["state"] = "cancelled"
            return {"state": "cancelled", "steps": self.records, "executions": self.executions,
                    "error": "cancelled"}
        if self.error:
            await self._stop_tasks()
        return {"state": self.final_state(), "steps": self.records, "executions": self.executions,
                "error": self.error}

    async def _stop_tasks(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for sid in self.tasks.values():
            self.records.setdefault(sid, {})["state"] = "cancelled"
        self.tasks.clear()

    def final_state(self) -> str:
        if self.error:
            return "failure"
        states = [r.get("state") for sid, r in self.records.items()
                  if not self.steps[sid].get("allow_failure")]
        for bad in _WORST:
            if bad in states:
                return bad
        if any(s in ("waiting", "running") for s in states):
            return "failure"
        return "success"
