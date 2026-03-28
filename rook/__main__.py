"""Rook entry point — `python -m rook` or `rook` CLI."""

from __future__ import annotations

import sys


SUBCOMMANDS = {
    "sessions": "Browse Claude Code session history",
    "history":  "Browse Claude Code session history",
    "tmux":     "Manage Claude Code sessions (spawn, attach, kill)",
    "hub":      "Start the Rook hub server",
    "sync":     "Sync Claude.ai cloud conversations",
    "extract":  "Extract concepts from conversations (local model)",
}


def main() -> None:
    # No args or help — show available commands
    if len(sys.argv) <= 1 or sys.argv[1] in ("-h", "--help", "help"):
        print("Rook — Knowledge graph and session network for Claude Code\n")
        print("Usage: rook <command> [args]\n")
        print("Commands:")
        for cmd, desc in SUBCOMMANDS.items():
            if cmd == "history":
                continue
            print(f"  {cmd:12s} {desc}")
        print(f"\nMCP server:  python -m rook.mcp_server")
        print(f"Hub server:  python -m rook.net.hub")
        return

    # Lightweight subcommands — no heavy imports needed
    if sys.argv[1] in ("sessions", "history"):
        from .cli.cc_history import main as history_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        history_main()
        return

    if sys.argv[1] == "tmux":
        from .cli.cc_tmux import main as tmux_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        tmux_main()
        return

    if sys.argv[1] == "hub":
        from .net.hub import main as hub_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        hub_main()
        return

    if sys.argv[1] == "sync":
        from .cli.cloud_sync import main as sync_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        sync_main()
        return

    if sys.argv[1] == "extract":
        from .cli.extractor import main as extract_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        extract_main()
        return

    if sys.argv[1] != "agent":
        print(f"Unknown command: {sys.argv[1]}")
        print("Run 'rook' for available commands.")
        return

    # Full agent mode — heavy imports here (rook agent --cli / rook agent)
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip "agent" subcommand

    import argparse
    import asyncio
    import logging
    from pathlib import Path

    from .core.config import Config
    from .core.agent import Agent

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("rook")

    parser = argparse.ArgumentParser(description="Rook — Personal AI Agent")
    parser.add_argument("--config", type=Path, default=None, help="Config file path")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no Discord)")
    args = parser.parse_args()

    config = Config(args.config)
    agent = Agent(config)

    log.info("Rook v0.1.0 starting")
    log.info("Default model: %s", config.default_model)
    log.info("Available models: %s", list(config.models.keys()))

    async def run_cli() -> None:
        print("Rook CLI — type 'quit' to exit, 'models' to list models")
        print(f"Active model: {agent.router.get_active().name}\n")
        agent.tools.memory_store.register_channel(
            platform="cli", platform_id="local", session_id="cli",
            name="local terminal", modality="text",
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

    async def run_discord() -> None:
        from .interfaces.discord import create_bot
        bot = create_bot(agent, config)
        token = config.resolve_env("ROOK_DISCORD_TOKEN") or config.resolve_env("DISCORD_BOT_TOKEN")
        if not token:
            log.error("ROOK_DISCORD_TOKEN not set. Run with --cli for local testing.")
            sys.exit(1)
        await bot.start(token)

    if args.cli:
        asyncio.run(run_cli())
    else:
        asyncio.run(run_discord())


if __name__ == "__main__":
    main()
