"""Shell execution tool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

MAX_OUTPUT = 4000  # chars to return to the LLM


class ShellTool(Tool):
    def definition(self) -> ToolDef:
        return ToolDef(
            name="shell",
            description="Execute a shell command and return its output. Use for running programs, scripts, git commands, system operations, etc.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30).",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout", 30)

        if not command:
            return ToolResult(success=False, output="", error="No command provided")

        log.info("shell: %s", command)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(
                success=False, output="", error=f"Command timed out after {timeout}s"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        # Truncate if too long
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n... (truncated, {len(out)} total chars)"

        if proc.returncode != 0:
            return ToolResult(
                success=False,
                output=out,
                error=f"Exit code {proc.returncode}: {err[:1000]}",
            )

        return ToolResult(success=True, output=out, error=err if err else None)
