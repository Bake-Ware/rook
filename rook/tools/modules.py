"""Module management tools — list, create, start, stop modules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

CUSTOM_MODULES_DIR = Path("./data/modules")


class ListModulesTool(Tool):
    def __init__(self, loader):
        self.loader = loader

    def definition(self) -> ToolDef:
        return ToolDef(
            name="list_modules",
            description="List all loaded modules with their status.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        modules = self.loader.list_modules()
        if not modules:
            return ToolResult(success=True, output="No modules loaded.")
        return ToolResult(success=True, output=json.dumps(modules, indent=2))


class CreateModuleTool(Tool):
    def __init__(self, loader, agent):
        self.loader = loader
        self.agent = agent

    def definition(self) -> ToolDef:
        return ToolDef(
            name="create_module",
            description=(
                "Create a new module — a persistent capability that runs alongside the agent. "
                "Modules can be channels (new ways to communicate), services (background tasks), "
                "or integrations (connecting to external systems). "
                "Provide the full Python source. Must define MODULE_NAME, MODULE_DESCRIPTION, "
                "MODULE_TYPE, and async start(agent, config) / stop() functions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Module name (snake_case).",
                    },
                    "code": {
                        "type": "string",
                        "description": "Full Python source code for the module.",
                    },
                },
                "required": ["name", "code"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "").strip().lower().replace(" ", "_")
        code = kwargs.get("code", "")

        if not name or not code:
            return ToolResult(success=False, output="", error="name and code required")

        CUSTOM_MODULES_DIR.mkdir(parents=True, exist_ok=True)
        path = CUSTOM_MODULES_DIR / f"{name}.py"
        path.write_text(code, encoding="utf-8")

        # Try to load it immediately
        try:
            from ..modules.loader import ModuleLoader
            from ..core.config import Config
            await self.loader._load_module(path, self.agent, self.agent.config, builtin=False)
            return ToolResult(success=True, output=f"Module '{name}' created and started.")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Module saved but failed to start: {e}")
