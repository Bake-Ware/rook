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
The page talks to the MCP server's `/settings/account-api` (`view=home`,
actions `home_save`, `home_models`, `home_test`), behind the operator
account like the rest of the Settings area. Values take effect live: the
plugin reads its settings on every use.

### 1.2 Reachable at the hub

- **Chat.** While enabled and configured, the plugin keeps `agent:<name>`
  present in the hub's chat store (so the dashboard roster lists it and
  `@home` resolves) and watches the rooms it is in every 1.5 s. It replies when
  a message mentions it (routing metadata), says `@<name>` in a room it is in,
  or is sent in a two-person room with it. The reply is built from the room's
  last `context_messages` messages (its own as `assistant`, everyone else's as
  `user` prefixed with the sender), the persona and the system prompt
  addition, and is posted to the room (in a 3+ room it mentions the asker).
  A failed call posts `(I could not answer: …)` instead of going silent.
  The dashboard shows "home is thinking" until it posts.
  - Safety: messages from before it was first enabled are never answered (a
    per-room cursor in its data dir); messages older than 10 minutes are
    ignored; it never answers itself; at most 6 replies per room per minute;
    one reply in flight per room, two in total.
- **Other agents.** `home.ask(question, context)` on worker `rook`
  (`rook_call(worker="rook", cap="home.ask", args={...})`). On hubs where the
  home agent is enabled when the MCP server starts, the dedicated tool
  `rook_home_ask` is listed as well (every connect pays for `tools/list`, so
  hubs that don't use it don't carry it). `home.status` reports state without
  the key. `home.ask` is declared risk `write`: it spends model time, so band
  peers (unauthenticated) can't call it unless the operator raises
  `ROOK_HUB_BAND_MAX_RISK`.
- **Not blocking the hub.** The model call is plain `aiohttp` on the hub's
  event loop with a total timeout; chat replies run as background tasks. The
  only synchronous work is the chat store's small sqlite reads.

### 1.3 Identity and the journal

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

### 1.4 Tools

None by default. With `home.tools` on, the model gets one OpenAI function,
`knowledge_search(query)`, which calls `knowledge.read` (action `search`) in
process when the knowledge plugin is loaded; at most three tool rounds per
turn. Every tool call is journaled as described above. Nothing that writes or
reaches a worker is exposed yet.

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

Hermes keeps `MEMORY.md`/`USER.md` files. The home agent's memory is the
shared knowledge wiki plus the persona:

- **Facts and procedures**: knowledge pages (search before answering; write
  new pages with evidence links once write tools are allowed), so every other
  agent shares them.
- **Who the operator is and how to talk**: the persona plugin (`owner`,
  rules, a `home` family addendum).
- **Conversation continuity**: chat rooms are already persistent; long rooms
  get a rolling summary stored as a handoff on the room's thread id.
- **Import**: a one-off migration turns Hermes' memory files into
  knowledge pages (as was done for other memory imports: unverified, one
  source link per page, no secrets).

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

- Streaming replies (replies are posted whole).
- Providers other than OpenAI-compatible (an Anthropic adapter would sit
  beside `llm.ChatClient`).
- More than one home agent per hub, or per-band home agents (the settings are
  hub scope; a band override would need `overridable=("band",)` and a band
  argument on every path).
- Write tools, worker caps, schedules, memory writes.
