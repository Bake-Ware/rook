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
4. `hub init --no-password` (its output, with the owner's first API token
   and the recovery key, is printed); that token enrolls this machine. The install facts go to `hub/data/setup.json`
   (`install`), which `hub.setup` returns to the house agent.
5. Writes and starts `NAME-relay`, `NAME-hub`, `NAME-worker` (systemd user
   units; enabled at login unless `--no-boot`). The worker runs `shell`,
   `file` and `agents-opencode` with the tested model (its own tools on);
   security is off. The site, MCP door and relay listen on every interface
   (0.0.0.0).
6. Opens the browser on the hub's address. While setup isn't completed the
   site needs no sign-in (Bake, Nov 6: "It is simple. If configuration
   isn't completed assume we are still in setup mode"): `/` signs the
   browser in as the (first) owner and opens the setup chat; `/login` sends
   it there too. Normal sign-in starts once `hub.setup {done: true}`.
   After that, `hub login-link` (and `--open`) makes a sign-in link that
   keeps working: no time limit, any number of uses (Bake, Nov 6: "This
   security shit nobody asked for. Why is there a secure only 15 min thing
   now?"). Codes are kept as they are in `login-links.json`.

## The site

While `hub.setup` says setup isn't done, the site is the house agent's chat,
full screen (with "Your account" and "Skip to the hub"). The house agent
follows the `first-run` skill (rook/plugins/agents/skills/first-run.md): look
and feel (sound, Rookie's style, theme, name), the person's name and a
password for after setup (Account page), what the hub is for, reaching it from
elsewhere, other machines, voice / phone / Telegram / Discord, backups, then a
knowledge page "How this hub is set up and why", and `hub.setup {done: true}`.
Hubs set up before this (no `setup.json`) count as done. Until setup is
done nobody signs in: opening the site is being the owner.

## Rookie

Bake, Nov 5 2026: "Yeah add it and translate for 0.5. Add the rookie style
as the first question it asks the user", then: "Ok added sounds and themes
prompt the user if they want sound first, then rookie style then rookie
theme, then rookie name. This will be the name of the home agent". Rookie
is the house agent's avatar, beside its newest message in the dock (and the
setup chat), and the site's animated tab icon.

- `static/rookie.js` is Bake's file, vendored unchanged (a drop-in: replace
  it and nothing else changes). Where it is loaded (shell.html,
  account.html) a comment says where it came from. The site takes styles
  and themes from `Rookie.SKINS` / `Rookie.THEMES`; the hub reads the same
  lists from the file (`SKINS.<name>={`, the `THEMES={...}` keys).
- Rookie comes in with `enter()` the first time it shows; `Rookie.favicon`
  animates the tab icon with the same states; `sound` follows the person's
  answer (off until they give one).
- Look and feel = `page.look {sound, rookie, theme, name}`; it returns
  `{sound, rookie, theme, name, chosen, next, styles, themes}` (also in
  `page.site` and `hub.setup` as `look`). Sound, style and theme are the
  person's own settings (`page.sound`, `page.rookie`, `page.rookie_theme`);
  the name is the hub setting `agent.house_name` (default Rookie): the
  house agent's name. Anyone (a person, or an agent alone) may set any of
  them; an unknown style or theme shows as the default. Its prompt says
  "You are <name>", the roster shows it, the dock header and the tab title use it.
  Its identity stays `agent:house`. All four change later on **Your
  account**.
- First-run steps 1-4 are sound, style, theme, name, one question each in
  that order (`next` is the first unanswered). The agent asks; the site
  shows the matching picker (live previews; the themes on the chosen style;
  a name box with "Keep Rookie") under its question until it is answered,
  and the agent sets typed answers with `page.look`. Then the rest of setup,
  the agent speaking as its new name.

### 0.5 events -> Rookie states

`static/rookie-map.js` (no DOM; tested under node). Inputs the site already
had or now gets cheaply: the dock chat (`agent.chat.read`), the person
typing, and `/web/api/activity`: every call on the hub as it starts and
ends, as journaled (args and why included; a dispatcher watcher). The site
shows Rookie the dock agent's calls for the person who is here.

| What happens | Rookie |
|---|---|
| The person types in the chat box | `listening` (4 s after the last key) |
| A message went to the agent, no reply yet | `thinking` |
| The agent's turn (`agent.backend` and its provider's `*.turn` running) | `thinking` |
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
| `agent.backend`, last word `turn` (its provider: `agents-opencode.turn`, ...) | `thinking` |
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

## For the hardening pass

Bake, Nov 6: "Remove all restrictions and gates. Stop adding complexity to
the process. We will do a hardening pass once everything is stable". These
were built on this branch and taken out again; nothing replaces them now:

1. Who may mark first-run setup done (`hub.setup {done}`): was owners only;
   now anyone.
2. Who may rename the house agent and set sound / style / theme
   (`page.look`): was owners only for the name and a person for the rest;
   now anyone, a person or an agent alone.
3. `page.look` values: were checked against rookie.js's styles and themes,
   names cut to 40 characters; now unchecked (unknown ones show as the
   default), no length cap.
4. `/web/api/activity`: was the dock agent's calls for the signed-in person,
   without args or why; now every call, with args and why.
5. Installer binding: site, MCP door and relay were 127.0.0.1 only and the
   setup skill warned against opening the hub to the internet without
   HTTPS; now 0.0.0.0 and no warning.
6. Installer file modes and output: the install folder was 700, install.log,
   hub.yaml and setup.json 600, and init's output (API token, recovery key)
   was hidden; now default modes and init's output is printed.
7. agents-opencode: OpenCode's own tools were off with bash / edit /
   webfetch denied; now on (`own_tools: true`) and nothing denied.
8. Sign-in links: only a hash of each code was kept and codes over 200
   characters were refused (earlier also single use, 15 minutes); now
   plain codes, no limits.
9. Changing a password asked for the current one; now it doesn't.

## OpenCode gotchas

- The free models refuse a custom system prompt (a custom agent or
  `instructions` hangs the turn on quiet retries), so `agents-opencode` puts
  the agent's instructions at the head of a new session's first message.
- `opencode run` reads stdin when it isn't a terminal: always give it a
  prompt on stdin or `</dev/null`.
- `--pure` hangs the free models.
