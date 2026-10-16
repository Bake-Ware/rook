# Huddles: roles, a chair, and Claude Code as the house agent

Status: design. Nothing here is built yet. Related: [home-agent.md](home-agent.md)
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
| Huddle | A thread with Bake, the chair and one or more roles. |
| Action card | A concrete proposal from a huddle (task, job, session) that Bake approves. |

## 3. Claude Code as a house agent backend

### 3.1 Shape

The hub stays thin (it has under 1 GB of RAM). A Claude Code turn runs on the
agent host as a headless process and streams back to the hub:

```
hub (home plugin)  --band-->  agent host (worker plugin agenthost)
                                 claude -p <prompt>
                                   --model <model>
                                   --resume <session> | (new session)
                                   --output-format stream-json --verbose
                                   --mcp-config <generated, Rook only>
                                   --strict-mcp-config --restricted
                                   --allowedTools <from settings/role>
                                   --permission-mode <setting>
                                 cwd = <agenthost dir>/<agent>/<role>/
```

The worker plugin `agenthost` (`rook/worker/plugins/agenthost.py`) owns the
process. Caps:

| Cap | Risk | Does |
|---|---|---|
| `agenthost.status()` | read | Claude Code found? version, login state, running turns. Also in the heartbeat (`hb.agenthost`). |
| `agenthost.turn(agent, role, prompt, model, session?, system?, mcp_token_ref, allowed_tools, permission_mode, max_turns, timeout_s, turn_id)` | exec | Runs one turn; returns `{ok, session, text, events_cursor, usage, cost?, exit}`. Idempotent on `turn_id`. |
| `agenthost.events(turn_id, cursor, wait)` | read | Long-poll the turn's stream-json events for live display. |
| `agenthost.cancel(turn_id)` | exec | Stops a running turn (SIGINT, then SIGTERM after 5 s). |
| `agenthost.reset(agent, role)` | exec | Forgets the session id for that role on this host. |

Rules:

- **One turn per role at a time.** `--resume` on a session that is already
  running corrupts it, so turns for the same role queue (on the hub, not the
  worker). Different roles run in parallel. A per-host cap (`agenthost.max_parallel`, default 3) bounds it.
- **The working directory is rendered, not kept.** Before each turn the hub
  sends the role's charter, persona text and a memory digest; the worker writes
  them to `CLAUDE.md` in the role's directory. Nothing durable lives only on the
  agent host (3.3).
- **MCP is Rook only.** The generated MCP config points at the hub's MCP
  endpoint with `Authorization: Bearer ${ROOK_AGENT_TOKEN}`; the token is
  resolved from the vault by the worker at spawn time and passed in the
  environment, never written to disk. Claude Code's own user and project MCP
  servers are not loaded (`--strict-mcp-config`), and `--restricted` drops
  the code-running tools and the host's user/project settings files. (`--bare`
  is not used: it requires API-key auth and ignores the subscription login.)
- **Turn limits are enforced by the agent host.** The CLI has no turn cap
  flag in 2.1.x, so `agenthost` counts `tool_use` events in the stream and
  cancels the turn past `max_turns`, and kills it at `timeout_s`.
- **Built-in tools are off unless a role needs them.** Default
  `allowed_tools` is the Rook MCP tools only. A role that needs to read code
  (the lead developer) can be given `Read`, `Grep`, `Glob` in a working copy;
  `Bash`/`Edit`/`Write` are never on by default.
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

**Moving the house agent** is changing `home.claude.worker`. The next turn
goes to the new host. Role sessions on the old host are not carried over: the
first turn on the new host starts a fresh session seeded from the role's
memory and the huddle's recent messages (3.3), so nothing is lost but the
model's scratch context. **Test** on the page runs one turn on the chosen host
with the form's values and shows the reply, model, latency and usage.

Usage counts against the subscription that is logged in on the agent host.
The page shows the last 24 h of turns and their token usage per role so Bake
can see what huddles cost.

### 3.3 Memory that survives a move

Claude Code's own memory files live on the agent host, so they would be lost
when the house agent moves. Role memory therefore lives at the hub, in the
existing `memory` plugin, under scope `role:<id>` (the chair uses `shared`
and the person scopes as today):

- Before a turn the hub builds the role's memory digest (the same budgeted
  recall the home agent already uses) into the rendered `CLAUDE.md`.
- The role writes memory through a Rook MCP tool, `rook_memory`
  (`remember`/`forget`/`recall`), limited to its own scope plus read access to
  `shared`. The memory plugin's write rules (vault values become references,
  deny patterns) apply unchanged.
- Claude Code's auto-memory is turned off in the role directory so there is
  one source of truth.
- The Memory page gets a scope filter for roles, so Bake can read and edit
  what QA remembers.

### 3.4 Identity

The house agent needs its own Rook API token for MCP (`agent_id`
`home:<name>`), stored in the vault and named by `home.claude.agent_token`.
The permissions policy sees it as the home agent, so the policy and the Jobs
guardrail model apply to what the turns can call. Role turns add the role's
own tool scope (4.1) on top: the hub refuses MCP calls outside it, keyed by a
per-turn header (`X-Rook-Role`) that the token alone cannot raise.

## 4. Roles

### 4.1 A role

Stored by a new hub plugin `roles` (or inside `huddle`, 5), one document per
role, versioned with history like persona profiles:

| Field | Meaning |
|---|---|
| `id`, `name` | `qa`, "QA engineer". Identity `role:<id>`; people write `@qa`. |
| `charter` | One paragraph: what the role is for. |
| `concerns` | List: what it watches and is expected to raise unprompted. |
| `sources` | What it reads before answering (deck, jobs runs, journal, sessions, knowledge folders), as Rook caps with args. |
| `tools` | Cap scope, the Jobs format (`allow`/`deny` patterns). Default read-only. |
| `autonomy` | `propose` (default: only action cards) or `act` (may call `tools` directly). |
| `backend`, `model` | Blank: the house agent's. Otherwise per-role override (the lead developer on Sonnet). |
| `persona` | Optional persona profile for voice. |
| `enabled` | Off roles are not called on. |

### 4.2 Starting roster (editable, shipped as defaults on the page, not code)

- **Project manager (`pm`)**: concerns: the task deck, what is blocked and why,
  priorities and scope creep, what is on deck next, deadlines Bake mentioned.
  Sources: `task` deck, handoffs, recent huddle decisions. Tools: read, plus
  propose task create/assign/reprioritise.
- **QA engineer (`qa`)**: concerns: failing or flaky jobs, test results on
  recent PRs, regressions after deploys, workers out of date or offline,
  anything shipped without evidence. Sources: Jobs runs, journal errors, PR
  checks, worker status. Tools: read, plus propose jobs and tasks.
- **Lead developer (`lead`)**: concerns: open PRs and branches, what every
  Claude Code session is doing and whether it is stuck, technical decisions
  and debt. Sources: `sessions.list` across workers, `sessions.follow` tails,
  GitHub PRs. Tools: read, plus propose `sessions.send` (nudge a session) and
  `work.stream.open` (start one with a task). The lead fronts the sessions; it
  does not write code itself.

Anything else (ops, reviewer) is added from the page.

### 4.3 Reaching one role

A 1:1 is a huddle with one role. Jobs can call a role directly (6).

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
   memory loaded, each under a length budget (`huddle.reply_chars`, 1200).
   Roles may `propose` action cards.
4. The chair closes the round: a short synthesis and the action cards,
   de-duplicated. Rounds end there; roles do not talk among themselves
   unless Bake or the chair asks again (no loops; at most
   `huddle.max_role_turns` (6) role turns per round).
5. After a round each role may write to its own memory (3.3). The huddle's
   decisions are recorded on the huddle.

Replies stream to the page from `agenthost.events` while they run.

### 5.2 Action cards

`{id, kind, by, summary, call: {worker, cap, args}, state}` where `kind` is
one of `task.create`, `task.update`, `job.run`, `job.create`, `session.send`,
`session.start`, `note`, `ask_bake`. State: `proposed` → `approved` /
`dismissed` → `done` / `failed`. Approving runs the call under Bake's identity
(the approver), journaled with the card and huddle ids. A role with
`autonomy: act` runs calls within its tools directly and posts the card
already `done`. Guardrails (jobs.md 7) apply to both.

### 5.3 Caps and MCP

On worker `rook`: `huddle.list`, `huddle.read`, `huddle.start(title, roles)`,
`huddle.post(huddle, text, mention?)`, `huddle.card(huddle, card, action)`,
`huddle.archive`; `role.list/get/set/delete/history`. One MCP tool,
`rook_huddle`, action-style like `rook_jobs`. External agents (Claude Code,
Claude web) can read a huddle and post into it as guests, but are never
called on.

### 5.4 Settings

`huddle.reply_chars`, `huddle.max_role_turns`, `huddle.retention_days`
(default 90), `huddle.default_roles` (`["pm","qa","lead"]`).

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

## 9. Workstreams (each one PR into `beta-huddles`)

- **H1 Agent host and the Claude Code backend.** `agenthost` worker plugin
  (spawn, stream-json parsing, queueing per role, cancel, heartbeat), the
  `home.backend` switch and `home.claude.*` settings, fallback, Test, usage.
  The house agent's chat replies, `home.ask` and job steps work on either
  backend. Tests use a fake `claude` executable that emits recorded
  stream-json.
- **H2 Roles and role memory.** Role store and caps, `role:<id>` memory scope,
  `rook_memory` scoped tool, rendered `CLAUDE.md`, per-turn tool scope
  enforcement (`X-Rook-Role`). Ships the three default roles as page presets.
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
