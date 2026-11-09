# Rook 0.5 first run

Bake, Nov 4 2026: "download the installer, the installer asks the basic
questions required: what do you use for AI now? ... Then it installs the hub
site, and opens it in the browser. This then detects the state of the
configuration and the user gets the home agent chat using sensible defaults.
At which point the home agent walks them through setting up the rest of the
system." First use cases: none / OpenCode, and Claude Code. No terminal commands, tokens or
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

1. Asks what you use for AI now. "Claude Code" (1, `--ai claude`) and
   "none" (6) are built: the others say "coming soon" and fall back to none.
2. Claude Code (Bake, Nov 9: "start it in claude code mode"): uses the
   `claude` CLI already on the machine, or installs it with the official
   installer (`curl -fsSL https://claude.ai/install.sh | bash`). If
   `claude auth status` says it isn't signed in, it asks the person to run
   `claude` once in another terminal to sign in and press Enter (three
   tries; with no terminal it stops and says so). Then one short turn the
   way Rook runs it (`claude -p --output-format json --permission-mode auto
   --setting-sources project,local --strict-mcp-config`) has to answer.
   None: installs OpenCode (opencode.ai's installer) if missing, lists its
   free `opencode/*` models and keeps the first that answers a real
   `opencode run` (no account).
3. Installs uv if missing, a uv-managed Python 3.12 and Rook into `DIR`.
4. `hub init --no-password` (its output, with the owner's first API token
   and the recovery key, is printed); that token enrolls this machine. The install facts go to `hub/data/setup.json`
   (`install`), which `hub.setup` returns to the house agent.
5. Writes and starts `NAME-relay`, `NAME-hub`, `NAME-worker` (systemd user
   units; enabled at login unless `--no-boot`). The worker runs `shell`,
   `file` and the backend: `agents-claude-code` (`claude` = the tested
   path, `permission_mode: auto`, `door_url` = this hub's MCP door; the
   hub's `agent.backend` contract is `agents-claude-code.turn`) or
   `agents-opencode` with the tested model (its own tools on); security is
   off. The site, MCP door and relay listen on every interface
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

Bake, Nov 9 2026: "I want this to be more like a two pane thing where like
the chat and rookie are on one side and the left is like stuff we're setting
up. Have the rookie avatar move to things it's talking about on the page."
and "I want it to guide the setup entirely".

While `hub.setup` says setup isn't done, the site is two panes, no page
list: the **work pane** on the left (what is being set up right now) and the
house agent's chat on the right, with Rookie at its head. After setup the
same chat is the right column beside every page (the bar's Chat button
shows or hides it), and pages fill the left. Until setup is done nobody
signs in: opening the site is being the owner. Hubs set up before this (no
`setup.json`) count as done.

The house agent follows the `first-run` skill
(rook/plugins/agents/skills/first-run.md): look and feel (sound, Rookie's
style, theme, name), the person's name and a password (account view), what
the hub is for, reaching this site (with the firewall directive below),
other machines (what a worker is first), voice / phone / Telegram / Discord,
backups, then a knowledge page "How this hub is set up and why", and
`hub.setup {done: true}`.

### Right after an install

The worker joins a few seconds after the installer starts it, and until it
serves `agent.backend` no turn can run. `page.site` says `ready` (some
provider of `agent.backend` installed and its worker online); the site
shows "Getting ready..." and starts the walk-through only when it is true.
A turn that fails because nothing serves the backend yet is never shown as
a message: the site waits and sends the kickoff again, and on a reload it
starts again when the agent never got to answer. The installer waits for
the same `ready` (through `page.site`) before it says Done.

While setup isn't done, every turn of the house agent carries the first-run
skill in its message (`setup_brief` in the agents runtime, from
`hub.setup`): OpenCode keeps only a session's first instructions, and
without this the agent drifted into a generic assistant after a failed
start.

### What the agent can put in a message

A line of its own in `[[...]]` (`static/chat-widgets.js`, node-tested):

| Line | What the site does |
|---|---|
| `[[choices: A \| B \| C]]` | buttons under the message; a click answers with that text |
| `[[open: vault \| new=host_password \| for=sudo on box]]` | the Vault view on the left: the key filled in, the cursor in the value field, Rookie pointing at it |
| `[[open: account]]` | the account view: the person's name and a password (`name=` pre-fills) |
| `[[open: network \| home=http://192.168.1.20:8740 \| status=...]]` | the three ways to reach this site, in plain words, with the addresses and what the agent checked |
| `[[open: page hub/workers]]` | a hub page on the left |
| `[[point: vault.value]]` | Rookie travels to that target and back |

Targets: `account.name`, `account.password`, `account.again`,
`account.save`, `vault.key`, `vault.value`, `vault.description`,
`vault.save`, `vault.list`, `network.local`, `network.home`,
`network.anywhere`, `network.status`, `page`. When the person saves in a
view, the site tells the agent ("[site] Secret host_password saved in the
vault", "[site] Account saved ...").

The work views are drawn by the shell (same page), so the agent can fill
and focus a field and Rookie can point at it; hub pages stay sandboxed
frames (Rookie points at the page as a whole).

- **Secrets:** the Vault view lists names (`GET /web/api/secrets`, never
  values) and saves with `POST /web/api/secret {name, value, description}`
  into the hub's vault. A password is never typed in the chat.
- **Root:** the agent opens the vault with `new=host_password`, then runs
  `shell.exec` with `argv: [sudo, -S, -p, "", ...]` and `stdin` = the vault
  reference (secret:host_password in double braces) plus a newline; the hub
  fills the value in at dispatch.
- **Reaching this site:** for anything but "Just this computer" the skill
  makes the agent check the binds, find the LAN address, detect the
  firewall (ufw, firewalld, nftables/iptables), open the site, MCP door and
  relay ports, and verify from the LAN address before saying it works
  (Bake's test hub listened on 0.0.0.0 while ufw blocked 8760).

### The brand

Bake, Nov 9: "get the styles from the old site. We spent a lot of time
tuning the brand". `static/shell.css` (shell, sign-in and account pages)
uses the 0.4 dashboard's rook-dark tokens (`rook/web/theme.css`,
`themes/rook-dark.json`) and bake-ware.github.io/rook: olive and amber, DM
Sans and IBM Plex Mono (Google Fonts, allowed in the shell's CSP), square
corners with the schematic corner tick, small spaced mono labels, the 0.4
chat's message look, and the rook mark from the landing page's icon.

### Pages in the sidebar, public pages, Workers

Bake, Nov 9: "Scroll chat to the bottom when it starts/opens. I want the
workers page to be the same as the older site. I don't like the archive
button where it is, archive should be a function on the sidebar per page.
Like an ellipse. in that ellipse I want a way to make a given page publicly
accessible for things like reports or reference".

- The chat shows its newest message when it opens, after its history
  loads, and as messages come (`scrollChat()` in shell.js).
- Each page in the sidebar has a ... menu: **Make public** (then the public
  link with Copy link) and **Archive**. The bar has no Archive button.
- `page.public {page, public}`: a public page opens at `/public/<page>` for
  anyone, no sign-in: the page alone (public.html), no sidebar or chat. Its
  calls go through `/web/api/public/*` and act as whoever made it public,
  within the page's own grants; making it private again stops the link and
  the page tokens it gave out. Kept in `pages.json` (`public`).
- **Workers** (hub/workers) is the 0.4 dashboard's Workers view on 0.5's
  data: workers / online / stale tiles, the versions bar, a heartbeat bar per
  worker, grouping by operating system, band or none (groups fold), sort,
  list or grid, and per row a band chip, the version, the battery (a battery
  plugin's status rides the heartbeat) and a heartbeat draining over 90 s,
  with the details (facts, plugins, what it can't serve) under the row.
  `hub.workers` gives `now` (the hub's clock) and `bands`. No 3D view and no
  per-worker actions yet.

## Rookie

Bake, Nov 5 2026: "Yeah add it and translate for 0.5. Add the rookie style
as the first question it asks the user", then: "Ok added sounds and themes
prompt the user if they want sound first, then rookie style then rookie
theme, then rookie name. This will be the name of the home agent". Rookie
is the house agent's avatar at the head of the chat, and the site's animated
tab icon. Bake, Nov 9: "make sure rookie like animates to these locations.
Like not just like pop in at random places. And only one instance of it on
any page at a time": there is exactly one live Rookie on a page (the chat's;
`Rookie.mount` is called once). When the agent points at something he lifts
off from the chat, hops along a curved path to it, settles, and goes back
the same way (shell.js `travel()`). The tab icon copies his canvas, and the
pickers (chat and Your account) draw still thumbnails with `Rookie.bake`.

- `static/rookie.js` is Bake's file, vendored unchanged (a drop-in: replace
  it and nothing else changes). Where it is loaded (shell.html,
  account.html) a comment says where it came from. The site takes styles
  and themes from `Rookie.SKINS` / `Rookie.THEMES`; the hub reads the same
  lists from the file (`SKINS.<name>={`, the `THEMES={...}` keys).
- Rookie comes in with `enter()` the first time he shows; `sound` follows
  the person's answer (off until they give one).
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
10. Public pages (`page.public`, Nov 9): anyone with the /public/<page> link
    opens the page without signing in, and its calls run as the person who
    made it public, within the page's own grants: writes included, and on
    an approvals page a public viewer could answer that person's holds. The
    public API takes no CSRF token and public page tokens (512 kept) live
    12 hours. Consider: read-risk calls only, no `approval.decide`, an
    unguessable link instead of the page id, rate limits.
11. Claude Code mode (`--ai claude`, Nov 9): agent turns run Claude Code
    with `--permission-mode auto` under the person's own subscription, so
    the house agent edits files and runs commands on this machine without
    asking. Consider a narrower mode or `allowed_tools`.

## OpenCode gotchas

- The free models refuse a custom system prompt (a custom agent or
  `instructions` hangs the turn on quiet retries), so `agents-opencode` puts
  the agent's instructions at the head of a new session's first message.
- `opencode run` reads stdin when it isn't a terminal: always give it a
  prompt on stdin or `</dev/null`.
- `--pure` hangs the free models.
