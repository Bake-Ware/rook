"""Built-in job scheduler — cron-like recurring tasks persisted in SQLite."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from croniter import croniter

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    name: str
    prompt: str
    cron: str
    session_id: str
    enabled: bool
    last_run: float | None
    next_run: float
    created_at: float
    notify_channel: str | None  # Discord channel ID to post results
    one_shot: bool = False  # if True, auto-remove after first run


class Scheduler:
    """Cron-based job scheduler. Jobs stored in SQLite, executed via the agent."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._jobs: dict[str, Job] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._handler: Callable[[str, str, str | None], Awaitable[str]] | None = None
        self._recent_results: list[dict[str, Any]] = []  # last N job results for context
        self._max_recent = 5
        self._init_table()
        self._load_jobs()

    def _init_table(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS job_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                job_name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                cron TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT 'scheduler',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run REAL,
                next_run REAL NOT NULL,
                created_at REAL NOT NULL,
                notify_channel TEXT,
                one_shot INTEGER NOT NULL DEFAULT 0
            );
        """)
        self._db.commit()

    def _load_jobs(self) -> None:
        cursor = self._db.execute("SELECT * FROM scheduled_jobs WHERE enabled = 1")
        cursor.row_factory = sqlite3.Row
        for row in cursor.fetchall():
            job = Job(
                id=row["id"],
                name=row["name"],
                prompt=row["prompt"],
                cron=row["cron"],
                session_id=row["session_id"],
                enabled=bool(row["enabled"]),
                last_run=row["last_run"],
                next_run=row["next_run"],
                created_at=row["created_at"],
                notify_channel=row["notify_channel"],
                one_shot=bool(row["one_shot"]) if "one_shot" in row.keys() else False,
            )
            # Recalculate next_run if it's in the past
            now = time.time()
            if job.next_run < now:
                job.next_run = croniter(job.cron, now).get_next(float)
            self._jobs[job.id] = job

        log.info("Loaded %d scheduled jobs", len(self._jobs))

    def set_handler(self, handler: Callable[[str, str, str | None], Awaitable[str]]) -> None:
        """Set the async handler: (prompt, session_id, notify_channel) -> response."""
        self._handler = handler

    def add_job(
        self,
        name: str,
        prompt: str,
        cron: str | None = None,
        delay_seconds: int | None = None,
        session_id: str = "scheduler",
        notify_channel: str | None = None,
        one_shot: bool = False,
    ) -> Job:
        """Create and persist a new scheduled job.

        Either cron or delay_seconds must be provided.
        delay_seconds creates a one-shot job that fires after N seconds.
        """
        now = time.time()

        if delay_seconds is not None:
            # One-shot delayed job — no cron, just a future timestamp
            next_run = now + delay_seconds
            cron_expr = "* * * * *"  # placeholder, won't be used again
            one_shot = True
        elif cron:
            if not croniter.is_valid(cron):
                raise ValueError(f"Invalid cron expression: {cron}")
            cron_expr = cron
            next_run = croniter(cron, now).get_next(float)
        else:
            raise ValueError("Either cron or delay_seconds must be provided")

        job = Job(
            id=str(uuid.uuid4())[:8],
            name=name,
            prompt=prompt,
            cron=cron_expr,
            session_id=session_id,
            enabled=True,
            last_run=None,
            next_run=next_run,
            created_at=now,
            notify_channel=notify_channel,
            one_shot=one_shot,
        )

        self._db.execute(
            """INSERT INTO scheduled_jobs
               (id, name, prompt, cron, session_id, enabled, last_run, next_run, created_at, notify_channel, one_shot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job.id, job.name, job.prompt, job.cron, job.session_id,
             1, job.last_run, job.next_run, job.created_at, job.notify_channel, int(job.one_shot)),
        )
        self._db.commit()
        self._jobs[job.id] = job

        log.info("Job created: [%s] %s — next: %.0fs, one_shot: %s", job.id, job.name, next_run - now, one_shot)
        return job

    def remove_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._db.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))
            self._db.commit()
            log.info("Job removed: %s", job_id)
            return True
        return False

    def disable_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            self._jobs[job_id].enabled = False
            self._db.execute("UPDATE scheduled_jobs SET enabled = 0 WHERE id = ?", (job_id,))
            self._db.commit()
            return True
        return False

    def enable_job(self, job_id: str) -> bool:
        job_row = self._db.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
        if job_row:
            self._db.execute("UPDATE scheduled_jobs SET enabled = 1 WHERE id = ?", (job_id,))
            self._db.commit()
            self._load_jobs()  # reload
            return True
        return False

    def list_jobs(self) -> list[dict[str, Any]]:
        cursor = self._db.execute("SELECT * FROM scheduled_jobs ORDER BY created_at")
        cursor.row_factory = sqlite3.Row
        jobs = []
        for row in cursor.fetchall():
            jobs.append({
                "id": row["id"],
                "name": row["name"],
                "prompt": row["prompt"][:100],
                "cron": row["cron"],
                "enabled": bool(row["enabled"]),
                "last_run": row["last_run"],
                "next_run": row["next_run"],
                "notify_channel": row["notify_channel"],
            })
        return jobs

    def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Scheduler started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        log.info("Scheduler stopped")

    async def _loop(self) -> None:
        """Main scheduler loop — checks for due jobs every 10 seconds."""
        while self._running:
            try:
                now = time.time()
                for job in list(self._jobs.values()):
                    if not job.enabled or job.next_run > now:
                        continue

                    # Job is due
                    log.info("Running job [%s] %s", job.id, job.name)
                    asyncio.create_task(self._execute_job(job))

                    if job.one_shot:
                        # One-shot: remove after firing
                        log.info("One-shot job [%s] %s completed, removing", job.id, job.name)
                        self.remove_job(job.id)
                    else:
                        # Recurring: update schedule
                        job.last_run = now
                        job.next_run = croniter(job.cron, now).get_next(float)
                        self._db.execute(
                            "UPDATE scheduled_jobs SET last_run = ?, next_run = ? WHERE id = ?",
                            (job.last_run, job.next_run, job.id),
                        )
                        self._db.commit()

            except Exception as e:
                log.error("Scheduler loop error: %s", e)

            await asyncio.sleep(10)

    async def _execute_job(self, job: Job) -> None:
        """Execute a single job."""
        if not self._handler:
            log.error("No handler set for scheduler")
            return

        try:
            response = await self._handler(job.prompt, job.session_id, job.notify_channel)
            log.info("Job [%s] %s completed: %s", job.id, job.name, response[:100])

            # Store result
            now = time.time()
            self._db.execute(
                "INSERT INTO job_results (job_id, job_name, prompt, result, created_at) VALUES (?, ?, ?, ?, ?)",
                (job.id, job.name, job.prompt, response[:2000], now),
            )
            self._db.commit()

            # Keep in-memory recent buffer
            self._recent_results.append({
                "job_id": job.id,
                "name": job.name,
                "result": response[:500],
                "at": now,
            })
            if len(self._recent_results) > self._max_recent:
                self._recent_results = self._recent_results[-self._max_recent:]

        except Exception as e:
            log.error("Job [%s] %s failed: %s", job.id, job.name, e)

    def recent_results(self) -> list[dict[str, Any]]:
        """Return recent job results for context injection."""
        return list(self._recent_results)
