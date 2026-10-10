"""``job.*``: scheduled, branching work owned by the hub (worker ``rook``).

docs/design/jobs.md is the contract. A job is a graph of steps (cap, fanout,
tool, wait, notify, join, noop; agent and ask come later) fired by cron, at,
after or manual triggers. The hub owns the schedule, the run state and the
history in its own SQLite store (``jobs.db`` in the plugin's data dir).

Modules:

* :mod:`.cron` - durations, zone-aware instants, 5-field cron with DST rules;
* :mod:`.model` - defaults, validation on save, the JSON schema;
* :mod:`.store` - SQLite store, migrations, atomic trigger fires and leases;
* :mod:`.steps` - the step-kind registry and step context; :mod:`.kinds`
  the built-in kinds; :mod:`.expr` the safe join-condition evaluator;
* :mod:`.executor` - walks one run's graph; :mod:`.scheduler` - the loop;
* :mod:`.runtime` - band calls, hub tools, vault substitution, the journal;
* :mod:`.identity` (``resolve_identity``) and :mod:`.guardrails`
  (``check_step``) - the seams the identity/guardrails work replaces;
* :mod:`.service` - the ``job.read`` / ``job.write`` actions.

Caps (``rook_call(cap=..., worker="rook")``; the ``rook_jobs`` MCP tool
routes to them):

* ``job.read`` (risk ``read``): list, get, runs, run_get, next, validate,
  guardrails_preview, describe_schema.
* ``job.write`` (risk ``write``): create, update, delete, enable, disable,
  run, cancel, set_guardrails, settings.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ....core.plugin import Plugin, capability, place, setting
from .cron import DEFAULT_TZ
from .executor import DEFAULT_MAX_EXECUTIONS, Executor
from .runtime import Runtime
from .scheduler import Scheduler
from .service import ERRORS, READS, WRITES, JobService, error_reply, reply, route
from .store import JobStore

log = logging.getLogger("rook.hub.plugins.jobs")


class Jobs(Plugin):
    NAMESPACE = "job"
    NAME = "jobs"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SETTINGS = (
        setting("enabled", bool, default=True, env="ROOK_JOBS", apply="restart", group="General",
                label="Jobs", help="Scheduled jobs and their scheduler loop. Read at start: restart the hub "
                                   "to apply."),
        setting("timezone", str, default=DEFAULT_TZ, group="General", label="Time zone",
                help="IANA zone for schedules without their own tz, and for times shown. "
                     "Never the host clock's zone."),
        setting("retention_days", int, default=30, min=1, max=3650, group="General",
                label="Keep run history (days)", help="A job's retention_days overrides it."),
        setting("max_step_executions", int, default=DEFAULT_MAX_EXECUTIONS, min=1, max=10000,
                group="Runs", label="Step executions per run",
                help="Stops a run whose branches loop more than this."),
        setting("notify_worker", str, default="", group="Runs", label="Notify worker",
                help="Worker that notify/voice steps go to when a step names none. Empty: the first "
                     "live worker with the cap."),
        setting("tick_seconds", int, default=5, min=1, max=300, group="Runs", advanced=True,
                label="Scheduler tick (seconds)"),
        setting("db_path", "path", default="", env="ROOK_JOBS_DB", apply="restart", group="General",
                advanced=True, label="Database file", help="Empty: jobs.db in the plugin's data directory."),
    )
    GUIDANCE = {
        "tool:rook_jobs": "",
        "cap:job.": ("job.read/job.write take rook_jobs' action, id, query and data; the rook_jobs "
                     "tool routes to them. describe_schema returns the job JSON schema."),
    }
    SKILL = ("### jobs\n"
             "Scheduled, branching work the hub runs. `rook_jobs(action=\"describe_schema\")` gives the "
             "job JSON schema; `validate` checks a draft (`data` = the job); `create` saves it (you are "
             "its owner and runs act as you). A job is `steps` (cap, fanout, tool, wait, notify, join, "
             "noop) wired by `on: {success, failure, hang}` from `entry`, fired by `triggers` (cron in "
             "the hub zone unless `tz`, at, after, manual). `run` starts one now; `runs` / `run_get` "
             "show masked step output; `next` lists fire times. Over the band: `job.read` / "
             "`job.write` on worker `rook`. Use `{{secret:name}}` in step args, never values.\n")

    def __init__(self) -> None:
        super().__init__()
        self.store: JobStore | None = None
        self.runtime: Runtime | None = None
        self.scheduler: Scheduler | None = None
        self.service: JobService | None = None
        self._node = None
        self._loop: asyncio.Task | None = None

    # -- setup -----------------------------------------------------------------------
    def db_path(self) -> Path | None:
        configured = self.settings.get("db_path")
        if configured:
            return Path(configured)
        if not self.__dict__.get("_data_root"):
            return None  # no state dir (tests, ephemeral hubs): no jobs
        return self.data_dir / "jobs.db"

    def _setting(self, name: str):
        return self.settings.get(name)

    def available(self) -> bool:
        if not self.settings.get("enabled", True):
            return False
        path = self.db_path()
        if path is None:
            return False
        try:
            self.store = JobStore(path)
        except Exception:
            log.exception("jobs store unavailable; job caps disabled")
            return False
        self.wire(Runtime(None))
        return True

    def wire(self, runtime: Runtime, *, owner: str = "hub", clock=None) -> None:
        """Build the executor, scheduler and service on the open store."""
        self.runtime = runtime
        executor = Executor(runtime, settings=self._setting)
        kw = {"clock": clock} if clock is not None else {}
        self.scheduler = Scheduler(self.store, executor, owner=owner, settings=self._setting, **kw)
        self.service = JobService(self.store, self.scheduler, settings=self._setting,
                                  node=self._node, setting_names=tuple(s.name for s in self.SETTINGS
                                                                       if s.name not in ("enabled", "db_path")),
                                  **kw)
        self.scheduler.alerts = self.service.alerts

    def bind_host(self, node) -> None:
        self._node = node
        if self.store is not None:
            self.wire(Runtime(node), owner=getattr(node, "worker_id", "") or "hub")

    async def start(self) -> None:
        if self.scheduler is not None and self._loop is None:
            self._loop = asyncio.get_running_loop().create_task(self.scheduler.run_forever())

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
            self._loop = None
        if self.scheduler is not None:
            await self.scheduler.stop()

    # -- caps ------------------------------------------------------------------------
    async def run(self, write: bool, action: str, rid, query, data):
        if self.service is None:
            raise ValueError("The jobs store is not open")
        return await self.service.dispatch(write, action, rid, query, data)

    @capability("read", risk="read")
    async def read(self, action: str = "list", id: str | None = None, query: str = "",
                   data: dict | None = None) -> dict:
        """Read jobs and runs: list|get|runs|run_get|next|validate|guardrails_preview|describe_schema.

        Same arguments and result as rook_jobs' read actions. id is a job id
        or name (a run id for run_get). describe_schema returns the job JSON
        schema; validate checks data (a job) without saving."""
        return await self.run(False, action, id, query, data)

    @capability("write", risk="write")
    async def write(self, action: str, id: str | None = None, data: dict | None = None) -> dict:
        """Write jobs: create|update|delete|enable|disable|run|cancel|set_guardrails|settings.

        Same arguments and result as rook_jobs' write actions. create data =
        the job; update id + data = fields to replace (data.revision checks
        for a concurrent edit); run id + data {vars}; cancel id = a run (or a
        job: all its runs)."""
        return await self.run(True, action, id, "", data)

    # -- MCP ---------------------------------------------------------------------------
    def mcp_tools(self, invoke):
        """The action-style ``rook_jobs`` tool, routed to ``job.read`` /
        ``job.write`` through ``invoke`` (the caller's identity)."""
        async def rook_jobs(action: str = "list", id: str | None = None, data: dict | None = None,
                            query: str = "") -> str:
            cap = route(action)
            args = {"action": action, "id": id, "data": data}
            if cap == "job.read":
                args["query"] = query
            try:
                return reply(await invoke(cap, args))
            except ERRORS as error:
                return error_reply(error)
        rook_jobs.__doc__ = (
            "Scheduled jobs on the hub. Reads: list|get|runs|run_get|next|validate|describe_schema. "
            "Writes: create|update|delete|enable|disable|run|cancel. Call describe_schema before writing "
            "a job. id: job id or name (run id for run_get/cancel). data: the job (create/validate), "
            "fields to change (update) or options (runs {states, steps:true}, run {vars}).")
        return [rook_jobs]


PLUGIN = Jobs
