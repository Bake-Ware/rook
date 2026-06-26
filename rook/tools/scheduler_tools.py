"""Scheduler tools — let the model create, list, and manage cron jobs."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Tool, ToolDef, ToolResult
from ..scheduler import Scheduler

log = logging.getLogger(__name__)


class ScheduleJobTool(Tool):
    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    def definition(self) -> ToolDef:
        return ToolDef(
            name="schedule_job",
            description=(
                "Create a scheduled job. Provide either cron for recurring, or delay_seconds for a one-shot timer. "
                "Cron format: 'minute hour day month weekday' (e.g., '*/30 * * * *' = every 30 min, "
                "'0 9 * * 1-5' = 9am weekdays). "
                "delay_seconds: run once after N seconds (e.g., 300 = 5 minutes). "
                "Set notify_channel to a Discord channel ID to post results there."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name for this job.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt/instruction to execute when the job fires.",
                    },
                    "cron": {
                        "type": "string",
                        "description": "Cron expression for recurring jobs (5 fields: min hour day month weekday).",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "description": "Seconds from now for a one-shot job. Use instead of cron for timers/reminders.",
                    },
                    "notify_channel": {
                        "type": "string",
                        "description": "Discord channel ID to post results to.",
                    },
                },
                "required": ["name", "prompt"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        prompt = kwargs.get("prompt", "")
        cron = kwargs.get("cron")
        delay_seconds = kwargs.get("delay_seconds")
        notify_channel = kwargs.get("notify_channel")

        if not name or not prompt:
            return ToolResult(success=False, output="", error="name and prompt are required")
        if not cron and delay_seconds is None:
            return ToolResult(success=False, output="", error="Either cron or delay_seconds is required")

        try:
            job = self.scheduler.add_job(
                name=name,
                prompt=prompt,
                cron=cron,
                delay_seconds=int(delay_seconds) if delay_seconds is not None else None,
                notify_channel=notify_channel,
            )
            if job.one_shot:
                mins = int((job.next_run - job.created_at) / 60)
                return ToolResult(
                    success=True,
                    output=f"One-shot job created: [{job.id}] {job.name}\nFires in {mins} minutes.",
                )
            return ToolResult(
                success=True,
                output=f"Recurring job created: [{job.id}] {job.name}\nCron: {job.cron}",
            )
        except ValueError as e:
            return ToolResult(success=False, output="", error=str(e))


class ListJobsTool(Tool):
    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    def definition(self) -> ToolDef:
        return ToolDef(
            name="list_jobs",
            description="List all scheduled jobs with their status, cron schedule, and next run time.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        jobs = self.scheduler.list_jobs()
        if not jobs:
            return ToolResult(success=True, output="No scheduled jobs.")
        return ToolResult(success=True, output=json.dumps(jobs, indent=2, default=str))


class RemoveJobTool(Tool):
    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remove_job",
            description="Remove a scheduled job by ID.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The job ID to remove."},
                },
                "required": ["job_id"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        job_id = kwargs.get("job_id", "")
        if self.scheduler.remove_job(job_id):
            return ToolResult(success=True, output=f"Job {job_id} removed.")
        return ToolResult(success=False, output="", error=f"Job {job_id} not found.")
