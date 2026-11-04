# Rook 0.5 first run

Bake, Nov 4 2026: "download the installer, the installer asks the basic
questions required: what do you use for AI now? ... Then it installs the hub
site, and opens it in the browser. This then detects the state of the
configuration and the user gets the home agent chat using sensible defaults.
At which point the home agent walks them through setting up the rest of the
system." First use case: none / OpenCode. No terminal commands, tokens or
password files for the person.

## Install (Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/c12/first-run/deploy/install/install.sh | bash
```

or `deploy/install/install.sh` from a checkout (installs that checkout's
commit). Options: `--dir` (default `~/.rook`), `--name` (unit prefix, default
`rook`), `--http-port 8740`, `--mcp-port 8741`, `--relay-port 7474`, `--ai`,
`--no-boot`, `--no-browser`; `--open` makes a new sign-in link,
`--uninstall` stops it and moves the folder aside.

What it does:

1. Asks what you use for AI now. Only "none" is built: the others say
   "coming soon" and fall back to it.
2. None: installs OpenCode (opencode.ai's installer) if missing, lists its
   free `opencode/*` models and keeps the first that answers a real
   `opencode run` (no account).
3. Installs uv if missing, a uv-managed Python 3.12 and Rook into `DIR`.
4. `hub init --no-password`; the owner's first API token enrolls this
   machine and is then forgotten; nothing secret is printed or left in a
   file for the person. The install facts go to `hub/data/setup.json`
   (`install`), which `hub.setup` returns to the house agent.
5. Writes and starts `NAME-relay`, `NAME-hub`, `NAME-worker` (systemd user
   units; enabled at login unless `--no-boot`). The worker runs `shell`,
   `file` and `agents-opencode` with the tested model; security is off.
6. Opens the browser on a one-time sign-in link (`hub login-link`: single
   use, 15 minutes, only its hash is kept).

## The site

While `hub.setup` says setup isn't done, the site is the house agent's chat,
full screen (with "Your account" and "Skip to the hub"). The house agent
follows the `first-run` skill (rook/plugins/agents/skills/first-run.md): name
and password (Account page), what the hub is for, reaching it from
elsewhere, other machines, voice / phone / Telegram / Discord, backups, then a
knowledge page "How this hub is set up and why", and `hub.setup {done: true}`.
Hubs set up before this (no `setup.json`) count as done.

## OpenCode gotchas

- The free models refuse a custom system prompt (a custom agent or
  `instructions` hangs the turn on quiet retries), so `agents-opencode` puts
  the agent's instructions at the head of a new session's first message.
- `opencode run` reads stdin when it isn't a terminal: always give it a
  prompt on stdin or `</dev/null`.
- `--pure` hangs the free models.
