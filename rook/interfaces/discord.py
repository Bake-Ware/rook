"""Discord interface — single bot, clean and simple."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from ..core.agent import Agent
from ..core.config import Config

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
DISCORD_MAX = 2000


def clean_response(text: str) -> str:
    text = _THINK_RE.sub("", text)
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


def create_bot(agent: Agent, config: Config) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        log.info("Discord connected as %s", bot.user)
        model = agent.router.get_active()
        log.info("Active model: %s (%s)", model.name, model.model)

        bot_name = bot.user.display_name if bot.user else "Rook"
        agent.set_identity(bot_name)

        # Wire scheduler notifications to Discord
        async def notify_channel(channel_id: str, message: str) -> None:
            channel = bot.get_channel(int(channel_id))
            if channel:
                cleaned = clean_response(message)
                if cleaned:
                    for chunk in split_message(cleaned):
                        await channel.send(chunk)

        agent._notify_callback = notify_channel
        await agent.start_services()

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.content.startswith("!"):
            await bot.process_commands(message)
            return

        text = message.content
        if bot.user:
            text = text.replace(f"<@{bot.user.id}>", "").strip()

        if not text:
            return

        session_id = f"discord:{message.channel.id}"

        # Register/touch this channel
        agent.tools.memory_store.register_channel(
            platform="discord",
            platform_id=str(message.channel.id),
            session_id=session_id,
            name=str(message.channel),
            modality="text",
        )

        # Tool notifications — each tool gets its own line, marked done when complete
        current_status_msg = None

        async def tool_notify(msg: str) -> None:
            nonlocal current_status_msg
            try:
                # Mark previous as done
                if current_status_msg:
                    old_content = current_status_msg.content
                    if not old_content.endswith(" done"):
                        await current_status_msg.edit(content=old_content + " done")
                # New status message
                current_status_msg = await message.channel.send(msg)
            except Exception:
                pass

        agent._tool_notify[session_id] = tool_notify

        async with message.channel.typing():
            response = await agent.handle_message(
                text,
                session_id=session_id,
                author=str(message.author),
                channel=str(message.channel),
            )

        # Mark last status as done
        if current_status_msg:
            try:
                old_content = current_status_msg.content
                if not old_content.endswith(" done"):
                    await current_status_msg.edit(content=old_content + " done")
            except Exception:
                pass

        response = clean_response(response)
        if not response:
            return

        chunks = split_message(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

    @bot.command(name="models")
    async def cmd_models(ctx: commands.Context) -> None:
        lines = []
        active = agent.router.get_active(f"discord:{ctx.channel.id}")
        for m in agent.router.list_models():
            flag = " **[active]**" if m["name"] == active.name else ""
            aliases = f" (aka {m['aliases']})" if m["aliases"] else ""
            lines.append(f"- `{m['name']}`{aliases}: {m['model']}{flag}")
        await ctx.send("\n".join(lines))

    @bot.command(name="use")
    async def cmd_use(ctx: commands.Context, model_name: str) -> None:
        session_id = f"discord:{ctx.channel.id}"
        entry = agent.router.set_active(session_id, model_name)
        if entry:
            await ctx.send(f"Switched to **{entry.name}** (`{entry.model}`)")
        else:
            names = ", ".join(f"`{m['name']}`" for m in agent.router.list_models())
            await ctx.send(f"Unknown model `{model_name}`. Available: {names}")

    return bot
