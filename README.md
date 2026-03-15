# Rook

Personal AI agent with Discord integration, 3-tier memory kernel, remote worker system, sub-agents, job scheduler, and zero-friction model switching between local LLMs and Anthropic API.

## Features

- **Multi-model routing** — Switch between local LLMs (LM Studio) and Anthropic (Claude) with natural language. OAuth support for Claude Code subscriptions.
- **3-tier memory kernel** — Volatile/working/concrete fact tiers with automatic extraction, promotion, and persistence. Memory survives restarts.
- **Discord bot** — Full tool-calling agent on Discord with editable status messages and cross-channel awareness.
- **Remote workers** — Bootstrap any machine (Linux, Windows, Mac, Android/Termux) with a one-liner. Workers connect back over WebSocket for remote execution and chat.
- **Sub-agents** — Spawn background agents for parallel work. Results synthesized and relayed automatically.
- **Persistent terminals** — Named shell sessions that stay alive across conversation turns.
- **Job scheduler** — Cron-based recurring jobs and one-shot timers, persisted in SQLite.
- **Cross-channel messaging** — Send messages between Discord, worker CLIs, and any future channel from anywhere.
- **System awareness** — CPU, RAM, GPU/VRAM, disk stats injected into every prompt. Anthropic quota tracking from response headers.
- **Knowledge graph** — KuzuDB for entity/relationship storage alongside SQLite.

## Quick Start

```bash
# Clone and install
git clone https://github.com/Bake-Ware/rook.git
cd rook
pip install -e .

# Configure
cp rook/.env.example .env
# Edit .env with your Discord bot token
# Edit config.yaml with your model endpoints and settings

# Run
python -m rook          # Discord mode
python -m rook --cli    # CLI mode
```

## Remote Workers

Bootstrap a worker on any machine:

```bash
# Linux/Mac
curl -sL https://your-domain/worker | bash

# Windows (PowerShell)
iex (irm https://your-domain/worker)
```

Workers auto-install Python, create a venv if needed, connect back over WebSocket, and optionally install as a system service. Type `rook` on any machine with the worker installed to chat.

## Architecture

```
rook/
├── core/
│   ├── agent.py          # Main agent with delegated tool execution
│   ├── router.py         # Multi-model routing (OpenAI-compat + Anthropic)
│   ├── anthropic_auth.py # OAuth token management for Claude Code
│   └── config.py         # YAML config with hot reload
├── memory/
│   ├── facts.py          # 3-tier fact store (volatile/working/concrete)
│   ├── compiler.py       # Dynamic system prompt assembly
│   ├── extractor.py      # Background fact extraction via LLM
│   └── sysinfo.py        # System stats collection
├── interfaces/
│   └── discord.py        # Discord bot with editable status messages
├── remote/
│   ├── bootstrap.py      # HTTP + WebSocket server for workers
│   ├── server.py         # Worker connection management
│   └── worker.py         # Self-bootstrapping remote worker script
├── tools/
│   ├── agents.py         # Sub-agent spawning
│   ├── channels.py       # Cross-channel messaging bridge
│   ├── files.py          # File read/write/list
│   ├── memory.py         # SQLite + KuzuDB tools
│   ├── memory_kernel.py  # Promote/demote/search memory tools
│   ├── remote.py         # Remote exec/update/uninstall
│   ├── scheduler_tools.py # Job scheduling tools
│   ├── shell.py          # Shell command execution
│   ├── terminals.py      # Persistent terminal sessions
│   └── web.py            # Web search (SearXNG) and fetch
└── scheduler.py          # Cron-based job scheduler
```

## License

MIT
