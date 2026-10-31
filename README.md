<img src="docs/img/rook.gif" align="right" width="300" alt="The Rook mark: a faceted chess rook turning slowly">

# Rook

**A personal assistant that lives on your own machines.** Rook gives an
agent more access where it matters: your computers, your phone, your devices
and your accounts, reached through one hub you run, which decides what may
run and keeps a journal of everything that did.

## Getting started

**The easiest way to get started with Rook is to ask whatever agent you're
already using to set it up.** Point it at this repo, for example:

> Set up Rook for me: https://github.com/Bake-Ware/rook

It reads the [setup instructions for agents](skills/rook-setup/SKILL.md),
asks you a few questions (which subscription you use, which computer can stay
on for the hub, what else to add), handles the prerequisites and gets it
running. If you came to Rook for something specific, tell it that too; the
setup starts from what you want it for.

You don't have to: building and installing it yourself is just as supported.
Start with [Install and first run](docs/manual-0.5/install.md).

## What you get

- **A hub** on a computer that stays on: it holds your band (your machines),
  your people and their rights, the journal, the vault and the plugins.
- **Workers** on your other machines and your phone, signed in as you: what
  they can do (run a command, read the screen, send a notification, ...) are
  **caps**, and every call through the hub is checked and journaled.
- **Agents** that think with the subscription you already have (Claude Code
  or Codex): a house agent that is your personal assistant and a small team
  of specialists, each with a persistent session, a notepad and a memory.
  Message the house agent from the hub's dashboard, or from Telegram or
  Discord once you link your account.
- **Your own agents** (Claude Code, Codex, anything that speaks MCP) reach
  the band through the hub's [MCP door](docs/manual-0.5/mcp.md), with the
  same checks.

## Status

This is Rook 0.5 ("Castling"), a clean rework of Rook. It runs from a
checkout of this branch; there are no release packages yet, and the hub is
tested on Linux only. Rook 0.4 is the `v0.4.0` tag (its README:
[docs/README-0.4.md](docs/README-0.4.md)).

## More

- [The Rook 0.5 manual](docs/manual-0.5/README.md)
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)
- License: [Apache-2.0](LICENSE) ([NOTICE](NOTICE))
