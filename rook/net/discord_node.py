"""Rook Discord Node — Discord bot as a Telesthete network client.

A standalone process that connects to Discord and the Rook hub.
Messages from Discord → hub RPC (lookup, index, project, etc.)
Hub can also push notifications to Discord channels.

Run: rook discord
     python -m rook.net.discord_node
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import discord
from discord.ext import commands

from .client import RookClient
from .hub import (
    METHOD_LOOKUP, METHOD_INDEX, METHOD_PROJECT, METHOD_PROJECT_UPDATE,
    METHOD_LOG_CLI, METHOD_CACHE_WEB, METHOD_CLOUD_SEARCH, METHOD_CLOUD_READ,
    METHOD_REMEMBER, METHOD_RECALL, METHOD_STATS,
)
from .config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rook-discord] %(message)s",
)
log = logging.getLogger("rook-discord")

DISCORD_MAX = 2000
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def clean_response(text: str) -> str:
    text = _THINK_RE.sub("", text)
    # Strip CC stderr noise
    text = re.sub(r"---\s*STDERR\s*---.*", "", text, flags=re.DOTALL)
    text = re.sub(r"Warning: no stdin data.*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_message(text: str, limit: int = DISCORD_MAX) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(". ", 0, limit)
            if cut != -1:
                cut += 1
        if cut == -1 or cut == 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


def _format_lookup_discord(results: dict) -> str:
    """Format lookup results for Discord (markdown)."""
    lines = []

    if results.get("projects"):
        lines.append("**PROJECTS**")
        for p in results["projects"]:
            lines.append(f"**{p.get('name','')}** — {p.get('status','')}")
            for e in (p.get("recent_events") or [])[:3]:
                lines.append(f"  `{e.get('event_type','')}` {e.get('summary','')}")
        lines.append("")

    if results.get("concepts"):
        concepts = [c.get("name", "") for c in results["concepts"][:10]]
        lines.append(f"**CONCEPTS** {', '.join(f'`{c}`' for c in concepts)}")
        lines.append("")

    if results.get("sources"):
        lines.append("**SOURCES**")
        for s in results["sources"][:5]:
            title = s.get("title", s.get("location", ""))
            lines.append(f"  [{s.get('type','')}] {title}")
        lines.append("")

    if results.get("cli_history"):
        lines.append("**CLI HISTORY**")
        for h in results["cli_history"][:3]:
            lines.append(f"  {h.get('context','')}")
            if h.get("resolution"):
                lines.append(f"  → {h['resolution'][:100]}")
        lines.append("")

    if results.get("web_cache"):
        lines.append("**PAST SEARCHES**")
        for w in results["web_cache"][:3]:
            lines.append(f"  \"{w.get('query','')}\"")
        lines.append("")

    return "\n".join(lines) if lines else "No existing knowledge on this topic."


class DiscordNode:
    """Discord bot that participates in the Rook network."""

    def __init__(self, token: str, hub_url: str, open_channels: list[str] | None = None):
        self.token = token
        self.hub_url = hub_url
        self.open_channels = set(open_channels or [])
        self.client: RookClient | None = None

        # Discord bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self._setup_events()
        self._setup_commands()

    def _setup_events(self):
        bot = self.bot

        @bot.event
        async def on_ready():
            log.info("Discord connected as %s", bot.user)

            # Connect to hub
            self.client = RookClient(hub_url=self.hub_url)
            await self.client.start()
            if self.client._transport.connected:
                log.info("Connected to Rook hub at %s", self.hub_url)
                stats = await self.client.rpc(METHOD_STATS, {}, timeout=5)
                log.info("Hub graph: %s", stats)
            else:
                log.warning("Hub not reachable — running in offline mode")

        @bot.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return

            # Process commands first
            if message.content.startswith("!"):
                await bot.process_commands(message)
                return

            # Check if we should respond
            mentioned = bot.user and bot.user.mentioned_in(message)
            in_open_channel = str(message.channel.id) in self.open_channels

            if not mentioned and not in_open_channel:
                return

            text = message.content
            if bot.user:
                text = text.replace(f"<@{bot.user.id}>", "").strip()
            if not text:
                return

            # Index this conversation happening
            if self.client:
                await self.client.rpc(METHOD_INDEX, {
                    "concepts": self._extract_keywords(text),
                    "source_type": "discord",
                    "source_location": str(message.channel.id),
                    "source_title": f"Discord #{message.channel}",
                }, timeout=5)

            async with message.channel.typing():
                response = await self._handle_message(text, message)

            if not response:
                return

            response = clean_response(response)
            chunks = split_message(response)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)

    def _setup_commands(self):
        bot = self.bot

        @bot.command(name="lookup")
        async def cmd_lookup(ctx: commands.Context, *, query: str):
            """Look up a topic in the knowledge graph."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            async with ctx.typing():
                result = await self.client.rpc(METHOD_LOOKUP, {"query": query}, timeout=10)
            text = _format_lookup_discord(result) if isinstance(result, dict) else str(result)
            for chunk in split_message(text):
                await ctx.send(chunk)

        @bot.command(name="project")
        async def cmd_project(ctx: commands.Context, *, name: str = ""):
            """Show project status."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            result = await self.client.rpc(METHOD_PROJECT, {"project": name, "limit": 5}, timeout=10)
            if isinstance(result, list):
                lines = []
                for p in result:
                    lines.append(f"**{p.get('name','')}** — {p.get('status','')}")
                    for e in (p.get("recent_events") or [])[:5]:
                        lines.append(f"  `{e.get('event_type','')}` {e.get('summary','')}")
                    lines.append("")
                await ctx.send("\n".join(lines) or "No projects.")
            else:
                await ctx.send(str(result)[:2000])

        @bot.command(name="stats")
        async def cmd_stats(ctx: commands.Context):
            """Show hub graph statistics."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            stats = await self.client.rpc(METHOD_STATS, {}, timeout=5)
            if isinstance(stats, dict):
                lines = [f"**Rook Hub**"]
                for k, v in stats.items():
                    lines.append(f"  {k}: {v}")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send(str(stats))

        @bot.command(name="search")
        async def cmd_search(ctx: commands.Context, *, query: str):
            """Search cloud conversations."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            async with ctx.typing():
                results = await self.client.rpc(METHOD_CLOUD_SEARCH, {"query": query, "limit": 5}, timeout=15)
            if isinstance(results, list) and results:
                lines = [f"**{len(results)} results for** `{query}`\n"]
                for r in results[:5]:
                    if r.get("source") == "conversation":
                        lines.append(f"  [{r.get('convo_name','')}]")
                        lines.append(f"  {r.get('snippet','')[:150]}")
                    else:
                        lines.append(f"  [{r.get('project_name','')}/{r.get('file_name','')}]")
                        lines.append(f"  {r.get('snippet','')[:150]}")
                    lines.append("")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send(f"No results for `{query}`.")

        @bot.command(name="remember")
        async def cmd_remember(ctx: commands.Context, key: str, *, value: str):
            """Store a fact in shared memory."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            await self.client.rpc(METHOD_REMEMBER, {"key": key, "value": value}, timeout=5)
            await ctx.send(f"Stored `{key}`.")

        @bot.command(name="recall")
        async def cmd_recall(ctx: commands.Context, *, query: str = ""):
            """Search shared memory."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            rows = await self.client.rpc(METHOD_RECALL, {"query": query}, timeout=5)
            if isinstance(rows, list) and rows:
                lines = []
                for r in rows[:10]:
                    lines.append(f"**{r.get('key','')}** ({r.get('category','')}) — {r.get('value','')[:150]}")
                await ctx.send("\n".join(lines))
            else:
                await ctx.send("Nothing found." if query else "Empty.")

    async def _handle_message(self, text: str, message: discord.Message) -> str:
        """Handle a Discord message through the hub.

        Flow: lookup first → if enough context, respond using CC → index findings
        """
        if not self.client or not self.client._transport.connected:
            return "Hub offline — can't process messages right now."

        # 1. Lookup first — what does the graph know about this?
        keywords = self._extract_keywords(text)
        lookup_context = ""
        if keywords:
            for kw in keywords.split(",")[:3]:
                result = await self.client.rpc(METHOD_LOOKUP, {"query": kw.strip()}, timeout=5)
                if isinstance(result, dict):
                    formatted = _format_lookup_discord(result)
                    if formatted and "No existing knowledge" not in formatted:
                        lookup_context += f"\n{formatted}"

        # 2. Spawn a CC session to handle the message with context
        from .client import get_client
        from ..cli.cc_tmux import SessionManager

        mgr = SessionManager()
        prompt = text
        if lookup_context:
            prompt = f"Context from knowledge graph:\n{lookup_context}\n\nUser message: {text}"

        short = await mgr.spawn(prompt, print_output=False)

        # Wait for completion (up to 60s)
        for _ in range(120):
            await asyncio.sleep(0.5)
            session = mgr.get_session(short)
            if session and session["status"] != "running":
                break

        session = mgr.get_session(short)
        if not session:
            return "Failed to process message."

        from ..cli.cc_tmux import render_stream_json
        raw = mgr.read_output(short, tail=100)
        if raw:
            parts = []
            for line in raw.splitlines():
                # Skip stderr noise
                if line.startswith("--- STDERR") or "no stdin data" in line.lower() or "Warning:" in line:
                    continue
                t = render_stream_json(line, print_it=False)
                if t:
                    parts.append(t)
            response = "".join(parts).strip()
            if response:
                return response

        last = session.get("last_output", "")
        # Clean stderr from last_output too
        if last and "--- STDERR" in last:
            last = last.split("--- STDERR")[0].strip()
        return last or "No response."

    @staticmethod
    def _extract_keywords(text: str) -> str:
        """Quick keyword extraction from message text (no LLM needed)."""
        # Remove mentions, URLs, punctuation
        text = re.sub(r'<@\d+>', '', text)
        text = re.sub(r'https?://\S+', '', text)
        text = re.sub(r'[^\w\s]', ' ', text)

        # Keep words > 3 chars, lowercase, skip stop words
        stop = {"the", "and", "that", "this", "with", "from", "have", "been",
                "what", "where", "when", "how", "does", "will", "would", "could",
                "should", "about", "like", "just", "some", "more", "than", "them",
                "then", "also", "into", "your", "they", "very", "much"}
        words = [w.lower() for w in text.split() if len(w) > 3 and w.lower() not in stop]

        # Deduplicate preserving order
        seen = set()
        unique = []
        for w in words:
            if w not in seen:
                seen.add(w)
                unique.append(w)

        return ",".join(unique[:10])

    async def run(self):
        """Start the Discord bot."""
        log.info("Starting Discord node (hub: %s)", self.hub_url)
        await self.bot.start(self.token)


def main():
    import argparse
    from dotenv import load_dotenv

    # Load .env from rook project root
    env_path = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(env_path)

    parser = argparse.ArgumentParser(description="Rook Discord Node")
    parser.add_argument("--token-env", default="ROOK_DISCORD_TOKEN",
                        help="Environment variable for Discord bot token")
    parser.add_argument("--hub-url", default=None,
                        help="Hub WebSocket URL (default: from ~/.rook/net.json)")
    parser.add_argument("--channels", nargs="*", default=None,
                        help="Open channel IDs (respond without mention)")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        log.error("%s not set. Set it in .env or environment.", args.token_env)
        sys.exit(1)

    config = load_config()
    hub_url = args.hub_url or config.get("hub_url", "ws://localhost:7006/band")

    # Open channels from args or config
    channels = args.channels
    if not channels:
        # Try to read from rook config.yaml
        try:
            import yaml
            cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                channels = cfg.get("discord", {}).get("open_channels", [])
        except Exception:
            pass

    node = DiscordNode(token=token, hub_url=hub_url, open_channels=channels or [])
    asyncio.run(node.run())


if __name__ == "__main__":
    main()
