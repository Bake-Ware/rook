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

    def __init__(self, token: str, hub_url: str, open_channels: list[str] | None = None,
                 guild_id: int | None = None):
        self.token = token
        self.hub_url = hub_url
        self.open_channels = set(open_channels or [])
        self.guild_id = guild_id
        self.client: RookClient | None = None

        # CC session IDs per channel — persists conversation via --resume
        self._channel_sessions: dict[str, str] = {}
        self._channel_locks: dict[str, asyncio.Lock] = {}

        # Broadcast polling state
        self._last_broadcast_id: int = 0
        # Map session_id → discord channel ID for live streams
        self._stream_channels: dict[str, int] = {}

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

            # Find the guild
            if self.guild_id:
                guild = bot.get_guild(self.guild_id)
            elif bot.guilds:
                guild = bot.guilds[0]
            else:
                guild = None
            self._guild = guild
            log.info("Guild: %s", guild.name if guild else "none")

            # Find or create the rook-activity channel
            if guild:
                self._activity_channel = discord.utils.get(guild.text_channels, name="rook-activity")
                if not self._activity_channel:
                    # Find or create a Rook category
                    category = discord.utils.get(guild.categories, name="Rook")
                    if not category:
                        try:
                            category = await guild.create_category("Rook")
                        except Exception as e:
                            log.warning("Couldn't create Rook category: %s", e)
                            category = None
                    try:
                        self._activity_channel = await guild.create_text_channel(
                            "rook-activity", category=category,
                            topic="Rook broadcast feed — CC session updates and announcements",
                        )
                        log.info("Created #rook-activity")
                    except Exception as e:
                        log.warning("Couldn't create #rook-activity: %s", e)
                        self._activity_channel = None

            # Connect to hub
            self.client = RookClient(hub_url=self.hub_url)
            await self.client.start()
            if self.client._transport.connected:
                log.info("Connected to Rook hub at %s", self.hub_url)
                stats = await self.client.rpc(METHOD_STATS, {}, timeout=5)
                log.info("Hub graph: %s", stats)
            else:
                log.warning("Hub not reachable — running in offline mode")

            # Start broadcast poller
            asyncio.create_task(self._poll_broadcasts())

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
                "Talk to me — I'm a full Claude Code session with tools, files, and shell access.\n"
                "Each channel gets its own persistent conversation.\n\n"
                "**Chat**\n"
                "`!new_session` — Clear conversation context, start fresh\n\n"
                "**Knowledge Graph**\n"
                "`!lookup <topic>` — Search the knowledge graph\n"
                "`!project [name]` — Show project status and recent activity\n"
                "`!search <query>` — Search synced claude.ai conversations\n"
                "`!stats` — Hub graph statistics\n\n"
                "**Memory**\n"
                "`!remember <key> <value>` — Store a fact\n"
                "`!recall [query]` — Search shared memory\n\n"
                "**Live Sessions**\n"
                "`!sessions` — List running CC sessions\n"
                "`!follow [id]` — Follow a session (creates a channel, streams output)\n"
                "`!detach` — Stop following a session in this channel\n\n"
                "**Index**\n"
                "`!index_channel [n]` — Index last N messages into the graph\n\n"
                "CC sessions can also push to Discord:\n"
                "• `rook_broadcast` — post a message\n"
                "• `rook_stream_start` — register for live streaming\n"
                "• `rook_stream_update` — post updates to the stream"
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
            """Start a fresh CC session for this channel (clears conversation context)."""
            channel_id = str(ctx.channel.id)
            await self._kill_channel_session(channel_id)
            await ctx.send("Session cleared. Next message starts a new conversation.")

        @bot.command(name="sessions")
        async def cmd_sessions(ctx: commands.Context):
            """List running CC sessions that can be followed."""
            import sqlite3
            db_path = Path.home() / ".rook" / "broadcast.db"
            if not db_path.exists():
                await ctx.send("No active sessions.")
                return
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT session_id, description, project, status, discord_channel FROM live_sessions ORDER BY last_activity DESC LIMIT 10"
            ).fetchall()
            db.close()
            if not rows:
                await ctx.send("No active sessions.")
                return
            lines = ["**Live Sessions**\n"]
            for r in rows:
                status_icon = "🟢" if r["status"] == "active" else "⚫"
                ch = f" → <#{r['discord_channel']}>" if r["discord_channel"] else ""
                lines.append(f"{status_icon} `{r['session_id']}` {r['description']}{ch}")
            await ctx.send("\n".join(lines))

        @bot.command(name="follow")
        async def cmd_follow(ctx: commands.Context, session_id: str = ""):
            """Follow a running CC session — creates a channel and streams output."""
            import sqlite3
            db_path = Path.home() / ".rook" / "broadcast.db"
            if not db_path.exists():
                await ctx.send("No sessions to follow.")
                return
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row

            if not session_id:
                # Show active sessions
                rows = db.execute(
                    "SELECT session_id, description, project FROM live_sessions WHERE status='active' ORDER BY last_activity DESC LIMIT 5"
                ).fetchall()
                db.close()
                if not rows:
                    await ctx.send("No active sessions. CC sessions register with `rook_stream_start`.")
                    return
                lines = ["**Active sessions — pick one:**\n"]
                for r in rows:
                    lines.append(f"`!follow {r['session_id']}` — {r['description']}")
                await ctx.send("\n".join(lines))
                return

            # Find the session
            row = db.execute("SELECT * FROM live_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                db.close()
                await ctx.send(f"Session `{session_id}` not found.")
                return

            # Create channel if it doesn't have one
            if not row["discord_channel"]:
                channel = await self._create_stream_channel(session_id, row["description"], row["project"])
                if channel:
                    db.execute("UPDATE live_sessions SET discord_channel=? WHERE session_id=?",
                               (str(channel.id), session_id))
                    db.commit()
                    await ctx.send(f"Following session `{session_id}` in <#{channel.id}>")
                else:
                    await ctx.send("Failed to create channel.")
            else:
                await ctx.send(f"Session `{session_id}` is already streaming in <#{row['discord_channel']}>")

            db.close()

        @bot.command(name="detach")
        async def cmd_detach(ctx: commands.Context):
            """Stop following a session in this channel (channel stays as a log)."""
            channel_id = ctx.channel.id
            # Find which session this channel is following
            detached = None
            for sid, cid in list(self._stream_channels.items()):
                if cid == channel_id:
                    del self._stream_channels[sid]
                    detached = sid
                    break
            if detached:
                await ctx.send(f"Detached from session `{detached}`. Channel preserved as log.")
            else:
                await ctx.send("This channel isn't following a session.")

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

        # Just pass the message straight to CC — no context injection
        # CC has its own tools (including Rook MCP) to look things up when needed
        response = await self._cc_send(channel_id, text)

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

    async def _cc_send(self, channel_id: str, prompt: str) -> str:
        """Send a message via CC --print --resume.

        Each invocation is a full CC tool loop (chained tool calls work).
        Session persists on disk between invocations via --session-id / --resume.
        """
        import uuid
        from ..cli.cc_tmux import _find_claude_binary, render_stream_json

        lock = self._channel_locks.get(channel_id)
        if not lock:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock

        async with lock:
            claude_bin = _find_claude_binary()
            session_id = self._channel_sessions.get(channel_id)

            if not session_id:
                session_id = str(uuid.uuid4())
                self._channel_sessions[channel_id] = session_id

            log.info("CC [%s] session=%s prompt=%s", channel_id[:6], session_id[:8], prompt[:60])

            # Track whether this session has been used before (for --resume vs --session-id)
            if not hasattr(self, '_used_sessions'):
                self._used_sessions = set()

            is_new = session_id not in self._used_sessions

            args = [claude_bin, "-p", prompt,
                    "--output-format", "stream-json", "--verbose"]
            if is_new:
                args.extend(["--session-id", session_id, "--name", f"discord-{channel_id}"])
                self._used_sessions.add(session_id)
            else:
                args.extend(["--resume", session_id])

            # create_subprocess_exec passes args directly — no shell escaping needed
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.home()),
            )

            # Read response
            text_parts = []
            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=180)
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").rstrip()
                    if not raw:
                        continue
                    t = render_stream_json(raw, print_it=False)
                    if t:
                        text_parts.append(t)
            except asyncio.TimeoutError:
                proc.kill()
                text_parts.append("\n(timed out after 3 minutes)")

            # Wait for exit
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

            # Check for session errors
            stderr = ""
            try:
                stderr_data = await proc.stderr.read()
                stderr = stderr_data.decode("utf-8", errors="replace")
            except Exception:
                pass

            if "Could not find session" in stderr or "not found" in stderr.lower():
                self._channel_sessions.pop(channel_id, None)
                log.warning("Session expired for channel %s, will create new next time", channel_id[:6])

            response = "".join(text_parts).strip()
            return response or "No response."

    async def _kill_channel_session(self, channel_id: str):
        """Clear the session for a channel."""
        self._channel_sessions.pop(channel_id, None)
        log.info("Session cleared for channel %s", channel_id)

    # ── Broadcast poller ─────────────────────────────────────────────────

    async def _poll_broadcasts(self):
        """Poll the broadcast DB for new messages and route to Discord."""
        import sqlite3
        db_path = Path.home() / ".rook" / "broadcast.db"

        while True:
            await asyncio.sleep(2)
            try:
                if not db_path.exists():
                    continue

                db = sqlite3.connect(str(db_path))
                db.row_factory = sqlite3.Row

                # Get new broadcasts since last poll
                rows = db.execute(
                    "SELECT id, session_id, message, project, timestamp FROM broadcasts WHERE id > ? ORDER BY id",
                    (self._last_broadcast_id,),
                ).fetchall()

                for row in rows:
                    self._last_broadcast_id = row["id"]
                    await self._route_broadcast(
                        session_id=row["session_id"],
                        message=row["message"],
                        project=row["project"] or "",
                    )

                # Check for new live sessions that need channels
                live = db.execute(
                    "SELECT session_id, description, project FROM live_sessions WHERE status='active' AND discord_channel=''"
                ).fetchall()

                for session in live:
                    channel = await self._create_stream_channel(
                        session["session_id"], session["description"], session["project"],
                    )
                    if channel:
                        db.execute("UPDATE live_sessions SET discord_channel=? WHERE session_id=?",
                                   (str(channel.id), session["session_id"]))
                        db.commit()

                db.close()

            except Exception as e:
                log.error("Broadcast poll error: %s", e)

    async def _route_broadcast(self, session_id: str, message: str, project: str):
        """Route a broadcast message to the right Discord channel."""
        # Check if this session has a dedicated stream channel
        if session_id in self._stream_channels:
            channel = self.bot.get_channel(self._stream_channels[session_id])
            if channel:
                for chunk in split_message(message):
                    await channel.send(chunk)
                return

        # Otherwise post to #rook-activity
        if hasattr(self, '_activity_channel') and self._activity_channel:
            prefix = f"**[{project}]** " if project else ""
            for chunk in split_message(f"{prefix}{message}"):
                await self._activity_channel.send(chunk)

    async def _create_stream_channel(self, session_id: str, description: str,
                                     project: str) -> discord.TextChannel | None:
        """Create a Discord channel for a live CC session stream."""
        guild = getattr(self, '_guild', None)
        if not guild:
            return None

        # Find or create Rook category
        category = discord.utils.get(guild.categories, name="Rook")
        if not category:
            try:
                category = await guild.create_category("Rook")
            except Exception:
                category = None

        # Channel name from project or session ID
        name = f"cc-{project}" if project else f"cc-{session_id}"
        name = re.sub(r'[^a-z0-9-]', '-', name.lower())[:90]

        try:
            channel = await guild.create_text_channel(
                name, category=category,
                topic=f"Live CC session: {description} [{session_id}]",
            )
            self._stream_channels[session_id] = channel.id
            log.info("Created stream channel #%s for session %s", name, session_id)

            await channel.send(
                f"**CC Session Started**\n"
                f"**Session:** `{session_id}`\n"
                f"**Task:** {description}\n"
                f"Updates will stream here. Type in this channel to send input."
            )
            return channel
        except Exception as e:
            log.error("Failed to create stream channel: %s", e)
            return None

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
