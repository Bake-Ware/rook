"""Memory kernel tools — promote, demote, search, status."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Tool, ToolDef, ToolResult
from ..memory.facts import FactStore

log = logging.getLogger(__name__)


class MemoryPromoteTool(Tool):
    def __init__(self, fact_store: FactStore):
        self.fact_store = fact_store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="memory_promote",
            description=(
                "Promote a memory fact to the next tier (volatile→working→concrete). "
                "Use this when you recognize something as important and want to keep it longer. "
                "Search by fact ID or keyword."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string", "description": "The fact ID to promote."},
                    "keyword": {"type": "string", "description": "Keyword to match against fact text."},
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        result = self.fact_store.promote(
            fact_id=kwargs.get("fact_id"),
            keyword=kwargs.get("keyword"),
        )
        self.fact_store.flush_to_db()
        return ToolResult(success=True, output=result)


class MemoryDemoteTool(Tool):
    def __init__(self, fact_store: FactStore):
        self.fact_store = fact_store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="memory_demote",
            description=(
                "Demote a memory fact to a lower tier or archive it to disk. "
                "Use this to free space or remove outdated information."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string", "description": "The fact ID to demote."},
                    "keyword": {"type": "string", "description": "Keyword to match against fact text."},
                },
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        result = self.fact_store.demote(
            fact_id=kwargs.get("fact_id"),
            keyword=kwargs.get("keyword"),
        )
        self.fact_store.flush_to_db()
        return ToolResult(success=True, output=result)


class MemorySearchTool(Tool):
    def __init__(self, fact_store: FactStore):
        self.fact_store = fact_store

    def definition(self) -> ToolDef:
        return ToolDef(
            name="memory_search",
            description=(
                "Search across all memory tiers (volatile, working, concrete) and archived facts on disk. "
                "Use this to find information from past conversations."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term to match against facts."},
                },
                "required": ["query"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        if not query:
            return ToolResult(success=False, output="", error="query required")

        results = self.fact_store.search(query)
        if not results:
            return ToolResult(success=True, output="No matching facts found.")

        output = json.dumps(results, indent=2, default=str)
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncated)"
        return ToolResult(success=True, output=output)


class ContextStatusTool(Tool):
    def __init__(self, fact_store: FactStore, get_conversation_info: Any = None):
        self.fact_store = fact_store
        self._get_conv_info = get_conversation_info

    def definition(self) -> ToolDef:
        return ToolDef(
            name="context_status",
            description=(
                "Get a detailed breakdown of memory allocation across all tiers. "
                "Shows token usage, fact counts, and context pressure level."
            ),
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        status = self.fact_store.status()

        output = {
            "tiers": status,
            "tier_budget": self.fact_store.tier_size,
        }

        # Add archived count
        try:
            cursor = self.fact_store._db.execute(
                "SELECT COUNT(*) as cnt FROM memory_facts WHERE tier = 'archived'"
            )
            row = cursor.fetchone()
            output["archived_count"] = row[0] if row else 0
        except Exception:
            output["archived_count"] = "unknown"

        return ToolResult(success=True, output=json.dumps(output, indent=2))
