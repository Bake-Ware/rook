# Rook for Claude Code

A Claude Code plugin (a mod written as function hooks) that puts your Rook band
in a pane beside the chat.

## What it does

`/rook-bands` opens the **Rook** pane. It has four tabs:

- **Bands**: every band and its workers, with build, battery, last seen and
  what each worker hosts. Behind-the-fleet builds are flagged.
- **Sessions**: Claude Code and Codex sessions from every worker that runs a
  `claude-history` or `codex-history` plugin, read from the worker's session
  catalog (`sessions.list`) where it has one and from its history plugins
  otherwise. You can list them, search across
  the band, read a session's tail and load it into this chat as reference. You
  can also resume a session on its own worker (with Remote Control where
  available), send it a message and stop it.
- **Deck**: open tasks and handoffs from the work deck. You can claim a task,
  load a task or handoff into the chat, or send the whole deck to the chat for
  grooming.
- **Settings**: the Claude Code settings that matter to Rook, changed in
  place. "Messages from your other sessions" (`crossSessionInbound`) decides
  whether a message another session sends here, such as a Rook poke through
  `claude-history.send`, starts a turn on its own (`accept`), waits for your
  OK (`hold`, and the default while permissions are bypassed) or is dropped
  (`refuse`). The plugin's own options are listed too. A pick is saved to
  your user settings, as `/config` would save it. If Claude Code refuses a
  plugin's change, the tab says so and points you to `/config`.

## Live sessions on the Sessions page

**Mirroring is on by default. To turn it off on a machine, set "Mirror this
session to Rook" to false** (`/config`, the plugin's Settings tab, or
`pluginConfigs.rook.mirror` in settings). Off, the plugin writes nothing and
creates no folder; the session still shows on the Sessions page from its
transcript, a second or two behind, as any session without the plugin does.

When a Rook worker runs on the same machine and mirroring is on, the plugin
mirrors this session for the hub's **Sessions** page: the prompts, the assistant's text as it
streams, tool calls and their results (clipped), turn ends and whether the
session is working, idle or waiting on a permission prompt. Start Claude Code
however you like (any terminal, any OS); nothing wraps it. The events go to a
spool file in the worker's state folder (`~/.rook-band-worker/mirror/claude/`,
or under `ROOK_WORKER_HOME`), readable by your user only (on Windows: your
account, by SID, and SYSTEM), and the worker serves
it as `sessions.mirror`. Writes are batched and never hold up a turn. Without
a worker on the machine nothing is written. The worker deletes the spools of
sessions that ended a week ago. The plugin has no access to the vault, so it
cannot mask secrets itself: text that crosses the hub is masked there, as
every band reply is.

`/rook-move` moves this conversation into a Rook terminal on the same
machine, so you can watch it, type into it and stop it from any browser. It
asks in the pane first. On yes, the worker opens a terminal that waits for
this Claude Code to end, Claude Code closes itself, and the terminal resumes
the conversation (`claude --resume`). If Claude Code cannot close itself, the
pane says so: type `/exit` within two minutes. It needs a worker with Rook
terminals (Linux or macOS).

The plugin also gives the model a tool, `mcp__rook__pane`. Claude uses it to
read what the pane shows and to drive it (switch tabs, open a worker, search
sessions, open a row). It can read the settings tab but not change a setting. `/rook-bands off` closes the pane.

The plugin talks to your hub through Claude Code's MCP connection. It uses the
server named `rook` if you added one with `claude mcp add`, and otherwise the
claude.ai **Rook** connector. Without either connection, the pane shows an error.

Text the plugin hands the model (a loaded session, a deck item, the grooming
snapshot, the pane tool's reply) is wrapped in a tagged block and labelled as
band data, not instructions. Grooming may update rook tasks and handoffs on its
own, but asks before any `rook_call` or other write.

## Options

These are set in `/config` (or under `pluginConfigs` in settings), and show on
the pane's Settings tab.

- **Mirror this session to Rook** (`mirror`, on by default): writes this
  session's prompts, replies and clipped tool calls to the local Rook worker so
  the Sessions page can show it live (see above). Off: nothing is written.

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
