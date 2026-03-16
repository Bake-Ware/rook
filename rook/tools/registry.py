"""Tool registry — collects all tools and provides them to the LLM."""

from __future__ import annotations

import logging
from typing import Any

from .base import Tool, ToolResult
from .shell import ShellTool
from .files import ReadFileTool, WriteFileTool, ListDirTool
from .web import WebSearchTool, WebFetchTool
from .memory import MemoryStore, SQLQueryTool, GraphQueryTool, GraphStoreTool, RememberTool, RecallTool
from .memory_kernel import MemoryPromoteTool, MemoryDemoteTool, MemorySearchTool, ContextStatusTool
from ..memory.facts import FactStore
from ..scheduler import Scheduler
from .scheduler_tools import ScheduleJobTool, ListJobsTool, RemoveJobTool
from .agents import AgentPool, SpawnAgentTool, CheckAgentsTool
from .terminals import TerminalPool, TerminalCreateTool, TerminalSendTool, TerminalReadTool, TerminalListTool, TerminalKillTool
from .remote import RemoteExecTool, RemoteListTool, RemoteUpdateTool, RemoteUninstallTool
from .channels import ChannelBridge, SendMessageTool, ListChannelsTool
from .custom import CreateToolTool, DeleteToolTool, load_custom_tools
from ..remote.bootstrap import CombinedServer

log = logging.getLogger(__name__)


class ToolRegistry:
    """Manages available tools and converts them to OpenAI function-calling format."""

    def __init__(
        self,
        searxng_url: str = "https://searxng.bake.systems",
        sqlite_path: str = "./data/rook.db",
        graph_path: str = "./data/knowledge",
        tier_size: int = 8000,
        remote_port: int = 7005,
        remote_auth_token: str = "",
        remote_domain: str = "rook.bake.systems",
        remote_web_user: str = "",
        remote_web_pass: str = "",
        promote_threshold: int = 3,
        concrete_threshold: int = 6,
    ):
        self._tools: dict[str, Tool] = {}

        # Core tools (always available)
        self.register(ShellTool())
        self.register(ReadFileTool())
        self.register(WriteFileTool())
        self.register(ListDirTool())
        self.register(WebSearchTool(searxng_url))
        self.register(WebFetchTool())

        # Memory backend
        self.memory_store = MemoryStore(sqlite_path, graph_path)
        self.fact_store = FactStore(
            self.memory_store._db,
            tier_size=tier_size,
            promote_threshold=promote_threshold,
            concrete_threshold=concrete_threshold,
        )

        # Memory tools (consolidated — remember, recall, promote/demote via remember)
        self.register(RememberTool(self.memory_store, self.fact_store))
        self.register(RecallTool(self.memory_store))
        self.register(MemorySearchTool(self.fact_store))

        # Scheduler
        self.scheduler = Scheduler(self.memory_store._db)
        self.register(ScheduleJobTool(self.scheduler))
        self.register(ListJobsTool(self.scheduler))
        self.register(RemoveJobTool(self.scheduler))

        # Sub-agents
        self.agent_pool = AgentPool()
        self.register(SpawnAgentTool(self.agent_pool))

        # Remote workers
        self.remote_server = CombinedServer(
            port=remote_port, auth_token=remote_auth_token or "",
            domain=remote_domain, web_user=remote_web_user, web_pass=remote_web_pass,
        )
        self.register(RemoteExecTool(self.remote_server))
        self.register(RemoteListTool(self.remote_server))
        self.register(RemoteUpdateTool(self.remote_server))

        # Cross-channel
        self.channel_bridge = ChannelBridge()
        self.register(SendMessageTool(self.channel_bridge, self.memory_store))
        self.register(ListChannelsTool(self.memory_store))

        # Advanced tools (available but not in main list — model can use db_query, graph, terminals via shell/write_file)
        self._advanced_tools: dict[str, Tool] = {}
        for tool_cls in [SQLQueryTool, GraphQueryTool, GraphStoreTool]:
            t = tool_cls(self.memory_store)
            self._advanced_tools[t.definition().name] = t
        self._advanced_tools["memory_promote"] = MemoryPromoteTool(self.fact_store)
        self._advanced_tools["memory_demote"] = MemoryDemoteTool(self.fact_store)
        self._advanced_tools["context_status"] = ContextStatusTool(self.fact_store)
        self._advanced_tools["check_agents"] = CheckAgentsTool(self.agent_pool)
        self._advanced_tools["remote_uninstall"] = RemoteUninstallTool(self.remote_server)

        # Terminal tools (advanced)
        self.terminal_pool = TerminalPool()
        for t_cls in [TerminalCreateTool, TerminalSendTool, TerminalReadTool, TerminalListTool, TerminalKillTool]:
            t = t_cls(self.terminal_pool)
            self._advanced_tools[t.definition().name] = t

        # Custom tools
        self.register(CreateToolTool(self))
        loaded = load_custom_tools(self)
        if loaded:
            log.info("Loaded %d custom tools", loaded)

    def register(self, tool: Tool) -> None:
        defn = tool.definition()
        self._tools[defn.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name) or self._advanced_tools.get(name)
        if not tool:
            return ToolResult(success=False, output="", error=f"Unknown tool: {name}")
        try:
            return await tool.execute(**arguments)
        except Exception as e:
            log.exception("Tool %s failed", name)
            return ToolResult(success=False, output="", error=str(e))

    def openai_tools(self) -> list[dict[str, Any]]:
        tools = []
        for name, tool in self._tools.items():
            defn = tool.definition()
            tools.append({
                "type": "function",
                "function": {
                    "name": defn.name,
                    "description": defn.description,
                    "parameters": defn.parameters,
                },
            })
        return tools

    def list_names(self) -> list[str]:
        return list(self._tools.keys())
