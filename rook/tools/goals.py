"""Goal and plan tools — set objectives, track progress, self-stimulate."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Tool, ToolDef, ToolResult
from ..memory.goals import GoalStore

log = logging.getLogger(__name__)


class SetGoalTool(Tool):
    def __init__(self, store: GoalStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="set_goal",
            description=(
                "Set a goal with a step-by-step plan. You will automatically work through "
                "each step until the goal is complete. Use this for any multi-step task. "
                "Each step should be a concrete, actionable item."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The goal."},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of steps to achieve the goal.",
                    },
                },
                "required": ["title", "steps"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = kwargs.get("title", "")
        steps = kwargs.get("steps", [])
        if not title or not steps:
            return ToolResult(success=False, output="", error="title and steps required")

        goal = self.store.create(title, steps)
        return ToolResult(success=True, output=f"Goal set: [{goal.id}] {title}\n{goal.render()}")


class CompleteStepTool(Tool):
    def __init__(self, store: GoalStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="complete_step",
            description=(
                "Mark the current/next step of the active goal as done. "
                "Optionally provide a result summary. "
                "The system will automatically move to the next step."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "Brief result or outcome of this step."},
                    "goal_id": {"type": "string", "description": "Goal ID (omit to use the active goal)."},
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        goal_id = kwargs.get("goal_id", "")
        result = kwargs.get("result", "")

        if not goal_id:
            goal = self.store.get_active()
            if not goal:
                return ToolResult(success=False, output="", error="No active goal")
            goal_id = goal.id

        msg = self.store.complete_step(goal_id, result=result)
        return ToolResult(success=True, output=msg)


class UpdatePlanTool(Tool):
    def __init__(self, store: GoalStore):
        self.store = store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="update_plan",
            description=(
                "Replace the remaining steps of the active goal with new ones. "
                "Use this when the plan needs to change based on what you've learned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New steps to replace the remaining incomplete ones.",
                    },
                    "goal_id": {"type": "string", "description": "Goal ID (omit to use the active goal)."},
                },
                "required": ["steps"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        goal_id = kwargs.get("goal_id", "")
        steps = kwargs.get("steps", [])

        if not goal_id:
            goal = self.store.get_active()
            if not goal:
                return ToolResult(success=False, output="", error="No active goal")
            goal_id = goal.id

        msg = self.store.update_plan(goal_id, steps)
        return ToolResult(success=True, output=msg)
