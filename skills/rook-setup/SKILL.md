---
name: rook-setup
description: Install and set up Rook 0.5 for the person you are helping, as a conversation - which subscription their agents think with, which computer runs the hub, what else to add, where it lives - then the install itself, prerequisites included. Use when someone asks you to set up, install or try Rook, or points you at github.com/Bake-Ware/rook.
---

# Setting up Rook for someone

You are installing Rook for the person you're talking with. Remove the
ambiguity for them: ask the few questions below in plain words, handle the
prerequisites and platform quirks yourself, and get it running. Say what you
are about to change on their machine before you change it.

Rook 0.5 runs from a checkout of the `castling` branch (no packages yet).
The quick installer below is tested on Linux.

## 1. Start from what they want

They may have found Rook in the middle of something else ("help me manage
my weekly agenda"). Ask, or recall from the conversation, what they want it
for, and carry that through: it decides what to add in step 5 and what to
show them first. A passing question needs no setup at all.

## 2. The conversation (one or two questions at a time)

- **Which subscription should Rook's agents think with?** Claude Code or
  Codex. Check what is installed and signed in: `claude --version`;
  `codex login status`. Rook uses the CLI under their subscription; no API
  key is needed.
- **Which computer can stay on for the hub?** A home server, a desktop that
  doesn't sleep, a small Linux box. The hub holds the band, the people, the
  journal and the vault. Today the hub runs on Linux; on a Mac or Windows
  machine, say so plainly and suggest a Linux box or VM for the hub (their
  other machines can still join later).
- **What else should join?** Other computers, their Android phone, Telegram
  or Discord to message the house agent from.
- **Where should things live?** Default: everything under
  `~/.rook-castling` on the hub machine, as systemd user units.

Their answers are settled, not open-ended: don't re-ask, and pick sensible
defaults for anything they don't care about (say which).

## 3. Prerequisites (on the hub machine)

- `git`, `curl`, and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`);
  uv brings its own Python 3.12, so the system Python doesn't matter.
- A systemd user session (`systemctl --user status` answers).
- The chosen CLI signed in as the person, on this machine (`claude`, or
  `codex login`). If it lives on another machine, install the hub here and
  put the agent backend on that machine once it has joined (step 5).

## 4. Install

```sh
git clone -b castling https://github.com/Bake-Ware/rook ~/rook
ROOK_CASTLING_OWNER=<their user name, lowercase> ~/rook/deploy/castling-sidecar/install.sh
~/.rook-castling/src/deploy/castling-sidecar/check.sh        # smoke test
```

This makes a relay, a hub and a worker on this machine, signs the worker in
as the owner and starts all three (`rook-castling-{relay,hub,worker}`). The
units don't start at boot: offer
`systemctl --user enable rook-castling-relay rook-castling-hub rook-castling-worker`
plus `loginctl enable-linger "$USER"` so they run without a login.

Then, without ever printing a secret into the chat:

- The dashboard is http://127.0.0.1:8750/. Tell them their sign-in token is in
  `~/.rook-castling/secrets/owner.token`.
- **The recovery key** in `~/.rook-castling/secrets/recovery-key.txt` is the
  only way to restore a backup or rebuild the vault: ask them to copy it
  somewhere safe now (a password manager), and to set up backups
  ([Install and first run](../../docs/manual-0.5/install.md#backups)).

## 5. Configure, from their answers

**Models** (all changeable later). The defaults per backend:

- Claude Code: Opus for the agents' normal thinking (System Two; they can
  move it down to Sonnet), Haiku for the light curation pass (System Three).
  Already the default.
- Codex: Codex's own default model for System Two; a fast one (e.g.
  `gpt-6-luna`) for System Three. Choose it as the agents' backend in
  `~/.rook-castling/hub/hub.yaml`:

  ```yaml
  contracts:
    agent.backend: agents-codex.turn
  ```

Model settings live in `~/.rook-castling/worker/worker.yaml` under
`settings:` (`agents-claude-code: {model, model_three}`, or
`agents-codex: {model, model_three}`). Restart after a change:
`~/.rook-castling/src/deploy/castling-sidecar/restart.sh`. The backends'
manuals: `rook/plugins/agents-claude-code/docs/manual.md`,
`rook/plugins/agents-codex/docs/manual.md`.

**Their own agent on the band.** The installer wrote
`~/.rook-castling/secrets/claude-mcp.json`, a Claude Code MCP config for the
hub's MCP door: `claude --mcp-config ~/.rook-castling/secrets/claude-mcp.json`
for one session ([MCP for agents](../../docs/manual-0.5/mcp.md) for other
agents and adding it for good).

**Other machines and the phone.** The quick install's relay listens on this
machine only, so a second machine needs a relay others can reach (Telesthete's
`telesthete-hub`) and TLS in front of the hub first: walk them through
[Install and first run](../../docs/manual-0.5/install.md#before-it-leaves-this-machine),
then [Adding machines](../../docs/manual-0.5/enrollment.md) (the Android app:
Settings > Band > Rook version > 0.5 preview).

**Telegram or Discord.** Make a bot, store its token in the vault
(`~/.rook-castling/venv/bin/python -m rook.core2.hub --config ~/.rook-castling/hub/hub.yaml secret set telegram-bot`,
the token on stdin), link the plugin into `~/.rook-castling/hub/library/`,
set `telegram: {token: telegram-bot}` under `settings:` in `hub.yaml`,
restart the hub, then the person links their account (`telegram.link`) and
can message the house agent there. Details: `rook/plugins/telegram/docs/manual.md`
(Discord the same way).

## 6. Hand over

Show them the one thing they came for working (a first message to the house
agent from the dashboard's chat dock is a good start), say where the manual
is (`docs/manual-0.5/README.md`), and how to undo all of it
(`~/.rook-castling/src/deploy/castling-sidecar/uninstall.sh` moves
everything aside).

## People who'd rather do it themselves

Building, installing and configuring it by hand is first-class: point them
at [Install and first run](../../docs/manual-0.5/install.md) ("By hand") and
stay out of the way.
