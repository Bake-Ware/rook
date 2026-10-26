# Huddles: roles, a chair, and Claude Code as the house agent

Status: H1 (section 3, the agent host) is built; H2 (roles, role homes and
role memory: 3.3, 4) is built as decided here; H3 (chaired huddles: 5, 7), H4
(the web: 8) and H5 (jobs and the lead developer: 6, 4.2) are built. Role
agency (2026-10-21: each role's workspace, the interim action tier the hub
keeps per cap, and sessions that report back by themselves: 4.5, 5.5) is
built; it is the precursor of H6's tiers. H6 (10, as built 2026-10-23) is
the house agent's front door, the trust map, onboarding and the conference
table with votes.
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
the repository is what really stops a merge (since 2026-10-23: `enforce_admins`
on master, a per-session push guard, and minted per-session tokens once a
GitHub App is set up; docs/design/trust.md 5); Rook has no notion of a
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

## 10. H6: the house agent's front door, the trust map and the conference table (as built 2026-10-23)

Status: built 2026-10-23 (overnight track B, phases B1 to B4), all merged
and live (B4, the conference table and votes, 10.5 to 10.7: PR #175, hub
919.rustic.turnip). Verified live on [TEST] pull requests into a throwaway
branch: one pinned, gated, voted yes by lead, qa, security and skeptic, and
merged by the hub at the voted SHA; one blocked by all four and asked of the
person. This replaces the H6 proposal of 2026-10-21 (episodes, intake,
report-back and action tiers; PR #147, huddle husky-stairwell-hive), which
is not built in that form. Vision: Rook knowledge page `house-agent-vision`.

### 10.1 What changed from the 2026-10-21 proposal

The proposal put a new layer (episodes behind an `intake.request` cap)
between Bake and the chair. Bake's vision (2026-10-22) put the house agent
there instead: it is his assistant and the front door of the band, and a
huddle is one of the tools it reaches for. So:

| Proposed (2026-10-21) | As built 2026-10-23 |
|---|---|
| `intake.request` from every client | The house agent's front door: every client already reaches it (`home.ask`, the chat panel, chat rooms, the voice Background, MCP); one router per request (10.2) |
| Episodes: a new huddle kind with a store, budget and migration | A chaired huddle the front door opens (`open_huddle`) or continues (`continue_huddle`); no new store, nothing migrated |
| Action tiers (just do it / do it, then tell / ask first) replacing `autonomy` | The trust map (sacred / guarded / ship-it), enforced by the hub on every holdable principal (10.3); the interim per-cap tier for roles (4.5) stays |
| Merges and deploys ask first (M1) | Votes at the conference table, carried out by the hub (10.5 to 10.7): merges and ready-for-QA go after a unanimous vote, deploys and ships still ask |
| Report-back (10.5 of the proposal) | Built for coding sessions before H6 (5.5); job runs do not report back |
| Roles calling roles, per-episode budgets | Not built (10.10) |

Nothing in sections 4 to 9 is amended by this section; the "As amended"
notes the proposal added to 4.1, 4.2, 5.1, 5.2, 5.4, 7 and 8 are dropped
with it. Role `autonomy` keeps its 4.5 meaning. H6 went into `master`, not
`beta-huddles` (section 9 is the H1 to H5 history).

### 10.2 The front door (B2)

[front-door.md](front-door.md). Every request that reaches the house agent
goes through one router turn with five paths: **answer**, **do** (a
reversible action the trust map says `go` to, run as the house agent, never
on a family device), **dispatch** (a coding session for the lead through
the roles plugin's own path, 4.5), **huddle** (open or continue a chaired
huddle with the roles) and **ask**. The reply's first line is written by
the hub from tool results, so the model cannot claim something was done.

Asks are one line with Yes / No / Explain, kept in `front_asks`, delivered
through the relayed approvals list (kind `house`): the phone's decision
card, `rook_approvals` and the dashboard's **Decisions** page. What the
house agent decided on its own lands in the **digest** with its citation
(trust entry and the person's words, or a playbook rule); **overturn** only
tightens. The **playbook** holds Bake's standing rules; the trust map
always wins over it. Setting `home.front_door` (off by default; on for the
live hub).

The chair of every huddle is the house agent, so the front door and the
chair are one identity with one set of limits (10.3).

### 10.3 Trust map and session isolation (B1)

[trust.md](trust.md). One versioned record (`trust.json`, with an
append-only history) says, per host, repository, project and resource, how
much agents may do alone: **sacred** (only on Bake's direct instruction;
agents read, propose, prepare), **guarded** (vote, act, then tell; an ask
for anything without a vote), **ship-it** (just go). Targets not on the map
are `balanced`, which is the hub's behaviour before the map (changes go,
admin asks).

It answers the proposal's action tiers:

- **Enforced by the hub, not by prompts.** The approval layer rules on every
  call from a holdable principal (agents, roles, integrations, the house
  agent itself) by cap kind (read, prepare, change, admin) and level; an
  `ask` or `person` ruling is a hold that cites the entry, its version and
  Bake's words. The map and the hard stops are the only holds (access v2).
- **Only the person loosens it.** The house agent may only tighten; other
  agents propose; changes are never relayed (`trust.*` is in the relay
  FLOOR). Exceptions are scoped and expire, and never cover what they
  exclude (family devices).
- **Roles** keep the interim tier (4.5) on top of the map: reads and their
  workspace are free, `start_session` and `session_send` are routine,
  everything else is an action card, and secrets (`*secret*`, `vault.*`,
  `settings.worker_secret`, `*token*`, ...) are an approval for every role,
  with no setting that lowers them. A routine session in a guarded or
  sacred code location becomes a card.

Open question 5 of the proposal (sessions could push master under Bake's
admin credential, card c_839e89992532) is settled:

1. `enforce_admins` is on for `master` (2026-10-23): GitHub refuses a direct
   push from anyone, admins included (probed live: GH006).
2. Every session gets a push guard (a pre-push hook outside the worktree)
   that refuses any ref but its own branch; `tests/test_trust.py` runs the
   real launcher with a fake `claude` and its `git push origin HEAD:master`
   is refused.
3. Minted per-session GitHub App tokens (one repository, contents and pull
   requests only) and a sandbox that hides the host's credentials are
   built; they switch on when Bake creates the GitHub App. Until then
   sessions still run under the host's own GitHub credential, held back by
   1 and 2. The repository ruleset that stops the app from merging is also
   Bake's step.

The band's policy file is still in audit mode (4.5); the trust map and the
interim tiers are enforced whatever that mode is.

### 10.4 Onboarding and the borrowed-state ledger (B3)

[onboarding.md](onboarding.md). A brainstorm with the house agent is one
thread; when it turns to building, the hub offers once to set up a project
and asks three questions in fixed words (where it lives, how protected it
is, what it touches). Answers must be in the person's own message. Setup
makes the folder or repository, a wiki page, a project record, a trust
entry (guarded and sacred apply at once; ship-it is a proposal) and a
starter team (toy: lead; guarded: lead, qa, critic; sacred: lead, qa,
security, critic).

A sacred host's normal state is recorded as a **baseline** (exact stop and
restore calls). A project may borrow a baseline resource under a **scoped
exception** (a proposal Bake accepts on the Trust page, with an expiry);
the exception covers only that recorded stop or restore call, nothing else
on the host. Every borrow is a lease in the **ledger** with the restore
call; a sweep offers to restore when the exception ends, the project ends
or goes quiet.

### 10.5 The conference table (B4)

`rook/hub/plugins/huddle/table.py`, tables `huddle_artifacts`,
`huddle_motions`, `huddle_ballots` (migration `003_table.sql`). In a
chaired huddle the chair (tools `pin`, `call_vote`) or a person (caps
`huddle.pin`, `huddle.vote`) puts artifacts **on the table**, each pinned
to an exact version:

| Kind | What | Version |
|---|---|---|
| `pr` | a pull request (repo, number) | its head commit SHA |
| `commit` | a commit | its SHA |
| `file` | a file in a repository | the commit SHA it was read at (and its hash) |
| `doc`, `tests` | pasted text: a document, test output | SHA-256 of the text |
| `link` | a screenshot or preview build | SHA-256 of what the hub fetched |

Every role in the huddle reads what is pinned through the hub with
`table_read`, always at the pinned version: `summary`, `diff`, `files`,
`file` (a path), `checks` (CI at that commit), `comments` (the PR's),
`content` (a pinned document or link). Long text is paged. A read of a PR
that has moved on says so. Each read is journaled (`huddle.table.read`)
with the reader. Pinning the same PR or file again at a new version
supersedes the old pin and voids any open vote on it.

This fixes the proving run's "reviewers read the worktree, not the
commit": in the H6 design run a review of f758209 was answered from a
worktree already at a23064c. Reads now come from GitHub at the pinned SHA,
never from a role's own checkout. Only a person or the house agent pins:
`huddle.pin` refuses anyone else (guest agents ask the chair), and roles
have it on the roles plugin's never-call list.

### 10.6 Votes

**Who decides a vote is needed.** The chair, when work is ready for an
action, calls `call_vote(kind, artifact)`; kinds are `merge` and `qa`
(ready for QA, on a PR) and `deploy` and `ship` (on a commit or PR, to a
deploy target from `huddle.vote_deploy_targets`). Only a person or the
house agent calls a vote; roles never do. The hub checks the trust map for
the target (the PR's repository and base branch, the deploy target's
worker; a family device counts as sacred): **ship-it** may skip the vote
(`skip_vote`; the gates still run), **guarded** and unlisted always vote,
**sacred** votes and then always goes to the person. An active exception
changes rulings on calls, never whether a vote is needed.

**Required voters** are data (`huddle.vote_voters`); a seat `a|b` is the
first of those roles that exists, is on and may vote:

| Action | Seats |
|---|---|
| merge | lead, qa, security, critic\|skeptic |
| qa | lead, qa, critic\|skeptic |
| deploy | release\|devops\|sre, qa, security, critic\|skeptic |
| ship | release\|devops\|sre, qa, security, critic\|skeptic, product |

Stand-ins (`huddle.vote_alternates`): lead -> architect. A seat nobody can
fill counts as couldn't verify.

**Who never votes.** Authors: a role whose coding session wrote the branch
or pull request (from the session records), and a role that pinned it. The
proposer, if a role. The chair never votes.

**Hard gates first, not voted on** (`GATES`): `version` (the PR is still at
the pinned head, open, not a draft for a merge), `mergeable` (merge only),
`ci` (every check on the voted commit green; `huddle.vote_require_ci`),
`tests` (a check matching `huddle.vote_test_checks` passed), `secrets` (no
credential-shaped string in the diff), and for deploy and ship `rollback`
(the target's dry-run call passed). A failed gate stops the vote with the
reason; the gates run again just before execution.

**The ballot.**

- *Blind.* The required voters vote in parallel, each in a turn of its own
  with `table_read` and `vote`. A ballot is sealed when cast: the huddle
  sees "@qa has voted (sealed <hash>)", a commitment over the ballot and a
  salt; nobody sees another's ballot until all are in, then all are
  revealed together with their salts.
- *Bound to the version.* A ballot records the version it read; a new
  commit on the PR (seen at the tally or at execution) voids the vote, and
  GitHub merges only at the voted SHA (a moved head is a 409, and the vote
  is void).
- *Couldn't verify is a no.* A yes with no evidence, or without a
  `table_read` of the pinned version in that turn, is counted as couldn't
  verify. The critic (or skeptic) seat must name a specific risk, or its
  yes is counted as couldn't verify. A role whose turn fails or that does
  not vote is couldn't verify.
- *Any no blocks.* The chair gets one resolution round: `revote` (once,
  blind again, with a note on what to check), `escalate`, or `withdraw`
  (the dissent is right; fix the work and pin it again). It never votes
  and never overrules. A second blocked vote, an escalation, or a chair
  that cannot decide goes to the person.
- *The person's ask* is the front door's: kind `vote` in `front_asks`, one
  line ("Merge bake-ware/rook#180? security said no."), the explanation with
  each side summarised, the full ballots under Explain. So the phone's
  decision card, `rook_approvals` and the Decisions page carry it. Yes
  carries the vote out (gates again first); No drops it.

### 10.7 Execution

A passed vote (every gate, every required voter yes) is carried out by the
hub, never by a session or a role:

- **Merges** use the GitHub API with the hub's own credential: the vault
  secret named by `huddle.vote_github_secret` (sessions and roles never see
  it), at the voted SHA, method `huddle.vote_merge_method`. **Ready for QA**
  labels the PR `ready-for-qa` and comments with the vote.
- **Deploys and ships** run the deploy target's `deploy` call (`{sha}` is
  the voted commit) as the house agent through the front door's
  `run_action`, so the trust map rules on that call too. A vote alone never
  approves a hold that call meets; only the person's yes does.
- **Audit:** every execution is journaled as `huddle.vote.execute` with the
  vote, the artifact, the voted version, the trust ruling, each gate, each
  ballot's voter, seat, vote, version and commitment, and who approved (the
  person, or none for an automatic one). The digest gets a `did` line
  ("Merged Bake-Ware/rook#180 at abc1234 after a unanimous vote (...)"):
  do it, then tell.

**The trust ramp** (`huddle.vote_auto`, default `merge`, `qa`): those kinds
go on their own after a passed vote; `deploy` and `ship` still ask the
person with a one-line ask until Bake adds them. Sacred targets always ask.
Exceptions (like the overnight one) never remove a vote, never let a
sacred target skip the person, and never loosen the ramp.

### 10.8 Worked example (as built)

Bake, on the phone: "get the desktop app work going, and land it when it's
ready". The repository's trust entry is guarded for `master` only.

1. The voice Background hands it to the house agent (`home.ask`, source
   `voice`). The router opens a huddle with lead, qa, security and critic
   (`open_huddle`); the voice reply is "I've got the team on it:
   <three-word name>." Interruptions: 0.
2. The chair calls on the lead; the lead (autonomy `routine`) starts a
   coding session on a branch in the rook checkout (`start_session`; a
   branch is not `master`, so the trust map says go).
3. The session reports DONE with a pull request (5.5); the chair pins the
   PR at its head SHA and calls a merge vote. Interruptions: 0.
4. The hub runs the gates (head unchanged, merges cleanly, CI and tests
   green, no secrets in the diff). The lead's session wrote it, so the
   architect stands in; architect, qa, security and critic read the diff
   and checks at the pinned SHA and vote blind.
5. Unanimous yes: the hub merges at that SHA with its own credential and
   the digest says so. Bake hears it when he asks "what did you decide for
   me?". Interruptions: 0.

If someone votes no and the chair's one round does not settle it, Bake
gets one card: "Merge bake-ware/rook#180? security said no." (Yes /
No / Explain). A deploy of the merged commit is another vote and, with the
default ramp, one card after it passes.

| Case | Interruptions | Bake sees |
|---|---|---|
| ship-it target, merge or qa, gates pass (vote skipped) | 0 | a digest line |
| guarded or unlisted, unanimous merge or qa | 0 | a digest line |
| any no, not settled by the chair | 1 | a Yes / No / Explain card |
| deploy or ship, passed | 1 (until he loosens `huddle.vote_auto`) | a card |
| sacred target | 1, always | a card |
| a gate fails | 0 | the reason in the huddle; the work goes back |

### 10.9 Review findings (huddle husky-stairwell-hive, 2026-10-21)

| Finding [raised by] | Status |
|---|---|
| Tiers must be hub-enforced per cap and argument, deny by default [skeptic] | Addressed: the trust map is enforced in the approval layer (10.3) and the interim role tier per cap and argument, unknown caps an approval (4.5). Unlisted targets are `balanced` by decision (B1), not deny-all |
| Topic routing misroutes; needs a replay test [skeptic] | Open: there are no topic-grouped episodes; the router chooses `open_huddle` or `continue_huddle` per request, and there is no routing replay test |
| No budgets or loop limits [skeptic, security] | Partly: `huddle.max_role_turns`, `huddle.max_followups`, `role.max_tool_calls`, `role.sessions_per_role`, `role.session_max_minutes`, one resolution round per vote, roles never call `role.ask`, a 1:1 huddle refuses `continue_huddle`. No token or money budget per request |
| Don't remove watcher jobs before report-back is proven [skeptic] | Addressed: nothing here removes them; removal stays gated on a failure test (10.10) |
| Who checks the rollback was tested [skeptic] | Addressed for votes: the `rollback` gate runs the target's dry run before any deploy or ship vote, and again before execution |
| Policy audit to enforce is a blocker [security] | Partly: the trust map and interim tiers are enforced whatever the policy mode; the policy file itself is still in audit mode |
| The never-call list cannot be lowered [security] | Addressed: secrets are an approval for every role with no setting that lowers them (4.5); `trust.*` is never relayed; roles never call `huddle.vote` or `huddle.pin` |
| Intake auth; request text as data [security, skeptic] | Partly: no new intake surface; requests come through authenticated callers (the house agent never does for a caller what the caller could not do), a dashboard Yes needs a sign-in that is not a band secret, and the hub (not the request text) decides tiers and writes the "Done" line. The router still reads the text as a prompt |
| Cross-episode leaks: query-time checks [security] | Open: no per-huddle scoping of search |
| Secret-scan transcripts [security] | Partly: the vote's `secrets` gate scans diffs and action results are masked; transcripts are not scanned |
| The chair has no privilege [security] | Addressed in a changed form: the chair is the house agent, held by the trust map like any agent; it cannot approve holds, cards or votes, and never votes |
| Append-only audit tagged role, episode, worker, approver [security] | Partly: the journal (`home.front.action` with `approved_by`, `huddle.vote.execute` with the vote, versions, gates and ballots), `trust-history.jsonl`, `session-tokens.jsonl`. Tagged by huddle, not episode; the huddle's own vote rows are deleted with the huddle (`huddle.retention_days`), the journal rows stay |
| Worked voice example, interruptions per tier, a test per phase, smallest slice first [product] | Addressed: 10.8; tests `tests/test_trust.py`, `tests/test_front_door.py`, `tests/test_onboarding.py`, `tests/test_table_votes.py`; built in slices B1 to B4 |
| t_1f8866 dependency [pm] | Open: Claude Code as a provider is still unreliable at tool calls (task in progress). A role that fails to vote counts as couldn't verify, so it fails closed |
| Rewrite 5.1, 5.2, 5.4, 7 and 9 consistently [architect] | Addressed by not amending them (10.1) |
| Sessions could push master with the admin token (card c_839e89992532) [skeptic, security] | Addressed: `enforce_admins`, the push guard; minted tokens pending the GitHub App (10.3) |
| Tier bypass by chaining calls [skeptic] | Partly: every call is ruled on by itself, so a chain reaches nothing a single call could not; `session_send` only resumes the role's own session. No dedicated chaining test |
| Report-back failure and a forged DONE [skeptic, security] | Partly: the hub follows the session's own terminal stream (5.5), so no other sender can report, and a stall, a vanished terminal or overtime is reported. A session can still print DONE when it is not done, which is why a merge needs gates and a vote. Hub-down fallback open |
| H6c job-run failure test, H6b budget test [product] | Open: job runs do not report back; no budgets |
| Private path and hostname in the public repo (old 10.11) [security] | Addressed in this section; the hostname in older sections is open (10.10) |
| The H6a push test must be a real git push [skeptic] | Addressed: `tests/test_trust.py` pushes with real git through the real launcher; and a live probe |
| M1: merges ask first or do it, then tell | Settled by votes: merges and ready-for-QA go after a unanimous vote, deploys and ships ask (10.7) |
| M2: one request or grouped by topic | Open (10.10) |
| S1 three-word names | Addressed: huddles, asks, votes and artifacts have speakable names |
| S2 projects are tags on episodes | Superseded: projects are records (B3, 10.4) |
| S3 how an episode ends | Superseded: no episodes; a huddle is archived, and its rounds close when settled (5.1) |
| S4 what a role's context carries | Partly: the huddle transcript, its open cards and the table summary; no recap of other huddles |
| S5 escalation worker, role, chair, Bake | Addressed: a session reports to its role (5.5), the chair closes or settles (front-door.md 4), and what remains is one ask |
| S6 the chair is a single point of failure | Open (10.10) |
| S7 tiers for spending money and test hubs | Open (10.10) |
| Reviewers read the worktree, not the commit (proving run) | Addressed: `table_read` at the pinned version (10.5) |

### 10.10 Open items

Settled: proposal questions 1 (H6 into `master`), 2 and 3 (no episode
store; huddles and project records instead), 5 (session isolation, 10.3)
and 7 (M1, by votes). Still open:

1. **Report-back when the hub is down.** Who tells Bake? Watcher jobs stay
   until a failing session and a failing job run are shown reaching him.
2. **Topic grouping (M2).** Bake's decision 1 groups work by topic; the
   router decides per request with no replay test.
3. **Tiers for spending money and test hubs** (S7): unlisted is `balanced`
   today, so they need a trust entry or a rule.
4. **The chair is down** (S6). Any fallback router must keep the same trust
   map and holds, and cannot approve cards, holds or votes.
5. **Budgets** per request or huddle (tokens, money, tool calls) and a
   daily cap: not built.
6. **Transcript secret scan** and **query-time scoping** of huddle search:
   not built.
7. **Roles calling roles**: not built; the chair still routes every turn.
8. **Bake's GitHub steps**: create the GitHub App (minted session tokens)
   and the master ruleset (trust.md 5). If the ruleset restricts updates
   to repository admins, the hub's merge credential has to be an admin's
   or be on the bypass list.
9. **Deploy targets and the merge credential** are per hub
   (`huddle.vote_deploy_targets`, `huddle.vote_github_secret`): with no
   target a deploy or ship vote cannot be called, a target without a dry
   run stops at the rollback gate, and without the credential the hub
   cannot merge. On the live hub (2026-10-23) the merge credential is set
   (an owner's token from the vault); no deploy target is configured.
10. **Local repositories** (no GitHub remote) cannot be pinned; the table
    reads through the GitHub API only.
11. **Job runs** do not report back the way sessions do (5.5).
12. **Voice tasks** (proposal question 4): voice reaches the front door
    through the voice Background (`home.ask`); whether the two older voice
    tasks are closed by that is Bake's call.
13. **The hostname of the agent host** in older sections of this file and
    in section 11: scrub it from the public docs or not.
14. **Vote records and retention.** Deleting an archived huddle (retention)
    deletes its artifacts, votes and ballots; the `huddle.vote.execute`
    journal row (votes, versions, gates) stays.
15. **Authorship** comes from the hub's session records (and who pinned
    it): a pull request written outside a role's session excludes nobody.
16. **Claude Code tool calls** (t_1f8866): a role whose turn fails to call
    `vote` counts as couldn't verify, so votes fail closed but may block
    more than they should until that is fixed.

### 10.11 Decision record

- The house agent is the front door and the huddle one of its tools.
  Reason: one assistant for every client, one identity and one set of
  limits. Rejected: a separate intake and episode layer (a second front
  door and a second store for the same conversation).
- Tiers are the trust map, enforced by the hub on every holdable call.
  Reason: a mislabel by a role or a prompt cannot loosen it, and Bake reads
  and changes one record. Rejected: role-chosen tiers, prompt-only rules,
  a separate tiers table.
- Merges and deploys are votes carried out by the hub. Reason: review is
  real (every voter reads the same pinned version), facts are gates rather
  than opinions, and no session ever holds a credential that can merge.
  Rejected: sessions merging their own work; asking Bake for every merge.
- The ramp starts at merge and ready-for-QA; deploys and ships ask until
  Bake loosens `huddle.vote_auto`. Sacred always asks.

## 11. Rules for every PR

As in jobs.md 10: tests run with a scratch `HOME`, never the real one; no
secret values anywhere (`{{secret:name}}`); nothing installed on or changed
on the work laptops; the agent host is cachyrig for live testing; docs and
the settings/tools references regenerated in the same PR.
