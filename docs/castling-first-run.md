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
`rook`), `--http-port 8740`, `--mcp-port 8741`, `--relay-port 7474`, `--level`, `--ai`,
`--no-boot`, `--no-browser`; `--open` makes a new sign-in link,
`--uninstall` stops it and moves the folder aside.

What it does:

1. Asks which level (`--level`): minimal (the plugins whose manifest says
   `package: core` or `package: devices`: a worker's caps, many of which run
   on the hub), assistant (plus `package: agents`) or team (plus
   `package: workbench`, the default). Those plugin folders are linked into
   the hub's library; this machine's worker runs `shell` and `file` and, with
   agents, the engine. Minimal skips the next two steps
   and the house agent.
   Then, with agents, what you use for AI now. "Claude Code" (1, `--ai claude`) and
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
4. `hub init --no-password`. Of its output the installer shows only the
   recovery key, at the end with one plain sentence; the owner's first API
   token stays in the script and enrolls this machine (more tokens: the
   Tokens page), and init's developer lines ("Start it", "Add a machine")
   aren't shown. The site's address is printed once, at the end. The install facts go to the agents plugin's
   `hub/data/plugins/agent/setup.json` (`install`), which `agent.setup`
   returns to the house agent.
5. Writes and starts `NAME-relay`, `NAME-hub`, `NAME-worker` (systemd user
   units; enabled at login unless `--no-boot`). The worker runs `shell`,
   `file` and the backend: `agents-claude-code` (`claude` = the tested
   path, `permission_mode: auto`, `door_url` = this hub's MCP door; the
   agents plugin's `backend` setting is `agents-claude-code.turn`) or
   `agents-opencode` with the tested model (its own tools on); security is
   off. The site, MCP door and relay listen on every interface
   (0.0.0.0).
6. Opens the browser on the hub's address. A new hub is open until it is
   claimed (castling-design, Agents: "creating the first user claims it"):
   `init --no-password` leaves `hub/data/unclaimed`, and while it is there
   `/` signs the browser in as the (first) owner and `/login` sends it
   there too. Setting any password removes it for good; a hub without it
   (every hub made before) is never open. This is core's sign-in, with or
   without the agents package. Once the owner sets a password (step 5 of
   the walk-through) a new browser gets the sign-in page, even while guided
   setup carries on; the browser that set it stays signed in.
   After that, `hub login-link` (and `--open`) makes a sign-in link that
   keeps working: no time limit, any number of uses (Bake, Nov 6: "This
   security shit nobody asked for. Why is there a secure only 15 min thing
   now?"). Codes are kept as they are in `login-links.json`.

## The site

Bake, Nov 9 2026: "I want this to be more like a two pane thing where like
the chat and rookie are on one side and the left is like stuff we're setting
up. Have the rookie avatar move to things it's talking about on the page."
and "I want it to guide the setup entirely".

While `agent.setup` says setup isn't done, the site is two panes, no page
list: the **work pane** on the left (what is being set up right now) and the
house agent's chat on the right, with Rookie at its head. After setup the
same chat is the right column beside every page (Rookie in the corner
opens it, its X closes it), and pages fill the left. The agents plugin
keeps the state in its own data (`setup.json`), which the installer writes
with `done: false`; with none, setup counts as done, so an existing hub
never starts the walk-through by surprise. A hub made by hand with `init`
gets it only after `agent.setup {done: false}`.

The house agent follows the `first-run` skill
(rook/plugins/agents/skills/first-run.md): look and feel (sound, Rookie's
style, theme, name), the person's name and a password (account view), what
the hub is for, reaching this site (with the firewall directive below),
other machines (what a worker is first), voice / phone / Telegram / Discord,
backups, then a knowledge page "How this hub is set up and why", and
`agent.setup {done: true}`.

### Right after an install

The worker joins a few seconds after the installer starts it, and until it
serves `agent.backend` no turn can run. `agent.site` says `ready` (some
provider of `agent.backend` installed and its worker online); the site
shows "Getting ready..." and starts the walk-through only when it is true
(after the look and feel, which the site asks itself; see Rookie below).
A turn that fails because nothing serves the backend yet is never shown as
a message: the site waits and sends the kickoff again, and on a reload it
starts again when the agent never got to answer. The installer waits for
the same `ready` (through `agent.site`) before it says Done.

While setup isn't done, every turn of the house agent carries the first-run
skill in its message (`setup_brief` in the agents runtime, from
`agent.setup`'s state): OpenCode keeps only a session's first instructions, and
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
| `[[point: vault.value \| Enter the password here]]` | Rookie travels to that target, says the words in a bubble there (default "Right here") and goes back |
| `[[say: The dashboard task is done]]` | a speech bubble beside Rookie, wherever he is; a click opens the chat at that message |

Targets: `account.name`, `account.password`, `account.again`,
`account.save`, `vault.key`, `vault.value`, `vault.description`,
`vault.save`, `vault.list`, `network.local`, `network.home`,
`network.anywhere`, `network.status`, `page`. When the person saves in a
view, the site tells the agent ("[site] Secret host_password saved in the
vault", "[site] Account saved ..."). Those `[site]` notes are the agent's
only: the chat never shows them as the person's words. A note can carry the
person's own words for it on a `[shown]` line ("Saved my account", "Saved
host_password in the vault"), and that is what the chat shows as theirs.
A view whose job is done shows it (a tick, "Saved: Bake", "You'll sign in
as bake", a way to do it again), on a reload too, never a blank form the
conversation has left. After a reload the field the view was opened for
has the cursor again. The account view shows the sign-in user name (the
one the installer made from the computer's login): what the agent calls the
person is only a name.

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
  `hub.workers` gives `now` (the hub's clock) and `bands`. **3D** is the
  0.4 fleet scene (one piece per worker, the hub as the rook). No per-worker
  actions yet.

### The Rook look, the 3D rook, Knowledge and Trust map, Rookie in the corner

Bake, Nov 9: "The like workers background is missing. I liked the 3d rook
from the old version. Let's bring over the trust map and knowledge pages
too. make sure the default styles are using the old rook style." And: "Also
for the house agent chat, I want to have like an X at the top right, and move
the rookie avatar to the bottom right corner when closed. Remove the chat
button from the like top nav on pages. Clicking rookie opens the chat
sidebar. I want rookie to make little chat bubbles too with short like
"Right here" "Enter username and password" "The dashboard task is done" etc".

- **The Rook look is the default everywhere.** `static/page.css` (injected
  into every page) carries rook/web/theme.css's rook-dark tokens and the 0.4
  controls: square corners, DM Sans and IBM Plex Mono, olive and amber.
  Earlier page names (`--muted`, `--card`, `--ok`) map onto them. The fonts
  are the hub's own files (`static/fonts`, OFL, the ones Google Fonts serves
  0.4): the shell, account, sign-in and public pages load them with
  `fonts.css`; pages, which have no network, get them inlined as `data:`
  fonts.
- **The margin drawing** behind every page, as on 0.4: the SVG sketch, then
  the three.js rook (`static/rook-scene.js`, built from rook/web/scene,
  loaded by `art.js` when the browser is idle). Pages are clear, so it shows
  behind them; panels sit on `--overlay`. "Pause artwork" at the foot of the
  sidebar stops it.
- **Libraries for pages**: a page can't load scripts, so
  `<script type="module" data-rook-lib="rook-scene"></script>` is replaced
  with the library, inline, its exports on `window.RookLib["rook-scene"]`
  and a `rooklib` event when it has run (`site.py` `LIBS`). The Workers
  page's 3D view uses it.
- **Knowledge** (knowledge/knowledge) is the 0.4 Knowledge view on 0.5's
  records; `knowledge.review` keeps 0.4's verify / dispute. **Trust map**
  (trust/trust) is the 0.4 Trust map on 0.5's model.
- **The chat**: an X at its top right closes it (in setup it also skips to
  the hub, as "Skip to the hub" does). The bar has no Chat button. When the
  chat closes, Rookie walks out of it to the bottom-right corner and waits
  there (still the one instance); a click on him opens the chat and he walks
  back.
- **Rookie's speech bubbles**: a few words beside him, wherever he is. The
  agent says them with `[[say: ...]]`, or `[[point: TARGET | words]]` when
  he points at something (default "Right here"). With the chat closed the
  site makes them too: a reply's first few words, "That task is done", "Your
  new page is ready", "I need your OK" for a hold. They fade after a few
  seconds; a click opens the chat at that message.

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
pickers (the look steps and Your account) draw still thumbnails with `Rookie.bake`.

- `static/rookie.js` is Bake's file, vendored unchanged (a drop-in: replace
  it and nothing else changes). Where it is loaded (shell.html,
  account.html) a comment says where it came from. The site takes styles
  and themes from `Rookie.SKINS` / `Rookie.THEMES`; the hub reads the same
  lists from the file (`SKINS.<name>={`, the `THEMES={...}` keys).
- Rookie comes in with `enter()` the first time he shows; `sound` follows
  the person's answer (off until they give one).
- Look and feel = `page.look {sound, rookie, theme, name}`; it returns
  `{sound, rookie, theme, name, chosen, next, styles, themes}` (also in
  `agent.site` as `look`). Sound, style and theme are the
  person's own settings (`page.sound`, `page.rookie`, `page.rookie_theme`);
  the name is the hub setting `agent.house_name` (default Rookie): the
  house agent's name. Anyone (a person, or an agent alone) may set any of
  them; an unknown style or theme shows as the default. Its prompt says
  "You are <name>", the roster shows it, the dock header and the tab title use it.
  Its identity stays `agent:house`. All four change later on **Your
  account**.
- First-run steps 1-4 are sound, style, theme, name, in that order (`next`
  is the first unanswered). The site asks them itself, on the left (Bake's
  review, Nov 9: "Put the look-and-feel there ... so the four steps happen
  on the left like everything else and the chat just narrates", and "Make
  them instant"): a stage the one live Rookie walks onto and is shown big
  on (`.staged`, CSS-scaled; the thumbnails are still pictures), the four
  steps with what was chosen (a click goes back to one), and the step's
  picker. A click calls `page.look`, Rookie changes at once, and the next
  picker shows with a line in the agent's voice in the chat (and a few
  words from Rookie); no agent turn. The agent hears once, when all four
  are done (the setup kickoff, with what was picked), or earlier when the
  person types instead of clicking (it then sets a typed answer with
  `page.look` and the site moves on). Then the rest of setup, the agent
  speaking as its new name. Before the review each step was a full agent
  turn (10 to 30 s with Claude Code; a typed name took over 90 s).

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

1. Who may mark first-run setup done (`agent.setup {done}`): was owners only;
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
6. Installer file modes: the install folder was 700, install.log, hub.yaml
   and setup.json 600; now default modes. (Output: init's API token was
   printed for a while; since the review pass only the recovery key is.)
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
12. Trust map (`trust.map`, `trust.sacred`, Nov 9): anyone who can call
    `trust.sacred` (rights and risk `admin` only, no owner-only rule) marks a
    machine sacred or not and it is written to hub.yaml; anyone who opens
    the page sees every agent's grants and every project's level. Consider:
    owners only for `trust.sacred`, and grants shown only to owners.

13. ~~Setup mode and the network step: while setup is unfinished anyone who
    opens the site is the owner, and step 7 can open the hub up before then.~~
    Obsolete: the site is open only until the owner sets a password (step 5,
    before step 7), not until setup is done. Left: a person who skips the
    password and still opens the hub to the network leaves it open; consider
    no open sign-in from anything but this computer.

### Open questions for Bake

- **Twelve steps before the first useful thing** (review pass, Nov 9).
  Steps 9 to 11 (voice / phone / chat apps, backups, the write-up) could be
  offered after setup is marked done instead of before it. Not changed.

## OpenCode gotchas

- The free models refuse a custom system prompt (a custom agent or
  `instructions` hangs the turn on quiet retries), so `agents-opencode` puts
  the agent's instructions at the head of a new session's first message.
- `opencode run` reads stdin when it isn't a terminal: always give it a
  prompt on stdin or `</dev/null`.
- `--pure` hangs the free models.
