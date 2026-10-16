# Home agent: an LLM that lives at the hub

Status: groundwork. This is the hub plugin `home` (`rook/hub/plugins/home/`), the
Manage > Home agent page (`rook/web/home.js`), and this design. Over time the
home agent is meant to take over what a separately hosted general assistant
(today a NousResearch Hermes Agent on its own box) does for the operator:
conversation, tools, skills, memory, scheduled work and messaging. This
document says what exists now and the path from here to there.

## 1. What the groundwork does

### 1.1 Configuration

The operator sets up one home agent per hub on **Manage > Home agent**. The
page is a table (the agent, its model and endpoint, its state, recent
activity) with a detail panel holding the form. Every field is a hub-scope
setting in the settings store under `home.*`, so it also shows on the
Settings page, has history and attribution, and can be locked by an
environment variable (`ROOK_HOME_<NAME>`):

| Key | Meaning |
|---|---|
| `home.enabled` | Off by default. On: it answers in chat and to `home.ask`. |
| `home.name` | Slug, default `home`. Its identity is `agent:<name>`; people write `@<name>`. |
| `home.backend` | `openai` (the endpoint below), or `claude-code`, `codex`, `opencode` (that harness on an agent host; section 1.6). |
| `home.provider` | `openai` (any OpenAI-compatible `/v1` endpoint). The only provider so far. |
| `home.base_url` | The `/v1` base, e.g. `http://llm.example:1234/v1`. |
| `home.model` | A model id. The page's **List models** reads `GET /v1/models` with the values on the form. |
| `home.api_key` | Only a vault reference, `{{secret:<name>}}` (the setting's pattern rejects anything else). The page offers the vault's secret names; the key itself never leaves the vault except into the request header. |
| `home.persona` | A persona profile id. Blank: whatever the persona plugin assigns to family `home` (then band/default). |
| `home.system_prompt` | Appended to the persona and the built-in instructions. |
| `home.tools` | Read-only tools (knowledge search). Off by default. |
| `home.timeout_s`, `home.max_tokens`, `home.temperature`, `home.context_messages` | Limits. |

**Test** runs one round trip with the form's values (unsaved) and shows the
reply and latency. **Save** validates every changed key before writing any.
**List models** and **Test** never resolve a vault reference typed into the
form: they use the *saved* API key, and only against the *saved* base URL
(another URL gets no key; the reply carries a `note` saying to save first).
The vault read is logged with the operator as actor (via `home agent page`),
and the key is scrubbed from model ids and replies.
The page talks to the MCP server's `/settings/account-api` (`view=home`,
actions `home_save`, `home_models`, `home_test`), behind the operator
account like the rest of the Settings area. Values take effect live: the
plugin reads its settings on every use.

### 1.2 Reachable at the hub

- **Chat.** While enabled and configured, the plugin keeps `agent:<name>`
  present in the hub's chat store (presence refreshed every 30 s, so the
  dashboard roster lists it and `@home` resolves) and watches the rooms it is
  in every 1.5 s (one query for every room's newest seq; only rooms past its
  cursor are read). It replies when a message mentions it (routing metadata),
  says `@<name>` in a room it is in, or is sent by a person in a two-person
  room with it (agents always have to mention it). The reply is built from
  the room's last `context_messages` messages (its own as `assistant`,
  everyone else's as `user` prefixed with the sender), the persona and the
  system prompt addition, and is posted to the room (in a 3+ room it mentions
  the asker, unless the asker is an agent). A failed call posts the generic
  "The home agent is unavailable right now." (at most once per room per 10
  minutes); the detail goes to the log, the page's activity list and the
  journal, and `home.status` shows only a summary.
  The dashboard shows "home is thinking" until it posts.
  - Safety: messages from before it was first enabled are never answered (a
    per-room cursor in its data dir); messages older than 10 minutes are
    ignored, and a room more than 200 messages behind skips to the newest 200;
    it never answers itself; at most 6 replies per room per minute; one reply
    in flight per room, two in total. A trigger that arrives while a reply is
    running or the room is rate limited stays pending and is answered on a
    later pass (a rate-limited room gets one "busy" note per minute). After 3
    replies in a row to agents in a room it waits until a person speaks.
- **Other agents.** `home.ask(question, context)` on worker `rook`
  (`rook_call(worker="rook", cap="home.ask", args={...})`). On hubs where the
  home agent is enabled when the MCP server starts, the dedicated tool
  `rook_home_ask` is listed as well (every connect pays for `tools/list`, so
  hubs that don't use it don't carry it). `home.status` reports state without
  the key. `home.ask` is declared risk `write`: it spends model time, so band
  peers (unauthenticated) can't call it unless the operator raises
  `ROOK_HUB_BAND_MAX_RISK`. At most 10 asks per caller per minute and two
  model calls at once; errors are returned as a summary.
- **Not blocking the hub.** The model call is plain `aiohttp` on the hub's
  event loop with a total timeout; chat replies run as background tasks. The
  only synchronous work is the chat store's small sqlite reads.

### 1.3 Chat on the page

Manage > Home agent has a chat panel under the agent table (`homeChat` in
`rook/web/home.js`). It adds no endpoint: it uses the dashboard's own chat
API (`/api/chat/rooms`, `read`, `start`, `send`), the same calls and the same
admin login as the Chat view, so it chats as the dashboard's chat identity
`user:operator`.

- **One room, shared with Chat.** The conversation is the newest two-person
  room between `user:operator` and `agent:<name>`, which is also the room
  the Chat view opens when you click the agent in Presence. Visiting the page
  creates nothing; the first message creates the room (titled with the
  agent's name). Renaming the agent switches the panel to the new identity's
  room.
- **Sending.** Enter sends (Shift+Enter for a newline). The message mentions
  the agent explicitly, so it is still answered if someone later joins the
  room.
- **Working state.** "<name> is thinking · N s" from the send until a reply
  from the agent lands. Reopening the page while the last message is still
  the operator's (and recent) shows it again. Replies are posted whole, so
  the panel polls the room: every 1.5 s while waiting, every 4 s otherwise,
  and not at all while another view is open.
- **Errors.** Off or missing a base URL/model: the panel says so, disables
  the box and offers **Go to the form** (on a phone the form is below). A
  failed reply ends the wait with an error and the status summary
  (`last_error`): from the room's generic "unavailable" line, or, since that
  line is posted at most once per room per 10 minutes, from a failed `chat`
  row for `user:operator` in the agent's activity (checked every third poll
  while waiting). No reply after `2 × timeout_s + 30` seconds says so and
  points at Recent activity. Chat API failures (no chat store, signed out)
  are shown in the panel.
- Status lines the agent posts (busy, unavailable) are shown dimmed; the
  busy note does not end the wait.

Tests: `tests/browser_home_chat.py` (real plugin, fake model endpoint, real
chat store; desktop and phone width) and `test_the_home_page_chat_contract`
in `tests/test_home_agent.py`.

### 1.4 Identity and the journal

The home agent is its own principal, not a borrowed token:

- chat identity `agent:<name>`;
- journal rows written by the plugin (`home.reply` per chat answer, and each
  tool call such as `knowledge.read`) carry identity `agent:<name>`, auth kind
  `home`, `agent_id` `home:<name>` and actor `home.<name>.hub`;
- in-process caps it calls run with `caller_identity` and the bridge's
  attribution context set to the home agent (`HomeAgent._as_self`), so the
  knowledge plugin records the home agent as the actor, not whoever asked it;
- vault reads of its key are logged with actor `agent:<name>`, via
  `home agent`.

A `home.ask` from an MCP agent is journaled by `rook_call` under that agent
as usual; what the home agent then does is journaled under the home agent.

### 1.5 Tools

None by default. With `home.tools` on, the model gets one OpenAI function,
`knowledge_search(query)`, which calls `knowledge.read` (action `search`) in
process when the knowledge plugin is loaded; at most three tool rounds per
turn. The tool is offered only when whoever asked may call `knowledge.read`
on the hub under the policy (`deny` and `would_deny` both count as no; a chat
sender is evaluated with the policy's defaults for its kind, since chat
identities are not credentials). Every tool call is journaled as described
above. Nothing that writes or
reaches a worker is exposed to chat or `home.ask`.

**Job steps.** A job's `agent` step (docs/design/jobs.md 6) is the one place
the home agent gets a wider tool set (`rook/hub/plugins/home/job_agent.py`):
`rook_workers`, `rook_call`, `rook_tool`, `job_run`, `notify_bake`,
`ask_bake` and `finish`. Each call is carried out by the jobs plugin on
behalf of the job's identity (the step's tool scope, the job's guardrails
and the policy all apply, so it never exceeds what the job may do) and is
journaled as `agent:<name>/job:<id>/<principal>`; `home.job_step` records
each step's verdict. Settings: `home.job_steps` (on; off refuses job
steps) and `home.job_max_tool_calls` (20).

### 1.6 Agent harness backends (Claude Code, Codex, OpenCode)

With `home.backend` = `claude-code`, `codex` or `opencode`, every model call
(chat replies, `home.ask`, job steps, memory summaries, Test) runs as one
headless run of that harness on an *agent host*: a worker whose `agenthost`
plugin (`rook/worker/plugins/agenthost.py`) found it installed. The plugin is
on wherever a harness is installed (worker setting `agenthost.enabled`,
default on; `ROOK_AGENTHOST=0` turns it off); it only offers, the hub's
settings decide whether it is ever used. Design: [huddles.md](huddles.md)
section 3. Hub side: `rook/hub/plugins/home/agent_host.py`.

- **Host.** `home.agent_host`, then `home.agent_fallback_hosts`; the first
  online one offering `agenthost.turn` whose heartbeat
  (`hb.agenthost.harnesses`) lists the harness and does not report it signed
  out. None: `home.agent_on_unavailable` = `fallback_backend` (the
  OpenAI-compatible settings, when complete) or `fail`. The heartbeat
  re-reads each harness's version and sign-in every 5 minutes and after a
  failed turn.
- **Tools stay at the hub.** Each turn registers its tool set (the same one
  the OpenAI path would offer: knowledge and memory tools for chat and
  `home.ask`, the job tool set for job steps) on `/home/turn-mcp`
  (`rook/hub/plugins/home/turn_mcp.py`) under a random bearer token that is
  dropped when the turn ends (and expires `timeout + 60 s` after it began).
  The agent host passes the token to the harness in the environment
  (`ROOK_TURN_TOKEN`; each harness's MCP config refers to the variable), so
  it is never in argv or on disk, and the worker audit trail redacts the
  `*_token` args. Tool calls run in the hub exactly as on the OpenAI path: as
  the home agent, journaled, policy-checked; job tools through the jobs
  backend with the job's guardrails and budget.
- **Locked down, per harness.** The prompt goes in on stdin; the host counts
  tool calls in the stream and stops the run past `max_tool_calls`, and at
  the timeout. Each runs in an empty working dir with no session files.
  - Claude Code: `--tools ""`, `--setting-sources ""`, `--strict-mcp-config`,
    `--allowedTools mcp__home`, `--permission-mode dontAsk`,
    `--no-session-persistence`, `--disable-slash-commands`, a full
    `--system-prompt` (never Claude Code's own; `--system-prompt-file` keeps
    it, so it is not used).
  - Codex: `exec --json --ephemeral --ignore-user-config --ignore-rules -s
    read-only`, `approval_policy="never"`, web search off,
    `include_apply_patch_tool=false`, the shell, unified exec, browser,
    computer use, plugins, apps, image, multi-agent and similar features off
    (`code_mode_host` stays on: Codex calls MCP tools through it), the
    system prompt as `developer_instructions`, and the hub as the only MCP
    server with `default_tools_approval_mode="approve"` (the hub decides
    what a tool may do; nobody is there to approve).
  - OpenCode: `run --format json --pure --agent rookhome` with
    `XDG_CONFIG_HOME` pointed at a generated config: the user's model
    providers and default model copied in, never their MCP servers, plugins,
    agents or instructions; tools `{"*": false, "home_*": true}`;
    edit/bash/webfetch denied; the system prompt as the agent's prompt.
    OpenCode's free tier refuses custom agents.
- **The call.** `agenthost.turn(harness, ...)` (risk exec) is called by the
  hub as system code (`system:home.<name>`), not as whoever asked: the
  asker's permissions are applied to the tools, not to the transport.
  Idempotent on `turn_id`; a job step cancelled by its own timeout also sends
  `agenthost.cancel`. `agenthost.models(harness)` feeds the page's List
  models.
- **Stateless.** The conversation is flattened into one prompt each time
  (the hub already sends the room context and memory), so moving the agent
  to another host loses nothing.
- **Sign-in.** The host's own. For Claude Code, `home.claude_auth = token`
  passes the vault secret named by `home.claude_token` (a `claude
  setup-token` token) as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Settings.** `home.agent_timeout_s` (180; job steps may use their whole
  step timeout), `home.agent_callback_url` (blank: `core.mcp.public_url`),
  and per harness `home.claude_model` (default `haiku`) /
  `home.claude_effort`, `home.codex_model` / `home.codex_effort`,
  `home.opencode_model` (blank: the harness's default).

## 2. Path to replacing Hermes

What Hermes does today, and where each piece lands in Rook. The rule
throughout: the home agent acts through the same caps, journal and
permissions as any other agent; nothing gets a private side door.

### 2.1 Tools via band caps

Next step after read-only knowledge search:

1. **A cap allowlist, not a free-for-all.** A setting `home.caps` (list of
   cap prefixes, e.g. `knowledge.`, `task.`, `info.`, `screenshot.`) and the
   generic tool `rook_call(worker, cap, args)` exposed to the model, with the
   tool schema built from `caps.describe` (as the voice agent's own tool loop
   already does: discover schemas, one call at a time, never replay an
   uncertain write).
2. **Its own token-shaped principal for policy.** Give the home agent a
   principal in `rook.hub.authz` (kind `home`, role chosen by the operator) so
   the permissions policy (docs/design/permissions.md) can allow or deny its
   calls exactly like a token's. Calls go out through the band client with
   that identity and land in the journal with `auth = home`.
3. **Secrets by reference only.** It passes `{{secret:name}}` in args; the
   bridge substitutes and masks as for any agent. It never sees values.
4. **Confirmation for risky tiers.** `exec`/`admin`/`physical` caps post a
   question in the room and wait for a person, until the policy says
   otherwise.

### 2.2 Memory

Done: the `memory` hub plugin (docs/design/memory.md). The home agent reads
a budgeted memory section before every chat reply, `home.ask` and job step,
and keeps what it learned afterwards (people's profiles, facts, conversation
episodes, procedures from job steps). The shared knowledge wiki stays the
place for curated, evidenced pages that every agent shares; the persona
plugin still says how to talk. Importing Hermes' `MEMORY.md`/`USER.md` is
still a one-off migration (to memory with `memory.remember`, or to the wiki),
never with the secrets those files held.

### 2.3 Scheduled work

Hermes runs cron jobs. In Rook this becomes hub-side triggers: the hygiene
loop and the work system's task deck. Plan: a `home.schedule` setting (cron
expressions to prompts, or task ids) driven by the same trigger mechanism the
hygiene work introduces, each run posting into a dedicated room
(`home: scheduled`) and claiming/finishing tasks through `rook_task` like any
agent.

### 2.4 Messaging gateways

The Discord and Telegram plugins (`rook/hub/plugins/discord.py`,
`telegram.py`) already bridge chat rooms to a channel. Mentions of the home
agent arriving through a bridge are ordinary room messages, so the home agent
answers them with no gateway code of its own. Remaining work: map a
platform's native mention of the bot to the home agent's identity, and
optionally a per-integration "always address the home agent" flag for DMs.

### 2.5 Voice

The voice agent currently runs its own tool loop on a local model. Two
options, not exclusive:

- **Home agent as the brain**: the voice agent sends the transcript turn to
  `home.ask` (or a streaming variant) and speaks the answer, so voice, chat
  and messaging share one persona, memory and tool policy.
- **Shared model, separate loop**: keep the voice loop for latency and point
  both at the same OpenAI-compatible front door; the home agent handles
  escalations ("work on this and tell me").

A streaming `home.ask` (server-sent chunks over the MCP tool or a band
stream) is a prerequisite for the first.

### 2.6 Migration off Hermes

1. Run both. Configure the home agent against the same model front door;
   use it in dashboard chat only.
2. Import Hermes memory into knowledge (2.2); assign the persona to family
   `home`.
3. Enable read-only tools, then the cap allowlist (2.1) for the caps Hermes'
   skills actually use, one by one, watching the journal.
4. Move scheduled jobs (2.3) and point the messaging bridges at rooms the home
   agent is in (2.4).
5. Switch the dashboard's wake route for the Hermes worker off; leave Hermes
   running read-only for a while, then retire it and rotate the credentials
   it held.

## 3. Not in the groundwork

- Streaming replies (replies are posted whole; the page's chat panel polls the room).
- Providers other than OpenAI-compatible (an Anthropic adapter would sit
  beside `llm.ChatClient`).
- More than one home agent per hub, or per-band home agents (the settings are
  hub scope; a band override would need `overridable=("band",)` and a band
  argument on every path).
- Write tools, worker caps, schedules.
