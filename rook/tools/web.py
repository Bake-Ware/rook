"""Web search and fetch tools using SearXNG."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

MAX_FETCH = 6000  # chars to return from a page


class WebSearchTool(Tool):
    def __init__(self, searxng_url: str = "https://searxng.bake.systems"):
        self.searxng_url = searxng_url.rstrip("/")

    def definition(self) -> ToolDef:
        return ToolDef(
            name="web_search",
            description="Search the web using SearXNG. Returns a list of results with titles, URLs, and snippets.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        num = kwargs.get("num_results", 5)

        if not query:
            return ToolResult(success=False, output="", error="No query provided")

        log.info("web_search: %s", query)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.searxng_url}/search",
                    params={"q": query, "format": "json", "engines": "bing,mojeek"},
                    headers={"Accept-Encoding": "gzip, deflate"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return ToolResult(
                            success=False,
                            output="",
                            error=f"SearXNG returned {resp.status}",
                        )
                    data = await resp.json()
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        results = data.get("results", [])[:num]
        if not results:
            return ToolResult(success=True, output="No results found.")

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = r.get("content", "")
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}")

        return ToolResult(success=True, output="\n\n".join(lines))


class WebFetchTool(Tool):
    def definition(self) -> ToolDef:
        return ToolDef(
            name="web_fetch",
            description="Fetch the text content of a web page. Returns the page body as plain text.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url", "")
        if not url:
            return ToolResult(success=False, output="", error="No URL provided")

        log.info("web_fetch: %s", url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "Rook/0.1", "Accept-Encoding": "gzip, deflate"},
                ) as resp:
                    if resp.status != 200:
                        return ToolResult(
                            success=False,
                            output="",
                            error=f"HTTP {resp.status}",
                        )
                    text = await resp.text()
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        # Basic HTML stripping — just get text content
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > MAX_FETCH:
            text = text[:MAX_FETCH] + f"\n... (truncated, {len(text)} total chars)"

        return ToolResult(success=True, output=text)
