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
follows the `first-run` skill (rook/plugins/agents/skills/first-run.md): look
and feel (Rookie's style, always the first question), name and password (Account page), what the hub is for, reaching it from
elsewhere, other machines, voice / phone / Telegram / Discord, backups, then a
knowledge page "How this hub is set up and why", and `hub.setup {done: true}`.
Hubs set up before this (no `setup.json`) count as done.

## Rookie

Bake, Nov 5 2026: "Yeah add it and translate for 0.5. Add the rookie style
as the first question it asks the user". Rookie is the house agent's pixel
avatar, beside its newest message in the dock (and the setup chat).

- `static/rookie.js` is Bake's file, vendored unchanged (a drop-in: replace
  it and nothing else changes). Where it is loaded (shell.html,
  account.html) a comment says where it came from. The site takes the
  styles from `Rookie.SKINS`; the hub reads them from the same file
  (`SKINS.<name>={`) for `page.look`, plus `none`.
- The style is a per-person setting, `page.rookie` (default `pixel`), set
  with `page.look {rookie}`; `page.look` and `page.site` return `{rookie,
  chosen, styles}`, `hub.setup` returns it as `look`. `none` hides Rookie.
  Change it later on **Your account** (Look and feel).
- First-run step 1 is **Look and feel** (Rookie's style now; a theme will
  join it): the agent asks it in its greeting, the site shows a picker of
  live previews under that message until one is chosen, and the agent sets
  a typed answer with `page.look`. Then the rest of setup.

### 0.5 events -> Rookie states

`static/rookie-map.js` (no DOM; tested under node). Inputs the site already
had or now gets cheaply: the dock chat (`agent.chat.read`), the person
typing, and `/web/api/activity`: the dock agent's own calls for the signed-in
person as they start and end (a dispatcher watcher; cap, decision, ok, hold
name only, never args or why).

| What happens | Rookie |
|---|---|
| The person types in the chat box | `listening` (4 s after the last key) |
| A message went to the agent, no reply yet | `thinking` |
| The agent's turn (`agent.backend` running) | `thinking` |
| A call starts: the newest call in flight shows, by cap (below) | a DOING state |
| A call is held (waits on the person) | `waiting`, until it is released or refused |
| A call is denied | `confused` (2.5 s) |
| A call fails, or the turn errors | `error` (2.5 s) |
| The agent replies | `talking` (`say`), then back |
| Nothing for 10 minutes | `sleeping` |
| Otherwise | `idle` |

Cap -> state, first match wins:

| Caps | State |
|---|---|
| `agent.backend` | `thinking` |
| `agent.chat.send`, `approval.ask` | `messaging` |
| `agent.skill` | `reading` |
| `agent.note`, `stream.write` | `writing` |
| `agent.wake`, `stream.open` | `spawning` |
| `stream.read` (following a long call or a terminal) | `loading` |
| `voice.speak`, `voice.tts` | `talking` |
| `voice.ask`, `voice.stt`, `speech.hear` | `listening` |
| `core.elevate` | `locking` |
| `core.plugin.install`, `core.plugin.rollback` | `deploying` |
| `backup-store.put` | `uploading` |
| `backup-store.pull`, `backup-store.get`, `torrent.add` | `downloading` |
| `shell.*` (`shell.open` too), `proc.*`, `job.run` | `exec` |
| `browser.*`, `web.*`, `http.*` | `websearch` |
| `secret.*`, `vault.*`, `token.*`, `trust.*`, `security.*` | `locking` |
| `notify.*`, `notification.*`, `telegram.*`, `discord.*`, `sms.*`, `call.*`, `huddle.*`, other `voice.*` / `speech.*` | `messaging` |
| `media.*`, `sound.*` | `music` |
| last word `search`, `find` | `searching` |
| last word `read`, `get`, `list`, `stat`, `status`, `view`, `diff`, `manifests`, `workers` | `reading` |
| last word `write`, `create`, `update`, `set`, `post`, `comment`, `note`, `link`, `setup` | `writing` |
| last word `delete`, `remove`, `clear`, `archive`, `revoke` | `delete` |
| last word `install`, `rollback`, `distribute`, `merge` | `deploying` |
| last word `test`, `check`, `checks` | `testing` |
| last word `upload`, `put` / `download`, `pull`, `fetch` | `uploading` / `downloading` |
| anything else | `working` |

rookie.js's own sample wiring used 0.4 names (`rook_call`, `fs.find`,
`voice.listen`, `change.submit`, `secret.get`, `task.finish`); the copy
vendored here has none, and this map replaces it.

## OpenCode gotchas

- The free models refuse a custom system prompt (a custom agent or
  `instructions` hangs the turn on quiet retries), so `agents-opencode` puts
  the agent's instructions at the head of a new session's first message.
- `opencode run` reads stdin when it isn't a terminal: always give it a
  prompt on stdin or `</dev/null`.
- `--pure` hangs the free models.
