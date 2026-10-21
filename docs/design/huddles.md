# Huddles: roles, a chair, and Claude Code as the house agent

Status: H1 (section 3, the agent host) is built; H2 (roles, role homes and
role memory: 3.3, 4) is built as decided here; H3 (chaired huddles: 5, 7), H4
(the web: 8) and H5 (jobs and the lead developer: 6, 4.2) are built. Role
agency (2026-10-21: each role's workspace, the interim action tier the hub
keeps per cap, and sessions that report back by themselves: 4.5, 5.5) is
built; it is the precursor of H6's tiers.
Built choices where this document left room: a round starts on a person's or
a job's post, or a guest agent's that names the chair or a role; the chair
closes every round it routed (and a direct one where a role failed or two
answered), retrying failed roles and following up until the thread is
settled (5.1, as amended 2026-10-19); a role's sources are read at the
hub before each huddle or job turn (4.1); `rook_chat_start` and `chat.write`
start are off unless `chat.new_rooms` is on; replies are posted whole
(per-role progress, no token streaming). Related: [home-agent.md](home-agent.md)
(the house agent this extends), [memory.md](memory.md) (where role memory
lives), [persona.md](persona.md) (how agents talk, which is not the same as
what they care about), [sessions.md](sessions.md) (the Claude Code sessions
the lead developer fronts), [jobs.md](jobs.md) (guardrails, agent steps).

## 1. What Bake asked for

- Chat as it stands has no job of its own. The hub's chat store held 60
  messages from 2026-08-21 to 2026-10-14: about 40 were Bake asking one agent
  something, about 15 were agents leaving task-shaped notes for each other
  (work that belongs in tasks and handoffs), the rest tests. Nobody huddled.
- What Bake wants instead is a **huddle**: a group thread where he can ask
  "what's on deck?" and have agents with different concerns answer, the way a
  standup works in a software shop.
- Huddle members are **roles** Rook can run on demand (a project manager, a
  QA engineer, a lead developer), not Claude Code or Claude web instances,
  which only exist while someone has them open.
- Each role has **its own concerns and its own memory**. QA worries about
  different things than the project manager, and remembers different things.
- The **lead developer** is the front for all the Claude Code sessions.
- The **house agent chairs** the huddle: it presents, calls on roles, and
  closes with what should happen next.
- The house agent can **run as Claude Code** (Bake's subscription), with a
  model picker in the house agent settings. Haiku is the expected default, but
  it is a setting.
- First pass runs Claude Code on **cachyrig**, with settings to move it to
  another worker.

Status update: section 3 (Claude Code as the house agent) is built first, as
H1, and is documented in [home-agent.md](home-agent.md) 1.6 as it is. H2
follows the operator's decision of 2026-10-19: every role has its own home directory
on its agent host (4.4), which holds its memory (3.3) and the harness
configuration that declares its tools. A
`huddle` hub plugin already exists on master (agents post status and ask the
person questions in a group room, Work > Huddle; docs/manual/huddles.md). H3
builds on it rather than starting a second store.

## 2. Names

The persona plugin already exists and means *how an agent talks* (voice,
rules, formatting). The new thing means *what an agent is responsible for*,
so it gets its own name: a **role**. A role may name a persona profile for
its voice; it never replaces one.

| Term | Means |
|---|---|
| House agent | The `home` hub plugin's agent (`agent:home`). Chairs huddles. |
| Backend | What runs a model turn: `openai` (today) or `claude-code` (new). |
| Agent host | The worker that runs Claude Code turns (first pass: cachyrig). |
| Role | A charter (concerns, tools, model) plus its own memory. `role:<id>`. |
| Role home | The role's own directory on its agent host: memory and harness config (4.4). |
| Huddle | A thread with Bake, the chair and one or more roles. |
| Action card | A concrete proposal from a huddle (task, job, session) that Bake approves. |
| Workspace | `workspace/` in a role's home: its drafts and notes, writable with no card (4.5). |
| Action tier | What a role may do without a person, decided by the hub per cap: free, routine or approval (4.5). |

## 3. Claude Code as a house agent backend

### 3.1 Shape

The hub stays thin (it has under 1 GB of RAM). A Claude Code turn runs on the
agent host as a headless process and streams back to the hub:

```
hub (home plugin)  --band-->  agent host (worker plugin agenthost)
                                 claude -p  (prompt on stdin)
                                   --model <model>
                                   --output-format stream-json --verbose
                                   --setting-sources "" --settings .claude/settings.json
                                   --mcp-config .mcp.json --strict-mcp-config
                                   --tools <the role's built-ins, usually none>
                                   --permission-mode dontAsk
                                 cwd = the role's home (4.4)
```

That is the role turn (H2). The house agent's own turns (H1) run the same
way but in an empty scratch directory with a generated config, as described
in [home-agent.md](home-agent.md) 1.6.

The worker plugin `agenthost` (`rook/worker/plugins/agenthost.py`) owns the
process. Caps:

| Cap | Risk | Does |
|---|---|---|
| `agenthost.status()` | read | Harnesses found (claude, codex, opencode), versions, sign-in, running turns. Also in the heartbeat (`hb.agenthost`). |
| `agenthost.models(harness)` | read | Models the harness offers on that host. |
| `agenthost.turn(turn_id, prompt, system, mcp_url, mcp_token, harness, model, timeout_s, max_tool_calls, effort?, oauth_token?, role?, role_name, role_rev, tools?, builtin?, memory)` | exec | Runs one turn; returns `{ok, text, model, tools_used, usage, cost_usd, latency_ms, harness, role?, home?}`. Idempotent on `turn_id`. With `role`, it runs in that role's home (4.4). |
| `agenthost.cancel(turn_id)` | exec | Stops a running turn. |
| `agenthost.roles`, `role_memory`, `role_memory_edit`, `role_export`, `role_import`, `role_set_aside` | read to admin | The role homes on that host (4.4). |

Turns are stateless (no harness session files): the hub sends the context and
the agent host adds the role's memory each time, so there is no session to
resume, stream or reset. Live streaming of a turn (`agenthost.events`) is left
for H3/H4.

Rules:

- **One turn per role at a time.** Turns for the same role queue on the hub
  (they share one home and one memory); different roles run in parallel. A
  per-host cap (3 turns at once) bounds it.
- **A role turn runs in the role's home.** The working directory is the
  role's home directory (4.4), and the harness loads its tools from the
  config files there, never from the request. The hub sends the role's
  charter and persona as the system prompt with each turn; the agent host adds
  the role's memory digest from the home (3.3).
- **MCP is Rook plus the role's own memory.** The home's config names two
  servers: `rook`, the hub's per-turn tool server (`/home/turn-mcp`, bearer
  from `ROOK_TURN_TOKEN` in the environment, never on disk), and `memory`, a
  local stdio server over the home's `memory/` directory. The host's own user
  MCP servers, plugins and settings are not loaded (`--strict-mcp-config`,
  `--setting-sources ""`; Codex `--ignore-user-config`; OpenCode with
  its config dir pointed away). (`--bare` is not used: it requires API-key
  auth and ignores the subscription login.)
- **Turn limits.** The CLI has no turn cap flag in 2.1.x, so `agenthost`
  counts tool calls in the stream and stops the turn past `max_tool_calls`,
  and kills it at `timeout_s`. For a role turn the budget is kept first at
  the hub (amended 2026-10-19): the role's own `max_tool_calls`, else its
  preset category's (`max_tool_calls` in categories.toml: engineering 30,
  operations 24; a role saved without a category uses the category of the
  preset with its id), else `role.max_tool_calls` (12). The system prompt
  states the budget; the last three hub tool results carry
  `[budget: N of M tool calls left; ...]`; past the budget the hub's tools
  answer "tool budget spent: answer now with what you have". The agent host
  gets the budget plus 4 (the role's memory tools count there too), so a
  role is told before it is stopped, and answers with what it has.
- **Every cut is marked.** Wherever text an agent or a person reads is
  shortened, the cut ends with `[truncated: N of M chars]`
  (`rook/core/text.py` `clip`): a role's tool results
  (`role.tool_result_chars`, 12000; it was a silent 6000, the "~9,000"
  cut the first all-hands huddle hit), its pre-read sources (1500 each, 8000
  in all), the house agent's own tool results (4000), the turn's final text
  on the agent host (40000), the chair's transcript (the oldest part,
  12000), a card's summary (300) and stored reply (2000), and a chat message
  (8000, in the chat store). Claude Code's own MCP output limit is the
  harness's and marks its own cut.
- **Built-in tools are off unless a role needs them.** Default is the Rook
  and memory MCP tools only. A role that needs to read files can be given
  `builtin: ["read", "search"]` (Claude Code `Read`, `Grep`, `Glob`; OpenCode
  `read`, `grep`, `glob`, `list`; Codex has no file tools without its shell,
  which stays off, so it gets none). Shell tools are never offered to a
  role, and edit and write tools only inside its workspace (4.5).
- **Everything is journaled** as the house agent (`agent:home`, auth kind
  `home`) or, for a role turn, `agent:home/role:<id>`, with the agent host and
  turn id. The MCP calls the turn makes are journaled by the hub as usual under
  that token.

### 3.2 Settings (Manage > Home agent, group "Claude Code")

| Key | Default | Meaning |
|---|---|---|
| `home.backend` | `openai` | `openai` or `claude-code`. |
| `home.claude.worker` | `cachyrig` | The agent host. The picker lists workers whose heartbeat reports `agenthost` with Claude Code found. |
| `home.claude.fallback_workers` | `[]` | Tried in order when the agent host is offline or its Claude is not logged in. |
| `home.claude.on_unavailable` | `fallback_backend` | `fallback_backend` (use the `openai` settings), `queue` (hold until a host is back, up to the turn timeout) or `fail` (say so). |
| `home.claude.model` | `claude-haiku-4-5` | Any id or alias Claude Code accepts. The page offers a list (haiku, sonnet, opus and the current full ids) plus free text. |
| `home.claude.auth` | `login` | `login` (the host's own `claude` login) or `token` (`home.claude.token`). |
| `home.claude.token` | `""` | Vault reference only, `{{secret:<name>}}`: a long-lived token from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN`. |
| `home.claude.agent_token` | `""` | Vault reference to the Rook API token the turns use for MCP (the house agent's own principal, 3.4). |
| `home.claude.permission_mode` | `default` | Passed through; `bypassPermissions` is refused unless `allowed_tools` is Rook-only. |
| `home.claude.allowed_tools` | Rook MCP only | Extra built-in tools, by name. |
| `home.claude.max_turns` | `12` | Agentic turns per call. |
| `home.claude.timeout_s` | `180` | Per call. |

**Moving the house agent** is changing `home.agent_host` (as built). The next
turn goes to the new host; its own turns are stateless, so nothing is lost.
Roles do not follow it silently: a role's first turn creates its home on the
host it ran on and pins that host on the role (`host`), and a role moves only
with `role.move`, which carries its home (4.4). **Test** on the page runs one turn on the chosen host
with the form's values and shows the reply, model, latency and usage.

Usage counts against the subscription that is logged in on the agent host.
The page shows the last 24 h of turns and their token usage per role so Bake
can see what huddles cost.

### 3.3 Memory that survives a move (decided)

Claude Code's own memory files live wherever the harness keeps them, so they
would not follow a role to another host. Role memory therefore lives **in the
role's home** (4.4), in `memory/`, and moves with it. This is the memory
scope `role:<id>`. (The chair keeps using the hub's `memory` plugin, scope
`shared` and the person scopes, as today.)

- **Format.** One Markdown file per memory (`memory/<id>.md`, a small header
  with id, kind, created and author, then the text) and a generated
  `memory/MEMORY.md` index, so a person can read the folder directly. Kinds:
  `fact`, `procedure`, `episode`.
- **Writes** go through the home's `memory` MCP server (stdio,
  `python -m rook.worker.rolemem <home>`): `memory_recall(query)`,
  `memory_remember(text, kind)`, `memory_forget(id)`. Every write passes the
  memory plugin's write rules (rook/hub/plugins/memory/rules.py: secrets,
  prompt-injection phrases, invisible characters and code are refused).
- **Reads.** Before each turn the agent host appends a budgeted digest to the
  system prompt: memories that share words with the prompt first, then the
  newest, up to 4000 characters.
- **One source of truth.** Claude Code's auto-memory is off
  (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `autoMemoryEnabled: false` in the
  home's settings) and Codex's memories feature is off.
- **Per role:** `memory` = `write` (default), `read` (recall only) or `off`.
- **People read and edit it** through `role.memory` (read) and
  `role.memory_edit` (remember/forget, admin) on worker `rook`, which call the
  role's host. The Memory page's role filter (H4) is built on them.

### 3.4 Identity

As built in H1, a turn does not use a long-lived Rook API token: its tools
are served by the hub's per-turn tool server under a random token that dies
with the turn (home-agent.md 1.6). For a role turn that server offers exactly
the role's tools (4.1), and its `rook_call` refuses any cap outside the role's
`tools` patterns (and, for a role that only proposes, anything above
`read`), so the scope is fixed by the token itself and nothing in the request
can raise it. (This replaces the `X-Rook-Role` header idea.) The calls are
policy-checked as an agent principal `home:<name>/role:<id>` and journaled as
`agent:<name>/role:<id>`.

## 4. Roles

### 4.1 A role

One document per role:

| Field | Meaning |
|---|---|
| `id`, `name` | `qa`, "QA engineer". Identity `role:<id>`; people write `@qa`. |
| `description` | One line: what it is for. The chair routes on it (with `concerns` and `examples`) instead of the whole charter. |
| `category` | Optional preset category id (4.2). |
| `charter` | What it owns, what it never does, how it decides, when it asks a person (up to 2000 characters). In every turn's system prompt. |
| `concerns` | List: what it watches and is expected to raise unprompted. |
| `checklist` | List: what it works through on every turn (in the system prompt). |
| `examples` | Up to 5 questions it answers well (the chair sees 3). |
| `seed_memory` | List: what it knows to start with. In the system prompt until its home exists, then written into its memory once (`agenthost.role_memory_edit`, by `system:roles`) right after the first turn (or by `role.move` when a role is moved before it has a home). |
| `sources` | What it reads before answering (deck, jobs runs, journal, sessions, knowledge folders), as Rook caps with args. In H2 they are listed in its system prompt and it reads them with `rook_call`; H3 may pre-read them. |
| `tools` | Cap scope, the Jobs format (`allow`/`deny` patterns). Default `knowledge.read` only. `role.ask`, `role.move`, `role.memory_edit` and `agenthost.*` are never callable by a role. |
| `autonomy` | `propose` (default: reads and its workspace on its own, everything else a card), `routine` (also coding sessions on a branch, 4.5) or `act` (for now the same as `routine`; H6 gives it its tiers). The hub's tier decides, never the role. |
| `builtin` | Harness built-in tools: none (default), `read`, `search` (3.1). |
| `memory` | `write` (default), `read` or `off` (3.3). |
| `backend`, `model`, `effort` | Blank: the house agent's. Otherwise per-role override (the lead developer on Sonnet). Roles run on an agent harness (`claude-code`, `codex`, `opencode`); with the house agent on `openai`, a role names its own. |
| `max_tool_calls` | Optional, 1-100: the role's tool-call budget per turn. Blank: its category's, else `role.max_tool_calls` (3.1). |
| `host` | The agent host that keeps the role's home. Blank until the first turn, which runs on `home.agent_host` and pins that host here; changed only by `role.move`. |
| `persona` | Optional persona profile for voice. |
| `enabled` | Off roles are not called on. |

Stored by the hub plugin `roles` (namespace `role`, `<state>/plugins/role/roles.db`),
with revisions and an attributed history like persona profiles.

### 4.2 Starting roster (editable presets, by category)

Offered by `role.presets` (and on the Roles tab in H4); nothing exists until
someone saves one with `role.set`. As shipped, the architect and the lead
developer do routine work (4.5); every other preset only proposes. Every
charter says to keep notes and drafts in the workspace, and that tasks are
for work items, never a notepad.

**Presets are data.** `rook/hub/plugins/roles/presets/` holds
`categories.toml` (`[[category]]`: stable `id`, display `name`, sort
`order`, `description`, optional `guard`) and one `<id>.toml` per preset (a
role document plus its `order` inside its `category`). `model.load_presets`
validates them all at first use; a bad file is an error naming it. A
category's `guard` applies to every preset in it: `autonomy` is required (a
preset that says otherwise fails to load, never silently changed) and `deny`
patterns are added to each preset's `tools.deny`. A denied cap can still be
proposed as an action card; approving runs it as the person.

`role.presets` returns `{categories: [{id, name, order, description, guard?,
presets: [{id, name, description, category, autonomy, saved}]}]}` in sort
order (`full: true` for whole documents, `category` to narrow), or
`{preset, saved}` for one `id`. The Roles page shows them as one `<select>`
with an `<optgroup>` per category and the picked preset's description under
it, and fetches the picked preset whole.

The categories as shipped: Product and planning (`pm`, `product`, `analyst`,
`style`), Software engineering (`lead`, `qa`, `architect`, `security`),
Operations (`release`, `sre`), Hardware and fabrication (`cad`, `pcb`,
`firmware`, `fab`), Writing (`writer`, `editor`), Research (`research`,
`ml`), Media and promotion (`social`, `launch`, `community`, `content`,
`comms`; guard: propose, and posting, publishing, sending, shell and input
caps denied), Critique (`skeptic`; guard: propose) and Home and family
(`household`; guard: propose, and messages, voice, wake, camera and input
denied). The manual's table (manual/huddles.md, Presets by category) lists
them. The three the roster started with:

- **Project manager (`pm`)**: concerns: the task deck, what is blocked and why,
  priorities and scope creep, what is on deck next, deadlines Bake mentioned.
  Sources: `task` deck, handoffs, recent huddle decisions. Tools: read, plus
  propose task create/assign/reprioritise. Shipped tools: `task.read`,
  `huddle.read`, `job.read`, `knowledge.read`.
- **QA engineer (`qa`)**: concerns: failing or flaky jobs, test results on
  recent PRs, regressions after deploys, workers out of date or offline,
  anything shipped without evidence. Sources: Jobs runs, journal errors, PR
  checks, worker status. Tools: read, plus propose jobs and tasks. Shipped
  tools: `job.read`, `task.read`, `info.*`, `knowledge.read`.
- **Lead developer (`lead`)**: concerns: open PRs and branches, what every
  Claude Code session is doing and whether it is stuck, technical decisions
  and debt. Sources: `sessions.list` across workers, `sessions.follow` tails,
  GitHub PRs. Tools: read, plus propose `sessions.send` (nudge a session) and
  `work.stream.open` (start one with a task). The lead fronts the sessions; it
  does not write code itself. Shipped tools: `sessions.list`,
  `sessions.follow`, `task.read`, `knowledge.read`.

Anything else is a new preset file, or a role added from the page.

**Where code lives** (added 2026-10-19). Presets never name folders or
hosts (the repository is public). The hub setting `role.code_locations`
(one line per repository: its name, the worker and the folder of its main
checkout, and where worktrees go) is band data, and every role turn's
system prompt lists it, or says none are set and that the role must say so
rather than search. The engineering, operations, writing and firmware
presets' checklists point at it, and a `session.start` card's `cwd` must be
one of the folders named there (or inside one) when any are set. This
replaces guessing: the first all-hands huddle's lead searched two stale
copies and proposed a session in one of them.

### 4.3 Reaching one role

A 1:1 is a huddle with one role. Jobs can call a role directly (6). Under
both is `role.ask(id, question, context?)` on worker `rook` (H2): one turn of
that role on its host, in its home, with its tools and memory. Turns of the
same role queue at the hub (one at a time); different roles run in parallel.

### 4.4 Role homes (decided)

Every role has its own home directory on its agent host, like a user's home
directory. The reasons: Claude Code used as a plain model provider works
for text, but answers 403 when MCP tools are injected through the request;
letting each CLI load its own tools from config files in its working
directory avoids that, and Codex and OpenCode stay supported the same way.

```
<roles dir>/<id>/           mode 0700. Roles dir: agenthost.roles_dir, default <agenthost data>/roles
  role.json                 what the files below were rendered from (id, name, rev, tools, memory, workspace)
  memory/                   the role's memory, scope role:<id> (3.3)
  workspace/                its drafts, notes and working files: the only folder it writes (4.5)
  .mcp.json                 Claude Code: the rook and memory MCP servers
  .claude/settings.json     Claude Code: permissions allow exactly the role's tools; deny shell/edit/web; auto-memory off
  .codex/config.toml        Codex: the two servers with enabled_tools, built-in tool features off, approvals never
  .config/opencode/opencode.json
                            OpenCode: the two servers, tools {"*": false, <the role's>: true}, edit/bash/webfetch denied
  .rook/                    host-local scratch (the per-turn system prompt for OpenCode); not part of the role
```

- **It is the working directory.** Every turn of the role runs with the home
  as its cwd, whichever harness runs it.
- **Each harness loads its tools from the home:**
  - Claude Code: `--settings .claude/settings.json` with
    `--setting-sources ""` (the home's settings file and nothing of the
    host's), `--mcp-config .mcp.json --strict-mcp-config`,
    `--permission-mode dontAsk` (only what the settings allow runs), `--tools`
    limited to the role's built-ins. (Loading the same file as *project*
    settings does not work: Claude Code ignores `permissions.allow` from a
    project folder nobody has accepted the trust dialog for, and marking it
    trusted would mean editing the host's `~/.claude.json`. Checked with
    Claude Code 2.1.301.)
  - Codex: `--ignore-user-config` plus
    `-c projects={"<home>"={trust_level="trusted"}}`, so Codex loads the
    home's `.codex/config.toml` as project config while its sign-in stays
    in `CODEX_HOME`.
  - OpenCode: `OPENCODE_CONFIG` names the home's
    `.config/opencode/opencode.json`; its global config dir points at a
    per-turn temp directory holding only the host's model providers (never
    written into the home). (As *project* config, `opencode.json` in the
    home, OpenCode 1.18.35 fails every turn with "Unexpected server error"
    once the remote MCP server answers; the same file as custom config
    works.)
- **Rendered from the role, kept in the home.** The hub sends the role's tool
  list with each turn and the agent host rewrites a file only when it differs.
  The hub stays the authority: the files never name more than the turn's tool
  server serves, and a role is changed on the hub (`role.set`, the Roles tab),
  not by editing the files.
- **Moving a role is moving the folder.** `role.move(id, host)` packs the home
  on the old host (`agenthost.role_export`: a tar.gz without `.rook/`),
  unpacks it on the new one (`agenthost.role_import`), points the role at the
  new host and sets the old copy aside (`<id>.moved-<time>`, never deleted).
  The next turn re-renders the host-specific parts (the memory server's
  Python path).
- **No secrets in the home.** The turn token is only in the harness's
  environment; sign-ins stay the host's own.
- Caps on the agent host: `agenthost.roles` (homes here), `agenthost.role_memory`
  (read), `agenthost.role_memory_edit` (write), `agenthost.role_export`,
  `agenthost.role_import` and `agenthost.role_set_aside` (admin). The hub calls
  them as system code; people use the `role.*` caps on worker `rook`.

### 4.5 Agency inside its own space (built 2026-10-21; interim, before H6)

Bake (2026-10-21): roles had too much approval friction. The H6 design run
(huddle husky-stairwell-hive) showed it: the architect could not write a
draft anywhere, so it put the outline in a `task.create` card and planned a
`session.start` card after it, both waiting on Bake. A role now has real
agency inside its own space, and the hub, not the role, decides what that
covers.

**The workspace.** `workspace/` in the role's home (4.4) holds its drafts,
notes, scratch files and working documents. The role reads, writes, lists,
searches and deletes there with no card; nothing outside it is writable
this way. Two ways in, one folder:

- *Native*, on an agent host whose heartbeat says `hb.agenthost.workspace =
  "native"` and a Claude Code role: its `--tools` add `Read`, `Write`,
  `Edit`, `Glob` and `Grep`, and the home's settings allow `Read`, `Glob`
  and `Grep` under the home and `Write` and `Edit` only under
  `//<home>/workspace/**` (in `dontAsk` mode anything not allowed is
  refused; `Bash` and the web stay denied). Checked with Claude Code 2.1.306
  headless: a write outside the allowed root, `ls /tmp` and a push to master
  were refused, the allowed ones ran.
- *Hub tools*, for Codex and OpenCode (Codex has no file tools without its
  shell; OpenCode's path-scoped permissions are not relied on) and for any
  host that does not announce it: `workspace_list`, `workspace_read`,
  `workspace_write`, `workspace_edit`, `workspace_search` and
  `workspace_delete`, served by the per-turn tool server. The hub checks the
  path (relative, no `..`, no absolute or home paths, no link anywhere on the
  way) and runs the host's own `file.*` caps (`shell.exec rm` for a delete)
  as hub system code, journaled as the role (`role.workspace`).

People see the workspace read-only on the role's page (Manage > Roles,
**Workspace**: the file list, then one file's text; `role.workspace`).

**The interim action tier** (`rook/hub/plugins/roles/tiers.py`), kept by the
hub per cap and per argument, whatever the band's policy mode (the live
policy is in audit mode; switching it to enforce is H6's):

| Tier | What | Who |
|---|---|---|
| free | Reads in the role's `tools` (the cap's effective tier is `read`), its workspace, `session_status` | every role |
| routine | `start_session` (a Claude Code session on a branch, below), `session_send` (more work for its own stopped session) | autonomy `routine` or `act`; for `propose` it is an approval |
| approval | Everything else: any cap above `read` (merges, deploys, deletes and writes outside the workspace, settings, jobs, shell), any call on a worker in `role.protected_workers` (family devices: reads too), anything the hub does not know (`exec` by default) | an action card, always |

Secrets (`*secret*`, `vault.*`, `settings.worker_secret`, `*token*`,
`*password*`, `*credential*`, `auth.*`, `enroll*.*`) are approval for every
role and every autonomy; no setting lowers them. A role that calls an
approval-tier cap with `rook_call` in a huddle gets a `call` card made for
it (its `why` is the summary) and is told not to retry; outside a huddle the
call is refused with the reason. Policy is still checked after the tier.
A call that fails on its arguments comes back with the cap's parameters
(`<cap> takes: ...`), since a role sees cap names but not their arguments.
Roles never call `role.ask`, `role.move`, `role.memory_edit` or
`agenthost.*` (unchanged).

**Coding sessions a role starts** (`rook/hub/plugins/roles/sessions.py`).
`start_session(location, branch, instructions, title?)`:

- *Where:* one of `role.code_locations` (parsed: the name first, `worker
  <name>`, the main checkout (first absolute path), "worktrees go under
  <folder>" (default `<checkout>/.claude/worktrees`), the base
  `origin/<branch>`). The session runs in a worktree under the worktrees
  folder, made (or reused) for the branch; never in the main checkout.
- *Which branch:* a valid name that is not the base branch, `master`,
  `main`, `HEAD`, `trunk`, `production` or `release`. The worktree must be
  on it.
- *How:* `work.stream.open` on the location's worker, `harness: shell`, a
  fixed launcher (argv, no shell parsing of any value) that makes the
  worktree and runs `claude -p --output-format stream-json --verbose
  --setting-sources "" --strict-mcp-config --permission-mode dontAsk
  --settings <json>` with the prompt on stdin: none of the host's own
  settings, plugins or MCP servers, and an allowlist only: edits and reads
  in the worktree, reading the role's workspace, git for its own work
  (bare, after a `cd` into the worktree, or as `git -C <worktree>`), pushing
  only its branch, `git clean` (its own untracked scratch files),
  `gh pr create/view/list/diff/checks/comment/edit`, `rook-pytest`. Denied: `gh pr merge`, `gh api`, `gh repo`, force pushes,
  `git reset --hard`, `sudo`, `ssh`, `curl`, the web. So it can open a pull
  request and can never merge one.
- *Limits:* `role.sessions_per_role` (2) running per role, one session per
  worktree, `role.session_max_minutes` (180) per run, then it is stopped.
- The prompt tells it to print `ROOK-STATUS: WORKING|DONE|BLOCKED <text>`
  lines (DONE with the pull request URL).
- A role that only proposes is not offered the tool; a `role.session_start`
  card (worker `rook`) does the same thing on approval, as the person.

Gaps, recorded as findings rather than fixed here: the in-session rules are
Claude Code permission rules on Bake's own account, so branch protection on
the repository is what really stops a merge; Rook has no notion of a
"family device" beyond the `role.protected_workers` list; `act` has no
meaning of its own until H6.

## 5. Huddles

Hub plugin `huddle` (namespace `huddle`, runs in rook-band-mcp, store
`<state>/plugins/huddle/huddle.db`).

### 5.1 Flow

1. Bake posts in a huddle (web, phone, MCP, or a job).
2. The **chair** (the house agent) takes the turn first. It has three tools:
   `call_on(roles, ask)` (which roles answer, with a focused question each),
   `answer(text)` (it answers itself when no role is needed), and
   `propose(card)`. Bake's `@qa` skips routing and calls that role directly.
3. Called roles answer **in parallel**, each with its sources read and its
   memory loaded, each asked to stay under a length budget
   (`huddle.reply_chars`, 1200; the answer is posted whole, not cut there).
   Roles may `propose` action cards. A role whose turn fails or runs out of
   tool calls is asked again (`huddle.role_retries`, 1) with a narrower
   question that quotes why it stopped.
4. The chair closes the round, and decides there whether the thread is
   settled: a role failed or a question a role can answer is still open:
   it calls on that role again with a narrower question (`call_on` in the
   close, at most `huddle.max_followups` (2) times a round), and closes
   again after; the person is needed: it says what for; else a short
   synthesis. With one role answered and nothing open it replies `SETTLED`
   and nothing is posted. It closes every round it routed; a direct
   `@role` round only when a role failed or two answered. Roles do not talk
   among themselves (no loops; at most `huddle.max_role_turns` (6) roles per
   pass). A session's report (5.5) starts a round too: the chair calls on
   the role that started it, always, and on roles the thread asked to
   review its result.
5. After a round each role may write to its own memory (3.3). The huddle's
   decisions are recorded on the huddle.

Replies stream to the page from `agenthost.events` while they run.

### 5.2 Action cards

`{id, kind, by, summary, call: {worker, cap, args}, state}` where `kind` is
one of `task.create`, `task.update`, `job.run`, `job.create`, `session.send`,
`session.start`, `note`, `ask_bake`. State: `proposed` → `approved` /
`dismissed` → `done` / `failed`. Approving runs the call under Bake's identity
(the approver), journaled with the card and huddle ids. A role with
`routine` or `act` autonomy whose card's call is in the free tier (4.5) has
it run at once and the card posted `done`; a role that only proposes always
waits. Kind `call` (added 2026-10-21) is any other cap a person approves:
the hub makes one when a role calls an approval-tier cap (4.5). A
`session.start` card may run `role.session_start` (worker `rook`, args
`{role, branch, instructions, location?}`) instead of `work.stream.open`.
Guardrails (jobs.md 7) apply to all.

As amended 2026-10-19, after the first all-hands huddle's three approved
cards all failed (two `task.create` cards called a cap that does not exist,
"unknown capability: task.create"; a `session.start` card put its
instructions in `work.stream.open`'s `task`, "invalid task (a Rook task id
or slug)"), and its house agent proposed two versions of each task card:

- **Checked when proposed.** The worker must be on the band and have the
  cap (the refusal lists its caps of the same family). Task cards run
  `task.write`: a `task.create`/`task.update` cap and flat `{title, body,
  parent}` args are normalised to `{action, kind, data}`; `create` needs
  `data.title` and `data.parent`, `update` needs `id`. A `session.start`
  card's `task` must be a task id or slug, and its `cwd` one of
  `role.code_locations` when set (4.2). A refused card goes back to its
  proposer as the tool's error, so it can fix it.
- **One card per proposal.** A new card the same as one still `proposed`
  in the huddle (same kind, and the same call, or summaries alike by
  characters (0.85) and words (0.6), or call titles alike (0.8)) is not
  added: the proposer's own waiting card is revised in place, anyone
  else's is returned as the duplicate. `propose` takes `revise: <card id>`
  for the proposer's own waiting card, and the chair is shown the waiting
  cards in every turn.
- **Claims follow results.** The chair's message ends with a `Cards:`
  list made from what its propose calls returned (the card id and state, or
  why it was refused); its rules say never to claim a card, task or call
  before its tool call returned ok.
- **Failures say why.** A failed card keeps the reply as `result` and gets
  `error` (the denial or error, the reply's or its result's); the chair
  posts "Card <id> (<kind>: <summary>) failed: <error> [<cap> on
  <worker>]" in the room; `rook_huddle` read and the web show it. A
  `task.write` card runs with `request_id` `huddle-card:<id>`, so a retried
  approval never makes two tasks.

### 5.3 Caps and MCP

On worker `rook`: `huddle.list`, `huddle.read`, `huddle.start(title, roles)`,
`huddle.post(huddle, text, mention?)`, `huddle.card(huddle, card, action)`,
`huddle.archive`; the role caps from H2: `role.list/get/history/presets`,
`role.set/delete/move/memory_edit` (admin), `role.ask`, `role.memory`;
from 4.5, `role.workspace` and `role.sessions` (read),
`role.session_start`, `role.session_send` and `role.session_stop` (write).
One MCP tool,
`rook_huddle`, action-style like `rook_jobs`. External agents (Claude Code,
Claude web) can read a huddle and post into it as guests, but are never
called on.

### 5.4 Settings

`huddle.reply_chars`, `huddle.max_role_turns`, `huddle.retention_days`
(default 90), `huddle.default_roles` (`["pm","qa","lead"]`),
`huddle.role_retries` (1), `huddle.max_followups` (2). On the roles plugin:
`role.max_tool_calls` (12), `role.tool_result_chars` (12000),
`role.code_locations` (`[]`), and from 4.5 `role.protected_workers` (`[]`),
`role.session_model` (`""`), `role.session_stall_minutes` (20),
`role.session_max_minutes` (180), `role.sessions_per_role` (2).

### 5.5 Report-back (built 2026-10-21)

A session a role started reports to that role and to the huddle it was
started from, with no watcher job: the hub follows it. The roles plugin
keeps a record per session (`role_sessions` in roles.db: role, huddle,
worker, terminal, Claude Code session id, branch, worktree, state, newest
status, pull request, read cursor) and one follower task per running
session, which long-polls `work.stream.read` (20 s) and folds the
stream-json (`Follow`):

- `ROOK-STATUS: WORKING` lines update the record and are posted quietly in
  the huddle (at most 4 a run), sender `session:<speakable name>`.
- When the run ends it sends **one** report: its DONE, BLOCKED or QUESTION
  line, with anything unexpected added (Claude Code's error, a non-zero
  exit, tool calls it was refused); UNEXPECTED when it said neither.
- Also UNEXPECTED, at once: no output for `role.session_stall_minutes`
  (once a run), its terminal vanishing (its host restarted), still running
  after `role.session_max_minutes` (then it is stopped).
- A report is posted in the huddle and starts a round (5.1) in which the
  chair wakes the role. A report round that waited behind another round is
  skipped when the role has spoken since the report was posted (it saw it). Without a huddle the role gets one turn with the
  report. Each report goes out once per run.

The follower resumes from the stored cursor when the hub restarts.
`session_send` resumes a stopped session (`claude -p --resume`, same
branch and worktree) with more work. `role.session_stop` closes a running
one.

## 6. Jobs

- Agent steps accept `agent: "role:<id>"` (a role answers with its sources
  and memory) as well as `home`.
- New step kind `huddle`: `{roles, prompt, deliver}`, which runs one round and
  delivers the chair's synthesis (Telegram, voice, notification) with the
  cards linked. Example: a weekday 08:30 standup.

## 7. What happens to chat

- The **Chat** tab becomes **Huddles**. Old chat rooms are archived read-only
  under a filter; nothing is deleted.
- The Home page chat panel opens the 1:1 huddle with the house agent instead
  of its own room.
- The MCP instructions stop telling agents to coordinate in chat; they point
  at tasks and handoffs. `rook_chat_*` stays for one release, read-only for
  new rooms, then is removed. `rook_home_ask` is unchanged.
- The worker `chat.py` local transcript plugin is unrelated and unchanged.

## 8. Web

Minimal, per Bake's standing rule (no chatty status, only actionable notices):

- **Huddles**: a narrow list of huddles on the left (collapses on a phone), the
  thread on the right. Each role's reply has its role chip and streams in.
  Action cards sit inline with Approve / Dismiss. A multi-line composer;
  `@role` completes. No presence sidebar.
- **Roles** tab: charter, concerns, sources, tools, autonomy, model; a link to
  the role's memory on the Memory page; history.
- **Manage > Home agent**: the backend switch and the Claude Code group (3.2),
  host picker with status, Test.

As built (H4):

- **Huddles** (`rook/web/huddles.js`, styles in `huddle.css`, served by the
  dashboard's `HuddleWeb` proxy) replaces the Chat tab; `#chat` and
  `#chat?room=<id>` redirect to `#huddles` (`?room=` opens that huddle, or
  that old chat room). The list: chaired huddles by last activity, then
  project and band status huddles; a filter for **Archived** and **Old chat
  rooms** (the dashboard's `/api/chat/rooms` minus every huddle's room, read
  with `/api/chat/read`, no composer). Badges count open cards (chaired) or
  open questions (status huddles). Status huddles open as a plain thread
  with a link to Work > Huddle (statuses, answering questions).
- The thread polls `view` (`since_seq`): 1.5 s while the newest round is
  `chair`/`roles`/`closing`, 6 s otherwise, never while the view or the page
  is hidden, and redraws only when something changed. Running rounds show one
  "answering" line per reply still `running` (from `rounds[-1].replies`), or
  "thinking"/"summing up" for the chair. Live token streaming is not built:
  replies appear whole (`agenthost.events` stays future work).
- Cards attach to their `seq`; a card without one goes after the last message
  of its round; cards older than the loaded messages go first. The only
  notices: the house agent is off (link to Manage > Home agent), and open
  questions in a status huddle (link to answer them). A failed card shows
  "Failed: <why>" (the card's `error`).
- New huddle: title and role checkboxes; checked are `huddle.default_roles`
  that exist and are on, else every role that is on (the `roles` action
  returns `default_roles`).
- **Manage > Roles** (`rook/web/roles.js`): list, form for every role field,
  Move (agent hosts with harness and sign-in status), history, presets,
  delete, a link to `#memory?role=<id>`. The **Memory** page has a "whose
  memory" menu: a role's memory through `role.memory` / `role.memory_edit`.
  Both use `/huddle/account-api` role actions (`role_list`, `role_get`,
  `role_history`, `role_presets`, `role_set`, `role_delete`, `role_move`,
  `role_memory`, `role_memory_edit`), which call the `role.*` caps with the
  operator account's principal (a band owner, so the roles plugin's admin
  gate passes) and `human:<username>` as the actor.
- **Manage > Home agent** adds "Last 24 hours on agent hosts": turns, failed
  turns, tokens and cost per role (`""` = the house agent, chair included),
  from an in-memory ledger in `AgentHostBackend` (since the MCP server
  started; `home` page `usage`). Its chat panel is the 1:1 huddle
  ([home-agent.md](home-agent.md) 1.3).
- Phone (under 760 px): one column; the list, then the thread with a back
  button.

## 9. Workstreams (each one PR into `beta-huddles`)

- **H1 Agent host and the Claude Code backend.** `agenthost` worker plugin
  (spawn, stream-json parsing, queueing per role, cancel, heartbeat), the
  `home.backend` switch and `home.claude.*` settings, fallback, Test, usage.
  The house agent's chat replies, `home.ask` and job steps work on either
  backend. Tests use a fake `claude` executable that emits recorded
  stream-json.
- **H2 Roles, role homes and role memory.** Role store and caps (`roles`
  plugin), a home directory per role on its agent host (4.4) with each
  harness's config rendered from the role's tool allowlist, the turn run with
  cwd = the home, `role:<id>` memory in the home with its stdio memory server
  (3.3), per-turn tool scope (3.4), `role.ask` and `role.move`. Ships the
  three default roles as presets (`role.presets`).
- **H3 Huddles.** Store, chair flow (call_on / answer / propose), parallel role
  turns, action cards and approval, caps, `rook_huddle`, chat migration.
- **H4 Web.** Huddles view replacing Chat, Roles tab, Home agent settings
  group, phone layout.
- **H5 Jobs and the lead developer.** `role:<id>` agents and the `huddle` step
  kind; the lead's session sources and nudge/start cards.

H1 lands first (everything else needs a backend that can run roles). H2 and
H4's settings group can follow in parallel; H3 needs H1 and H2; H5 last.

## 10. Rules for every PR

As in jobs.md 10: tests run with a scratch `HOME`, never the real one; no
secret values anywhere (`{{secret:name}}`); nothing installed on or changed
on the work laptops; the agent host is cachyrig for live testing; docs and
the settings/tools references regenerated in the same PR.
