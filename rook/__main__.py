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
    "skill":     "Install or package the Rook agent skill (Claude Code, Codex)",
    "hub":       "Build, deploy, inspect and roll back signed hub releases",
}


TMUX_RETIRED = """\
`rook tmux` was removed. Agent sessions now live in Rook terminals on each worker:
  - Start, watch, type into and stop them on the dashboard's Sessions page.
  - From a terminal: `rook band` (run sessions.list / work.stream.open / sessions.send /
    sessions.stop on a worker), or the MCP tools rook_console_open / rook_console_write.
  - Browse local Claude Code history offline with `rook sessions`.
  - Run sessions.shim.install on a worker so `claude`/`codex` typed in your own
    terminal run in a Rook terminal too."""


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
        # Retired: it ran a second session manager (its own ~/.rook/sessions.db)
        # beside the worker's Rook terminals. Say where its jobs went.
        print(TMUX_RETIRED, file=sys.stderr)
        sys.exit(2)

    if sys.argv[1] == "hub":
        from .hubdeploy.cli import main as hub_main
        sys.exit(hub_main(sys.argv[2:]))

    if sys.argv[1] == "skill":
        from .cli.skill import main as skill_main
        sys.exit(skill_main(sys.argv[2:]))

    print(f"Unknown command: {sys.argv[1]}")
    print("Run 'rook --help' for available commands.")
    sys.exit(2)


if __name__ == "__main__":
    main()
