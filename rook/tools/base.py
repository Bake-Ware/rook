"""Base tool interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Result from a tool execution."""

    success: bool
    output: str
    error: str | None = None


@dataclass
class ToolDef:
    """Tool definition for the LLM."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class for all tools."""

    @abstractmethod
    def definition(self) -> ToolDef:
        """Return the tool definition for the LLM."""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...
