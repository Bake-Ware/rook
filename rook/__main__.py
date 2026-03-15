"""Rook entry point — `python -m rook` or `rook` CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .core.config import Config
from .core.agent import Agent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rook")


async def run_cli(agent: Agent) -> None:
    """Simple interactive CLI for testing."""
    print("Rook CLI — type 'quit' to exit, 'models' to list models")
    print(f"Active model: {agent.router.get_active().name}")
    print()

    agent.tools.memory_store.register_channel(
        platform="cli",
        platform_id="local",
        session_id="cli",
        name="local terminal",
        modality="text",
    )

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("you> "))
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        line = line.strip()
        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            break
        if line.lower() == "models":
            for m in agent.router.list_models():
                flag = " *" if m["name"] == agent.router.get_active().name else ""
                aliases = f" (aka {m['aliases']})" if m["aliases"] else ""
                print(f"  {m['name']}{aliases}: {m['model']} @ {m['endpoint']}{flag}")
            continue

        response = await agent.handle_message(line, session_id="cli")
        print(f"\nrook> {response}\n")


async def run_discord(agent: Agent, config: Config) -> None:
    """Start the Discord bot."""
    from .interfaces.discord import create_bot

    bot = create_bot(agent, config)
    token = config.resolve_env("ROOK_DISCORD_TOKEN") or config.resolve_env("DISCORD_BOT_TOKEN")
    if not token:
        log.error("ROOK_DISCORD_TOKEN not set. Run with --cli for local testing.")
        sys.exit(1)
    await bot.start(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rook — Personal AI Agent")
    parser.add_argument("--config", type=Path, default=None, help="Config file path")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no Discord)")
    args = parser.parse_args()

    config = Config(args.config)
    agent = Agent(config)

    log.info("Rook v0.1.0 starting")
    log.info("Default model: %s", config.default_model)
    log.info("Available models: %s", list(config.models.keys()))

    if args.cli:
        asyncio.run(run_cli(agent))
    else:
        asyncio.run(run_discord(agent, config))


if __name__ == "__main__":
    main()
