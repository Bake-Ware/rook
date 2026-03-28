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

        # Persistent CC subprocesses per channel — full interactive mode
        self._channel_procs: dict[str, asyncio.subprocess.Process] = {}
        self._channel_locks: dict[str, asyncio.Lock] = {}

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

        @bot.command(name="rook")
        async def cmd_help(ctx: commands.Context):
            """Show available commands."""
            await ctx.send(
                "**Rook Commands**\n"
                "Just talk to me — I'm a full Claude Code session with tools, files, and shell access.\n"
                "Each channel gets its own persistent conversation.\n\n"
                "**Commands**\n"
                "`!lookup <topic>` — Search the knowledge graph\n"
                "`!project [name]` — Show project status and recent activity\n"
                "`!search <query>` — Search synced claude.ai conversations\n"
                "`!stats` — Show hub graph statistics\n"
                "`!remember <key> <value>` — Store a fact in shared memory\n"
                "`!recall [query]` — Search shared memory\n"
                "`!index_channel [n]` — Index last N messages from this channel into the graph\n"
                "`!new_session` — Clear conversation context, start fresh\n"
                "`!rook` — This help message"
            )

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

        @bot.command(name="index_channel")
        async def cmd_index_channel(ctx: commands.Context, limit: int = 200):
            """Index this channel's message history into the knowledge graph."""
            if not self.client:
                await ctx.send("Hub not connected.")
                return
            await ctx.send(f"Indexing last {limit} messages from #{ctx.channel}...")
            indexed = 0
            async for msg in ctx.channel.history(limit=limit):
                if msg.author.bot and msg.author != bot.user:
                    continue
                text = msg.content
                if not text or len(text) < 10:
                    continue
                keywords = self._extract_keywords(text)
                if keywords:
                    await self.client.rpc(METHOD_INDEX, {
                        "concepts": keywords,
                        "source_type": "discord",
                        "source_location": f"{ctx.channel.id}/{msg.id}",
                        "source_title": f"Discord #{ctx.channel} — {msg.author.display_name}",
                    }, timeout=5)
                    indexed += 1
            await ctx.send(f"Indexed {indexed} messages from #{ctx.channel}.")

        @bot.command(name="new_session")
        async def cmd_new_session(ctx: commands.Context):
            """Kill the current CC session and start fresh (clears conversation context)."""
            channel_id = str(ctx.channel.id)
            await self._kill_channel_session(channel_id)
            await ctx.send("Session cleared. Next message starts fresh.")

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

        Uses persistent CC sessions per channel — conversation context carries over.
        Looks up the graph for context before responding. Indexes afterward.
        """
        channel_id = str(message.channel.id)

        # 1. Graph lookup for context injection
        lookup_context = ""
        if self.client and self.client._transport.connected:
            keywords = self._extract_keywords(text)
            if keywords:
                for kw in keywords.split(",")[:3]:
                    result = await self.client.rpc(METHOD_LOOKUP, {"query": kw.strip()}, timeout=5)
                    if isinstance(result, dict):
                        formatted = _format_lookup_discord(result)
                        if formatted and "No existing knowledge" not in formatted:
                            lookup_context += f"\n{formatted}"

        # 2. Build prompt — inject graph context if we have it
        prompt = text
        if lookup_context:
            prompt = f"[Rook context: {lookup_context.strip()}]\n\n{text}"

        # 3. Run through CC with persistent session
        response = await self._cc_send(channel_id, prompt)

        # 4. Index the exchange into the graph (fire and forget)
        if self.client and self.client._transport.connected:
            keywords = self._extract_keywords(text + " " + response)
            if keywords:
                asyncio.create_task(self.client.rpc(METHOD_INDEX, {
                    "concepts": keywords,
                    "source_type": "discord",
                    "source_location": channel_id,
                    "source_title": f"Discord #{message.channel}",
                }, timeout=5))

        return response

    async def _get_cc_proc(self, channel_id: str) -> asyncio.subprocess.Process:
        """Get or create a persistent interactive CC subprocess for a channel."""
        proc = self._channel_procs.get(channel_id)
        if proc and proc.returncode is None:
            return proc

        # Start a new interactive CC process with stream-json I/O
        from ..cli.cc_tmux import _find_claude_binary
        claude_bin = _find_claude_binary()

        cmd = (
            f'"{claude_bin}" --output-format stream-json --input-format stream-json'
            f' --verbose --name "discord-{channel_id}"'
            f' --dangerously-skip-permissions'
        )

        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.home()),
            )
        else:
            parts = cmd.split()
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.home()),
            )

        self._channel_procs[channel_id] = proc
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()

        log.info("Started CC process for channel %s (pid=%d)", channel_id, proc.pid)

        # Consume the init messages (system, rate_limit) without blocking
        asyncio.create_task(self._drain_stderr(proc, channel_id))

        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, channel_id: str):
        """Continuously drain stderr to prevent buffer deadlock."""
        try:
            while proc.returncode is None:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text and "no stdin data" not in text.lower() and "warning" not in text.lower():
                    log.debug("CC stderr [%s]: %s", channel_id[:6], text[:100])
        except Exception:
            pass

    async def _cc_send(self, channel_id: str, prompt: str) -> str:
        """Send a message to a persistent interactive CC session.

        The CC process stays alive between messages — full tool calling,
        multi-step reasoning, conversation memory.
        """
        lock = self._channel_locks.get(channel_id)
        if not lock:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock

        async with lock:
            proc = await self._get_cc_proc(channel_id)

            if proc.returncode is not None:
                # Process died — remove and retry once
                self._channel_procs.pop(channel_id, None)
                proc = await self._get_cc_proc(channel_id)

            # Send message via stream-json input format
            input_msg = json.dumps({"type": "user_message", "content": prompt}) + "\n"
            try:
                proc.stdin.write(input_msg.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as e:
                log.error("Failed to send to CC [%s]: %s", channel_id[:6], e)
                self._channel_procs.pop(channel_id, None)
                return "CC session died — try again."

            # Read response — collect text until we see result or message_stop
            from ..cli.cc_tmux import render_stream_json
            text_parts = []
            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=180)
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").rstrip()
                    if not raw:
                        continue

                    try:
                        event = json.loads(raw)
                        event_type = event.get("type", "")

                        # End of response markers
                        if event_type == "result":
                            result_text = event.get("result", "")
                            if result_text:
                                text_parts.append(result_text)
                            break

                        # Accumulate text deltas
                        t = render_stream_json(raw, print_it=False)
                        if t:
                            text_parts.append(t)

                    except json.JSONDecodeError:
                        text_parts.append(raw)

            except asyncio.TimeoutError:
                text_parts.append("\n(response timed out after 3 minutes)")

            return "".join(text_parts).strip() or "No response."

    async def _kill_channel_session(self, channel_id: str):
        """Kill the CC process for a channel."""
        proc = self._channel_procs.pop(channel_id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.sleep(1)
                if proc.returncode is None:
                    proc.kill()
            except Exception:
                pass
            log.info("Killed CC process for channel %s", channel_id)

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
