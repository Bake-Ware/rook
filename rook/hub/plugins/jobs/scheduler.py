"""The scheduler loop: triggers -> runs -> executor, with leases.

One loop per hub process (``run="one"``), safe to run in several processes
on one database (see :mod:`.store`). Each :meth:`Scheduler.tick`:

1. interrupts runs whose lease ran out (their process died);
2. arms ``after`` triggers from newly finished runs;
3. fires due cron / at / after triggers, applying the missed-run rules;
4. promotes queued runs whose job is idle;
5. claims due runs and starts them;
6. now and then, prunes run history past retention.

The clock and sleep are injectable so tests can move time by hand.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .cron import Cron, parse_duration
from .executor import Executor
from .identity import PAUSE_REASON, IdentityRevoked, resolve_identity
from .store import AFTER_STATES, JobStore, grace_seconds

log = logging.getLogger("rook.hub.plugins.jobs.scheduler")

#: A fire this late (seconds) still counts as on time, not missed.
ON_TIME = 90.0
LEASE = 60.0
PRUNE_EVERY = 3600.0
MAX_FIRES = 1000


class Scheduler:
    def __init__(self, store: JobStore, executor: Executor, *, owner: str,
                 settings: Callable[[str], Any] = lambda _k: None,
                 clock: Callable[[], float] = time.time, lease: float = LEASE,
                 alerts: Callable[[dict, dict, Any, str], Any] | None = None) -> None:
        self.store = store
        self.executor = executor
        self.owner = owner
        self.settings = settings
        self.clock = clock
        self.lease = lease
        self.alerts = alerts
        #: ``(job, owner_info) -> RunIdentity``; the plugin binds the hub's
        #: directory, settings and guardrails (identity.resolve_identity).
        self.resolve: Callable[[dict, dict | None], Any] = resolve_identity
        self.active: dict[str, asyncio.Task] = {}
        self._last_prune = 0.0
        self._stopping = False
        self._wake: asyncio.Event | None = None

    def tz(self) -> str:
        from .cron import DEFAULT_TZ, valid_zone
        tz = self.settings("timezone") or DEFAULT_TZ
        return tz if valid_zone(tz) else DEFAULT_TZ

    # -- lifecycle --------------------------------------------------------------
    def startup(self) -> list[str]:
        """Runs this hub left ``running`` are ``interrupted`` (not resumed)."""
        ids = self.store.interrupt(self.clock(), owner=self.owner)
        if ids:
            log.warning("jobs: %d run(s) interrupted by a hub restart", len(ids))
        return ids

    async def run_forever(self) -> None:
        self.startup()
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("jobs: scheduler tick failed")
            if self._wake is None:
                self._wake = asyncio.Event()
            try:
                await asyncio.wait_for(self._wake.wait(), max(1, int(self.settings("tick_seconds") or 5)))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        """Tick soon (a manual run should not wait for the next tick)."""
        if self._wake is not None:
            self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self.active.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self) -> list[str]:
        """One pass; returns the ids of the runs it started."""
        now = self.clock()
        self.store.interrupt(now)
        self.arm_after(now)
        self.fire_due(now)
        self.store.promote()
        started = []
        while True:
            run = self.store.claim(self.owner, self.clock(), self.lease)
            if run is None:
                break
            started.append(run["id"])
            self.active[run["id"]] = asyncio.ensure_future(self._execute(run))
        if now - self._last_prune >= PRUNE_EVERY:
            self._last_prune = now
            self.prune(now)
        return started

    def prune(self, now: float | None = None) -> int:
        days = int(self.settings("retention_days") or 30)
        return self.store.prune(self.clock() if now is None else now, days)

    # -- triggers ---------------------------------------------------------------
    def arm_after(self, now: float) -> None:
        for t in self.store.after_triggers():
            job = self.store.get_job(t["job_id"])
            last = self.store.last_finished(t["job_id"])
            if job is None or last is None or last["id"] == t["anchor"]:
                continue
            spec = (job["definition"].get("triggers") or [])[t["idx"]]
            want = AFTER_STATES.get(spec.get("on", "success"), ())
            next_at = last["finished"] + parse_duration(spec["every"]) if last["state"] in want else None
            self.store.arm_after(t["job_id"], t["idx"], t["anchor"], last["id"], next_at)

    def fire_due(self, now: float) -> list[dict]:
        created: list[dict] = []
        for t in self.store.due_triggers(now):
            job = self.store.get_job(t["job_id"])
            if job is None:
                continue
            spec = (job["definition"].get("triggers") or [])[t["idx"]]
            times, new_next, done = self._occurrences(t, spec, now)
            fires = self._apply_missed(job["definition"], times, now)
            label = spec["kind"] + (f" {spec.get('expr')}" if spec["kind"] == "cron" else "")
            runs = self.store.fire(t, new_next, done, fires, label, now)
            if runs is None:
                continue  # another scheduler fired it
            skipped = len(times) - len(fires)
            if skipped and job["definition"].get("missed", {}).get("mode") == "skip":
                self.store.enqueue(job, label, now, missed=True, scheduled=times[-1], state="dropped",
                                   error=f"missed {skipped} fire(s) while the hub was down (missed.mode skip)")
            created += runs
        return created

    def _occurrences(self, t: dict, spec: dict, now: float) -> tuple[list[float], float | None, bool]:
        """(due fire instants, the next one after now, trigger finished?)."""
        if spec["kind"] == "cron":
            tz = spec.get("tz") or self.tz()
            cron = Cron(spec["expr"])
            times = [t["next_at"]] + cron.fires(t["next_at"], now, tz, limit=MAX_FIRES)
            nxt = cron.next_fire(max(now, times[-1]), tz)
            return times, nxt, nxt is None
        # at and after: a single occurrence; after is re-armed by its next finish
        return [t["next_at"]], None, spec["kind"] == "at"

    @staticmethod
    def _apply_missed(job: dict, times: list[float], now: float) -> list[tuple[float, bool]]:
        """Missed-run rules (docs/design/jobs.md 4): fires later than ON_TIME
        are missed. ``run_once`` runs the latest missed fire within ``grace``,
        ``all`` every missed fire within ``grace``, ``skip`` none. On-time
        fires always run."""
        mode = (job.get("missed") or {}).get("mode", "run_once")
        grace = grace_seconds(job)
        on_time = [(t, False) for t in times if now - t <= ON_TIME]
        missed = [t for t in times if now - t > ON_TIME and now - t <= max(grace, ON_TIME)]
        if mode == "skip" or not missed:
            return on_time
        if mode == "run_once":
            return [(missed[-1], True)] + on_time
        return [(t, True) for t in missed] + on_time

    # -- runs -------------------------------------------------------------------
    async def _execute(self, run: dict) -> None:
        rid = run["id"]
        try:
            await self.execute(run)
        except Exception:
            log.exception("jobs: run %s crashed", rid)
            self.store.finish(rid, self.owner, "failure", self.clock(), error="internal error")
        finally:
            self.active.pop(rid, None)

    async def execute(self, run: dict) -> dict:
        rid = run["id"]
        row = self.store.get_job(run["job_id"])
        if row is None:
            self.store.finish(rid, self.owner, "cancelled", self.clock(), error="the job was deleted")
            return {"state": "cancelled"}
        job = row["definition"]
        try:
            identity = self.resolve(job, row["owner_info"])
        except PermissionError as e:
            self.store.finish(rid, self.owner, "blocked", self.clock(), error=str(e))
            if isinstance(e, IdentityRevoked):
                self.pause_revoked(row, str(e))
            return {"state": "blocked"}
        self.store.set_identity(rid, identity.id + (" (fallback)" if identity.mode == "fallback" else ""))
        ctx_run = {"id": rid, "started": run.get("started"), "missed": bool(run.get("missed")),
                   "trigger": run.get("trigger"), "vars": run.get("vars") or {}}

        def progress(records: dict, executions: int) -> None:
            self.store.save_progress(rid, self.owner, records, executions)

        work = asyncio.ensure_future(self.executor.execute(job, ctx_run, identity, progress))
        renew = asyncio.ensure_future(self._hold(rid, work))
        try:
            result = await work
        except asyncio.CancelledError:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            result = {"state": "cancelled", "error": "cancelled"}
            if not work.cancelled() and work.exception() is None and isinstance(work.result(), dict):
                result = work.result()
        finally:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
        if self._stopping and result.get("state") == "cancelled":
            # The hub is shutting down: the run did not finish and is not resumed.
            result = {**result, "state": "interrupted", "error": "interrupted: the hub stopped while it ran"}
        alerts = None
        if self.alerts is not None and not self._stopping:
            try:
                alerts = await self.alerts(job, ctx_run, identity, result["state"])
            except Exception:
                log.exception("jobs: alerts for run %s failed", rid)
        self.store.finish(rid, self.owner, result["state"], self.clock(), steps=result.get("steps"),
                          executions=result.get("executions"), error=result.get("error"), alerts=alerts)
        return result

    async def _hold(self, rid: str, work: asyncio.Task) -> None:
        """Renew the lease while the run works; cancel it when asked to."""
        while not work.done():
            await asyncio.sleep(max(0.05, self.lease / 3))
            held, cancel = self.store.renew(rid, self.owner, self.clock() + self.lease)
            if cancel or not held:
                work.cancel()
                return

    def pause_revoked(self, row: dict, reason: str) -> None:
        """No usable identity is left: pause the job (``identity_revoked``)
        until someone with edit access re-enables it (and so takes it over)."""
        try:
            self.store.set_enabled(row["id"], False, "system:jobs", self.clock(), self.tz(),
                                   reason=PAUSE_REASON, detail={"error": reason[:300]})
            log.warning("jobs: paused %s: %s", row["name"], reason)
        except Exception:
            log.exception("jobs: pausing %s failed", row.get("id"))

    def cancel_local(self, rid: str) -> bool:
        task = self.active.get(rid)
        if task is None:
            return False
        task.cancel()
        return True
