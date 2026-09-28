"""Rook entry point — `python -m rook` or `rook` CLI."""

from __future__ import annotations

import sys


SUBCOMMANDS = {
    "band":      "Terminal control panel for the worker band (live view, run/manage caps)",
    "worker":    "Run the background worker",
    "dashboard": "Run the hub dashboard / installer server (python -m rook.remote.bootstrap)",
    "mcp":       "Run the hub MCP server and WebSocket bridge (python -m rook.band_mcp)",
    "sessions":  "Browse Claude Code session history",
    "history":   "Browse Claude Code session history",
    "tmux":      "Manage Claude Code sessions (spawn, attach, kill)",
}


def main() -> None:
    if len(sys.argv) <= 1:
        from .cli.band_tui import main as band_main
        band_main()
        return
    if sys.argv[1] in ("-h", "--help", "help"):
        print("Rook — worker band and terminal dashboard\n")
        print("Usage: rook <command> [args]\n")
        print("Run rook with no arguments to open the terminal dashboard.\n")
        print("Commands:")
        for cmd, desc in SUBCOMMANDS.items():
            if cmd == "history":
                continue
            print(f"  {cmd:12s} {desc}")
        return

    # Lightweight subcommands — no heavy imports needed
    if sys.argv[1] == "worker":
        from .worker.cli import main as worker_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        worker_main()
        return

    if sys.argv[1] == "dashboard":
        from .remote.bootstrap import _cli_main as dashboard_main
        sys.argv = ["rook dashboard"] + sys.argv[2:]
        dashboard_main()
        return

    if sys.argv[1] == "mcp":
        from .band_mcp.server import main as mcp_main
        sys.argv = ["rook mcp"] + sys.argv[2:]
        mcp_main()
        return

    if sys.argv[1] in ("band", "tui"):
        from .cli.band_tui import main as band_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        band_main()
        return

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

    print(f"Unknown command: {sys.argv[1]}")
    print("Run 'rook --help' for available commands.")
    sys.exit(2)


if __name__ == "__main__":
    main()
