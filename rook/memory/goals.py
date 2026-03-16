"""Goal and plan tracking — persistent objectives with self-stimulation."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PlanStep:
    id: str
    description: str
    done: bool = False
    result: str = ""


@dataclass
class Goal:
    id: str
    title: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "active"  # active, completed, failed, paused
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for s in self.steps if s.done)
        return done, len(self.steps)

    @property
    def next_step(self) -> PlanStep | None:
        for s in self.steps:
            if not s.done:
                return s
        return None

    @property
    def is_complete(self) -> bool:
        return all(s.done for s in self.steps) and len(self.steps) > 0

    def render(self) -> str:
        done, total = self.progress
        lines = [f"GOAL: {self.title} [{self.status}] ({done}/{total})"]
        for s in self.steps:
            mark = "x" if s.done else " "
            result = f" — {s.result[:80]}" if s.result else ""
            lines.append(f"  [{mark}] {s.description}{result}")
        return "\n".join(lines)


class GoalStore:
    """Manages goals with SQLite persistence."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._goals: dict[str, Goal] = {}
        self._init_table()
        self._load()

    def _init_table(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                steps_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                session_id TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL
            );
        """)
        self._db.commit()

    def _load(self) -> None:
        cursor = self._db.execute("SELECT * FROM goals WHERE status = 'active'")
        cursor.row_factory = sqlite3.Row
        for row in cursor.fetchall():
            steps_data = json.loads(row["steps_json"])
            steps = [PlanStep(id=s["id"], description=s["description"],
                              done=s.get("done", False), result=s.get("result", ""))
                     for s in steps_data]
            goal = Goal(
                id=row["id"],
                title=row["title"],
                steps=steps,
                status=row["status"],
                session_id=row["session_id"] or "",
                created_at=row["created_at"] or time.time(),
                updated_at=row["updated_at"] or time.time(),
            )
            self._goals[goal.id] = goal
        log.info("Loaded %d active goals", len(self._goals))

    def _save(self, goal: Goal) -> None:
        goal.updated_at = time.time()
        steps_json = json.dumps([
            {"id": s.id, "description": s.description, "done": s.done, "result": s.result}
            for s in goal.steps
        ])
        self._db.execute(
            """INSERT OR REPLACE INTO goals
               (id, title, steps_json, status, session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (goal.id, goal.title, steps_json, goal.status,
             goal.session_id, goal.created_at, goal.updated_at),
        )
        self._db.commit()

    def create(self, title: str, steps: list[str], session_id: str = "") -> Goal:
        goal = Goal(
            id=str(uuid.uuid4())[:8],
            title=title,
            steps=[PlanStep(id=str(uuid.uuid4())[:6], description=s) for s in steps],
            session_id=session_id,
        )
        self._goals[goal.id] = goal
        self._save(goal)
        log.info("Goal created: [%s] %s (%d steps)", goal.id, title, len(steps))
        return goal

    def complete_step(self, goal_id: str, step_id: str | None = None,
                      step_index: int | None = None, result: str = "") -> str:
        """Mark a step as done. Use step_id or step_index."""
        goal = self._goals.get(goal_id)
        if not goal:
            return "Goal not found"

        step = None
        if step_id:
            step = next((s for s in goal.steps if s.id == step_id), None)
        elif step_index is not None and 0 <= step_index < len(goal.steps):
            step = goal.steps[step_index]
        else:
            # Complete the next incomplete step
            step = goal.next_step

        if not step:
            return "No step to complete"

        step.done = True
        step.result = result

        # Check if goal is fully complete
        if goal.is_complete:
            goal.status = "completed"
            log.info("Goal completed: [%s] %s", goal.id, goal.title)

        self._save(goal)
        done, total = goal.progress
        return f"Step done ({done}/{total}): {step.description}"

    def update_plan(self, goal_id: str, new_steps: list[str]) -> str:
        """Replace remaining steps with new ones."""
        goal = self._goals.get(goal_id)
        if not goal:
            return "Goal not found"

        # Keep completed steps, replace incomplete ones
        completed = [s for s in goal.steps if s.done]
        new = [PlanStep(id=str(uuid.uuid4())[:6], description=s) for s in new_steps]
        goal.steps = completed + new
        self._save(goal)
        return f"Plan updated: {len(completed)} done, {len(new)} new steps"

    def fail_goal(self, goal_id: str, reason: str = "") -> str:
        goal = self._goals.get(goal_id)
        if not goal:
            return "Goal not found"
        goal.status = "failed"
        self._save(goal)
        return f"Goal failed: {goal.title}" + (f" — {reason}" if reason else "")

    def pause_goal(self, goal_id: str) -> str:
        goal = self._goals.get(goal_id)
        if not goal:
            return "Goal not found"
        goal.status = "paused"
        self._save(goal)
        return f"Goal paused: {goal.title}"

    def resume_goal(self, goal_id: str) -> str:
        goal = self._goals.get(goal_id)
        if not goal:
            return "Goal not found"
        goal.status = "active"
        self._save(goal)
        self._goals[goal_id] = goal
        return f"Goal resumed: {goal.title}"

    def get_active(self, session_id: str | None = None) -> Goal | None:
        """Get the active goal, optionally filtered by session."""
        for g in self._goals.values():
            if g.status == "active":
                if session_id is None or g.session_id == session_id or g.session_id == "":
                    return g
        return None

    def list_goals(self) -> list[dict]:
        results = []
        # Include active from memory, all from DB
        cursor = self._db.execute("SELECT * FROM goals ORDER BY updated_at DESC LIMIT 20")
        cursor.row_factory = sqlite3.Row
        for row in cursor.fetchall():
            done_count = sum(1 for s in json.loads(row["steps_json"]) if s.get("done"))
            total = len(json.loads(row["steps_json"]))
            results.append({
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "progress": f"{done_count}/{total}",
            })
        return results

    def render_active(self) -> str:
        """Render all active goals for system prompt injection."""
        active = [g for g in self._goals.values() if g.status == "active"]
        if not active:
            return ""
        return "\n".join(g.render() for g in active)
