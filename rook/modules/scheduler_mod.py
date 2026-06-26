"""Scheduler module — cron-based job scheduling."""

from __future__ import annotations

from typing import Any

MODULE_NAME = "scheduler"
MODULE_DESCRIPTION = "Cron-based job scheduler with one-shot timers"
MODULE_TYPE = "service"

_scheduler = None


async def start(agent: Any, config: Any) -> None:
    global _scheduler
    _scheduler = agent.scheduler
    _scheduler.start()


async def stop() -> None:
    if _scheduler:
        _scheduler.stop()
