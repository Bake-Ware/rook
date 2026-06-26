"""File read/write tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

MAX_READ = 8000  # chars to return


class ReadFileTool(Tool):
    def definition(self) -> ToolDef:
        return ToolDef(
            name="read_file",
            description="Read the contents of a file. Returns the text content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path to read.",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        path_str = kwargs.get("path", "")
        if not path_str:
            return ToolResult(success=False, output="", error="No path provided")

        path = Path(path_str).expanduser()
        log.info("read_file: %s", path)

        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        if len(content) > MAX_READ:
            content = content[:MAX_READ] + f"\n... (truncated, {len(content)} total chars)"

        return ToolResult(success=True, output=content)


class WriteFileTool(Tool):
    def definition(self) -> ToolDef:
        return ToolDef(
            name="write_file",
            description="Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        path_str = kwargs.get("path", "")
        content = kwargs.get("content", "")

        if not path_str:
            return ToolResult(success=False, output="", error="No path provided")

        path = Path(path_str).expanduser()
        log.info("write_file: %s (%d chars)", path, len(content))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        return ToolResult(success=True, output=f"Wrote {len(content)} chars to {path}")


class ListDirTool(Tool):
    def definition(self) -> ToolDef:
        return ToolDef(
            name="list_dir",
            description="List files and directories at a given path.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list.",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        path_str = kwargs.get("path", ".")
        path = Path(path_str).expanduser()

        if not path.exists():
            return ToolResult(success=False, output="", error=f"Not found: {path}")
        if not path.is_dir():
            return ToolResult(success=False, output="", error=f"Not a directory: {path}")

        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines = []
            for entry in entries[:200]:
                prefix = "d " if entry.is_dir() else "f "
                lines.append(f"{prefix}{entry.name}")
            output = "\n".join(lines)
            if len(entries) > 200:
                output += f"\n... ({len(entries)} total entries)"
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        return ToolResult(success=True, output=output)
