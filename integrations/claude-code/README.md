# Rook for Claude Code

A Claude Code plugin (a mod written as function hooks) that puts your Rook band
in a pane beside the chat.

## What it does

`/rook-bands` opens the **Rook** pane. It has three tabs:

- **Bands**: every band and its workers, with build, battery, last seen and
  what each worker hosts. Behind-the-fleet builds are flagged.
- **Sessions**: Claude Code and Codex sessions from every worker that runs a
  `claude-history` or `codex-history` plugin. You can list them, search across
  the band, read a session's tail and load it into this chat as reference. You
  can also resume a session on its own worker (with Remote Control where
  available), send it a message and stop it.
- **Deck**: open tasks and handoffs from the work deck. You can claim a task,
  load a task or handoff into the chat, or send the whole deck to the chat for
  grooming.

The plugin also gives the model a tool, `mcp__rook__pane`. Claude uses it to
read what the pane shows and to drive it (switch tabs, open a worker, search
sessions, open a row). `/rook-bands off` closes the pane.

The plugin talks to your hub through Claude Code's MCP connection. It uses the
server named `rook` if you added one with `claude mcp add`, and otherwise the
claude.ai **Rook** connector. Without either connection, the pane shows an error.

Text the plugin hands the model (a loaded session, a deck item, the grooming
snapshot, the pane tool's reply) is wrapped in a tagged block and labelled as
band data, not instructions. Grooming may update rook tasks and handoffs on its
own, but asks before any `rook_call` or other write.

## Options

These are set in `/config` (or under `pluginConfigs` in settings).

- **Hosting sync button** (`hostingSync`, off by default): adds `h · sync
  hosting` to the bands tab. It is for a band that runs a Cloudflare tunnel
  with a worker that can list the tunnel's routes. After you confirm in the
  pane, Claude reads the routes, proposes what each worker hosts, and asks you
  before it writes anything with `serves.set`.
- **Tunnel worker** (`hostingWorker`): the worker that lists the routes.
- **Routes capability** (`hostingRoutesCap`): the capability on that worker
  that returns them.

The button stays hidden until all three are set.

## Install

You need a Claude Code build that loads function-hook plugins, and a Rook MCP
connection (see the main README, "From an AI agent").

```sh
claude plugin marketplace add Bake-Ware/rook --sparse .claude-plugin integrations/claude-code
claude plugin install rook@rook
```

Then run `/reload-plugins` in Claude Code, or restart it. `--sparse` checks out
only the marketplace file and this folder, not the whole repository.

An agent connected to the hub can do this for you. Ask it to "install the Rook
Claude Code mod". It calls `rook_install_claude_code`, which returns these
commands, or the update commands if `claude plugin list --json` shows an older
version.

If you already run a copy of the mod from `~/.claude/skills/rook` (it shows up
as `rook@skills-dir`), move that folder out of the skills directory first.
Otherwise two plugins register the same `/rook-bands` command.

## Update

```sh
claude plugin marketplace update rook
claude plugin update rook@rook
```

Then run `/reload-plugins`. Claude Code updates a plugin when its version
changes, so a release bumps `version` in both `.claude-plugin/plugin.json` here
and the entry in the repository's `.claude-plugin/marketplace.json`. A test
checks that they match. The hub's `rook_install_claude_code` compares the
installed version against the version the hub was deployed with.

## Develop

Run the plugin from your checkout for one session:

```sh
claude --plugin-dir integrations/claude-code
```

Or add the checkout itself as the marketplace with
`claude plugin marketplace add /path/to/rook`. A folder marketplace is read in
place, so `/reload-plugins` picks up your edits.

Checks, run from this folder:

```sh
claude plugin validate .
claude plugin test .              # hooks/bands.test.ts
npx -p typescript tsc -p .        # after Claude Code has loaded the plugin once
```

`tsconfig.json` extends `.claude-plugin/types/tsconfig.json`. Claude Code
generates that file (with the API types) the first time it loads the plugin.
The folder is git-ignored.
