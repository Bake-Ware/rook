"""Sub-agent tools — spawn background agents for parallel work."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)


@dataclass
class SubAgent:
    id: str
    name: str
    prompt: str
    status: str = "running"  # running, completed, failed
    result: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    session_id: str = ""
    notify_channel: str | None = None


class AgentPool:
    """Manages spawned sub-agents running in the background."""

    def __init__(self):
        self._agents: dict[str, SubAgent] = {}
        self._handler: Callable[[str, str], Awaitable[str]] | None = None
        self._on_complete: Callable[[SubAgent], Awaitable[None]] | None = None

    def set_handler(self, handler: Callable[[str, str], Awaitable[str]]) -> None:
        """Set the async handler: (prompt, session_id) -> response."""
        self._handler = handler

    def set_on_complete(self, callback: Callable[[SubAgent], Awaitable[None]]) -> None:
        """Set callback fired when a sub-agent finishes."""
        self._on_complete = callback

    def spawn(self, name: str, prompt: str, notify_channel: str | None = None) -> SubAgent:
        agent = SubAgent(
            id=str(uuid.uuid4())[:8],
            name=name,
            prompt=prompt,
            session_id=f"agent:{str(uuid.uuid4())[:8]}",
        )
        agent.notify_channel = notify_channel
        self._agents[agent.id] = agent
        asyncio.create_task(self._run(agent))
        log.info("Spawned agent [%s] %s", agent.id, name)
        return agent

    async def _run(self, agent: SubAgent) -> None:
        if not self._handler:
            agent.status = "failed"
            agent.error = "No handler configured"
            return

        try:
            result = await self._handler(agent.prompt, agent.session_id)
            agent.result = result
            agent.status = "completed"
            agent.completed_at = time.time()
            elapsed = agent.completed_at - agent.started_at
            log.info("Agent [%s] %s completed in %.1fs", agent.id, agent.name, elapsed)
        except Exception as e:
            agent.status = "failed"
            agent.error = str(e)
            agent.completed_at = time.time()
            log.error("Agent [%s] %s failed: %s", agent.id, agent.name, e)

        # Fire completion callback
        if self._on_complete:
            try:
                await self._on_complete(agent)
            except Exception as e:
                log.error("Agent completion callback failed: %s", e)

    def get(self, agent_id: str) -> SubAgent | None:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[dict[str, Any]]:
        results = []
        for a in self._agents.values():
            results.append({
                "id": a.id,
                "name": a.name,
                "status": a.status,
                "prompt": a.prompt[:100],
                "result": a.result[:300] if a.result else "",
                "error": a.error,
                "elapsed": f"{a.completed_at - a.started_at:.1f}s" if a.completed_at else "running",
            })
        return results

    def recent_completed(self, n: int = 5) -> list[dict[str, Any]]:
        """Return the N most recently completed agents for context injection."""
        completed = [
            a for a in self._agents.values()
            if a.status in ("completed", "failed")
        ]
        completed.sort(key=lambda a: a.completed_at or 0, reverse=True)
        results = []
        for a in completed[:n]:
            results.append({
                "id": a.id,
                "name": a.name,
                "status": a.status,
                "result": a.result[:500] if a.result else a.error[:500],
                "at": a.completed_at,
            })
        return results

    def cleanup(self, max_age: float = 3600) -> int:
        """Remove completed agents older than max_age seconds."""
        now = time.time()
        to_remove = [
            aid for aid, a in self._agents.items()
            if a.status in ("completed", "failed")
            and a.completed_at
            and now - a.completed_at > max_age
        ]
        for aid in to_remove:
            del self._agents[aid]
        return len(to_remove)


class SpawnAgentTool(Tool):
    def __init__(self, pool: AgentPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="spawn_agent",
            description=(
                "Spawn a background sub-agent to work on a task independently. "
                "The agent gets its own conversation and runs your prompt through the full tool suite. "
                "Use this for: parallel research, long-running tasks, things you want done without blocking the conversation. "
                "Check results with check_agents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name for this agent task.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The full instruction for the sub-agent. Be specific — it has no context from this conversation.",
                    },
                    "notify_channel": {
                        "type": "string",
                        "description": "Discord channel ID to post results to when done. Use the current channel ID.",
                    },
                },
                "required": ["name", "prompt"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        prompt = kwargs.get("prompt", "")
        if not name or not prompt:
            return ToolResult(success=False, output="", error="name and prompt required")

        notify_channel = kwargs.get("notify_channel")
        agent = self.pool.spawn(name, prompt, notify_channel=notify_channel)
        return ToolResult(
            success=True,
            output=f"Agent spawned: [{agent.id}] {agent.name}\nRunning in background. Use check_agents to see results.",
        )


class CheckAgentsTool(Tool):
    def __init__(self, pool: AgentPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="check_agents",
            description=(
                "Check the status and results of spawned sub-agents. "
                "Returns all agents with their status (running/completed/failed) and results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Optional: check a specific agent by ID. Omit to see all.",
                    },
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        agent_id = kwargs.get("agent_id")

        if agent_id:
            agent = self.pool.get(agent_id)
            if not agent:
                return ToolResult(success=False, output="", error=f"Agent {agent_id} not found")
            result = {
                "id": agent.id,
                "name": agent.name,
                "status": agent.status,
                "prompt": agent.prompt,
                "result": agent.result,
                "error": agent.error,
            }
            return ToolResult(success=True, output=json.dumps(result, indent=2, default=str))

        agents = self.pool.list_agents()
        if not agents:
            return ToolResult(success=True, output="No agents spawned.")
        return ToolResult(success=True, output=json.dumps(agents, indent=2, default=str))
