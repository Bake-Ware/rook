"""Custom tool loader — lets the model write and register its own tools at runtime."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

CUSTOM_TOOLS_DIR = Path("./data/custom_tools")


class CustomTool(Tool):
    """Wraps a dynamically loaded tool module."""

    def __init__(self, name: str, description: str, parameters: dict, execute_fn):
        self._name = name
        self._description = description
        self._parameters = parameters
        self._execute_fn = execute_fn

    def definition(self) -> ToolDef:
        return ToolDef(
            name=self._name,
            description=self._description,
            parameters=self._parameters,
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = self._execute_fn(**kwargs)
            # Support both sync and async
            if hasattr(result, '__await__'):
                result = await result
            if isinstance(result, ToolResult):
                return result
            return ToolResult(success=True, output=str(result))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


def load_custom_tools(registry) -> int:
    """Load all custom tools from the tools directory. Returns count loaded."""
    CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0

    for f in CUSTOM_TOOLS_DIR.glob("*.py"):
        try:
            tool = _load_tool_file(f)
            if tool:
                registry.register(tool)
                count += 1
                log.info("Loaded custom tool: %s", tool.definition().name)
        except Exception as e:
            log.error("Failed to load custom tool %s: %s", f.name, e)

    return count


def _load_tool_file(path: Path) -> CustomTool | None:
    """Load a single tool from a Python file.

    The file must define:
        TOOL_NAME: str
        TOOL_DESCRIPTION: str
        TOOL_PARAMETERS: dict (JSON Schema)
        def run(**kwargs) -> str:
    """
    spec = importlib.util.spec_from_file_location(f"custom_tool_{path.stem}", path)
    if not spec or not spec.loader:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    name = getattr(module, "TOOL_NAME", None)
    description = getattr(module, "TOOL_DESCRIPTION", None)
    parameters = getattr(module, "TOOL_PARAMETERS", {"type": "object", "properties": {}})
    run_fn = getattr(module, "run", None)

    if not all([name, description, run_fn]):
        log.warning("Custom tool %s missing TOOL_NAME, TOOL_DESCRIPTION, or run()", path.name)
        return None

    return CustomTool(name, description, parameters, run_fn)


class CreateToolTool(Tool):
    """Meta-tool: lets the model create new tools."""

    def __init__(self, registry):
        self.registry = registry

    def definition(self) -> ToolDef:
        return ToolDef(
            name="create_tool",
            description=(
                "Create a new custom tool by writing Python code. The tool persists across restarts. "
                "Provide the tool name, description, parameters (JSON Schema), and the Python code for the run() function. "
                "The run function receives kwargs matching the parameters and should return a string result. "
                "You have access to standard library, subprocess, aiohttp, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Tool name (snake_case, e.g. 'ping_host').",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this tool does.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "JSON Schema for the tool's parameters.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Python code for the run(**kwargs) function body. Will be wrapped in 'def run(**kwargs):'.",
                    },
                },
                "required": ["name", "description", "code"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        description = kwargs.get("description", "")
        parameters = kwargs.get("parameters", {"type": "object", "properties": {}})
        code = kwargs.get("code", "")

        if not name or not code:
            return ToolResult(success=False, output="", error="name and code required")

        # Sanitize name
        name = name.replace(" ", "_").replace("-", "_").lower()

        # Build the tool file
        params_json = json.dumps(parameters, indent=4)
        file_content = f'''"""Custom tool: {name} — {description}"""

TOOL_NAME = "{name}"
TOOL_DESCRIPTION = """{description}"""
TOOL_PARAMETERS = {params_json}


def run(**kwargs):
{_indent(code, 4)}
'''

        # Write to file
        CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        tool_path = CUSTOM_TOOLS_DIR / f"{name}.py"
        tool_path.write_text(file_content, encoding="utf-8")

        # Load and register immediately
        try:
            tool = _load_tool_file(tool_path)
            if tool:
                self.registry.register(tool)
                log.info("Created and registered custom tool: %s", name)
                return ToolResult(success=True, output=f"Tool '{name}' created and active.")
            else:
                return ToolResult(success=False, output="", error="Tool file written but failed to load")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Tool created but failed to load: {e}")


class DeleteToolTool(Tool):
    """Delete a custom tool."""

    def __init__(self, registry):
        self.registry = registry

    def definition(self) -> ToolDef:
        return ToolDef(
            name="delete_tool",
            description="Delete a custom tool by name.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to delete."},
                },
                "required": ["name"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        if not name:
            return ToolResult(success=False, output="", error="name required")

        tool_path = CUSTOM_TOOLS_DIR / f"{name}.py"
        if tool_path.exists():
            tool_path.unlink()
            # Remove from registry
            if name in self.registry._tools:
                del self.registry._tools[name]
            return ToolResult(success=True, output=f"Tool '{name}' deleted.")
        return ToolResult(success=False, output="", error=f"Tool '{name}' not found")


def _indent(code: str, spaces: int) -> str:
    """Indent code block."""
    prefix = " " * spaces
    lines = code.split("\n")
    return "\n".join(prefix + line if line.strip() else line for line in lines)
