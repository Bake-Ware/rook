# Persistent agents: role directories, always-on sessions, the hub queue and the voice conductor

Status: built 2026-10-23 for the house agent first (Bake's design, approved for this round).
Related: [front-door.md](front-door.md), [huddles.md](huddles.md) 4.4 (role homes),
[voice-front-background.md](voice-front-background.md), [trust.md](trust.md).

## 1. What Bake asked for

* Every role has its own directory, and Claude Code reads its configuration from it:
  `CLAUDE.md`, `.claude/settings.json` (model, thinking, permissions, env, hooks),
  `.mcp.json` (MCP servers), agents and skills.
* For the house agent, and any agent that uses voice:
  * in the role's main directory a **persistent, always-on Claude Code session on Opus**
    (the agent itself: persona, memory, its MCP tools);
  * in `voice/` a **persistent Claude Code session on Haiku with thinking off** (the
    voice; it inherits the persona through the parent `CLAUDE.md`).
* Sessions keep a stable identity (session id saved; resumed by id after restarts; never
  started fresh silently) and get maintenance: compaction at a threshold, with durable
  facts written to memory first.
* The hub owns a per-role inbox. Every input goes through it; nothing talks to a session
  directly. Priority: a live person (voice first), then huddle turns, then background
  items; queued background items are batched into one turn ("While you were busy: ...").
* The Haiku voice session is the **conductor**: it speaks first and routes second, through
  a small set of flow ops (steer, fork, queue, fold, status, pause/cancel, btw, mode).
  It conducts; it does not act. Only the back end says an action is done.
* (2026-10-23, later) The voice runs on **Haiku 5.5 at effort low with no tools**: with
  any tool, 5.5 (whose thinking cannot be turned off) thinks, calls the tool, then speaks
  (first word 1.6-2.7 s). It routes with a **routing line** at the end of its reply
  instead (3.4). One assistant with one name (`voice.assistant_name` and `voice.owner`, the
  voice plugin's: Sojourn, Bake's assistant, here): the
  voice never talks about the plumbing and never says it can't find something out.
* The house agent can change other roles' Claude Code configuration at the person's
  request ("give our reverse engineering technician the REA MCP"); granting tools is a
  guarded trust-map change: fine on the person's request, never self-granted.

## 2. Where things run

| Piece | Runs on | Why |
|---|---|---|
| Hub supervisor, queue, flow tools, role config | the hub (`rook-band-mcp`, home plugin) | the hub owns state, trust and identity |
| Session host (`python -m rook.agents`) | **cachyrig** (`systemd --user` unit `rook-agent-sessions`) | the Claude subscription login the house agent already uses (`home.agent_host = cachyrig`) |
| Role directories | cachyrig, `~/.rook-band-worker/plugins/agenthost/roles/<role>/` (the existing role homes) | one directory per role, shared with huddle role homes |
| Voice server (STT, TTS, echo guard, speaker ID) | kaiju (unchanged), new engine `house` | kaiju keeps the audio; only the "brain" moves |

The session host is its own process, not part of the band worker: it survives worker
restarts and OTA updates (the sessions keep running), it is deployed from a release
directory without touching the worker bundle feed, and it holds no band key.

## 3. The pieces

```
 phone ──audio──> kaiju voice server ──LAN ws /voice/house──> session host (cachyrig)
                     (STT, echo guard,                          ├─ voice lane: claude -p (Haiku, effort low)
                      speaker ID, TTS)  <──text deltas──────────│     └─ routing lines ──link──> hub (flow ops)
                                                                ├─ main lane: claude -p (Opus)
                                                                │     └─ MCP "rook" ──https──> hub /home/turn-mcp
 web, Telegram, MCP, huddles ──> hub home plugin                ├─ forks / btw: claude -p --resume --fork-session
          (front door, router)  ──agents.run──> inbox (queue) ──wss /home/agents-link──┘
```

### 3.1 Role directory (rendered by the hub, written by the session host)

```
<roles_dir>/<role>/
  CLAUDE.md                  persona + house rules (hub-rendered)
  .claude/settings.json      model, effortLevel, alwaysThinkingEnabled, permissions, env
  .mcp.json                  "rook" (hub tools, bearer ${ROOK_TURN_TOKEN}), granted servers
  memory/  workspace/        as for role homes (huddles.md 4.4)
  .rook/sessions.json        host-local: session ids per lane, resume counts (never moved)
  voice/
    CLAUDE.md                the conductor's rules (inherits ../CLAUDE.md)
    .claude/settings.json    Haiku 5.5, effort low, no tools (never a bare "Read" deny:
                             that also stops Claude Code loading CLAUDE.md)
    .mcp.json                no servers (the voice routes with a line, not a tool)
  forks/<fork id>/           a fork's scratch dir (or a git worktree for code)
```

Tokens are never written to disk: config files reference `${ROOK_TURN_TOKEN}`, which the
session host puts in each process's environment. (The voice had a flow token for its
flow tools until it went tool-free; it has none now.)

The session host also passes the files explicitly (`--settings`, `--mcp-config
--strict-mcp-config`, `--model`, `--effort`), because Claude Code ignores some project
settings (permissions.allow) from an untrusted folder in print mode. The files stay the
single source: the flags only point at them.

**Thinking off.** `claude-haiku-5-5` rejects disabled thinking (Claude Code turns a
disabled setting into adaptive thinking for it). `--effort low` plus
`MAX_THINKING_TOKENS=0` gives zero thinking tokens in practice (measured 0 of 15 turns);
the host records `thinking_tokens` per voice turn so a regression shows. For strictly no
thinking, set the voice model to `claude-haiku-4-5`.

### 3.2 Session host (`rook/agents/`, on cachyrig)

* `python -m rook.agents --config ~/.config/rook-agents/host.json`; the link token comes
  from the environment (`ROOK_AGENTS_LINK_TOKEN`, a 0600 env file), never argv.
* One **lane** per (role, `main` | `voice`): a long-lived
  `claude -p --input-format stream-json --output-format stream-json --verbose` process in
  the lane's directory. One turn at a time per lane.
* Session ids are saved in `.rook/sessions.json` and reported to the hub. A start resumes
  by id (`--resume <id>`). If the id cannot be resumed (its transcript is gone), the host
  starts fresh **and says so**: a `session` event with `reset_reason`, which the hub shows
  on the Home agent page and journals (`home.session_reset`).
* Crash: restart with backoff (2 s doubling to 60 s), resumed by id; an item that was
  running gets a failed `result` ("the session restarted").
* Idle sleep: a lane with `idle_sleep_s > 0` stops its process after that long idle and
  resumes it on the next input. The house agent's lanes default to never sleeping.
* Reload: after a config change the lane restarts at its next idle moment, resumed by id,
  so new `.mcp.json` / settings take effect without losing the conversation.
* Steer: a message written while a turn runs is folded in by Claude Code at the next tool
  boundary of the same turn (verified: one `result`). Interrupt: the stream-json control
  request `{"type":"control_request","request_id":…,"request":{"subtype":"interrupt"}}`.
* Compact: the user message `/compact <instructions>` (works in stream-json mode; emits a
  `compact_boundary`).
* Forks: `claude -p --resume <main id> --fork-session [--resume-session-at <uuid>]` in
  `forks/<id>/` (a scratch dir, or `git worktree add` for code), its own hub token, capped
  concurrency. `--resume-session-at` (the uuid of the last assistant message of the last
  completed turn) makes it a fork "from the last completed turn" while main is mid-turn.
* By-the-way: `/btw` answers "isn't available in this environment" in headless
  stream-json mode (verified with Claude Code 2.1.306). The equivalent: a throwaway fork
  `claude -p --resume <main id> --fork-session --no-session-persistence --tools ""` with
  no MCP servers, asked the question with a note that the running step was not
  interrupted. It answers from the main session's current transcript (including the
  in-progress turn), is never folded and leaves no session file. ~2-3 s.

### 3.3 The link: hub <-> session host

`wss://<hub>/home/agents-link`, `Authorization: Bearer <token>`. The hub checks
`sha256(token)` against `home.link_token_sha256` (a hash is not a secret, so it can be an
ordinary setting; the token lives only on the session host). One JSON object per message;
`"t"` is the type.

Host -> hub:

| t | fields |
|---|---|
| `hello` | `host`, `version`, `pid`, `servers` (names of MCP servers in the host's user config that may be granted), `lanes` {"role/lane": state} |
| `ev` | `role`, `lane`, `kind`, ... (below) |
| `reply` | `req`, `ok`, `result` / `error` (answers to hub requests that carry `req`) |

`ev` kinds: `state` {state: starting, idle, busy, sleeping, stopped, crashed; session_id;
context_tokens}, `session` {session_id, resumed: bool, reset_reason?}, `turn_start`
{item}, `tool` {item, name}, `text` {item, text} (main: whole assistant text blocks),
`result` {item, ok, text, error?, usage, context_tokens, cost_usd, duration_ms,
num_turns, thinking_tokens}, `compacted` {pre_tokens, post_tokens}, `fork` {fork, state:
started, done, failed, cancelled; session_id?; text?; error?; usage?; duration_ms?},
`btw` {req, ok, text, error?, ms}, `voice_turn` {turn, text, identity}, `voice_reply`
{turn, text, first_ms, total_ms, tools, thinking_tokens}.

Hub -> host (all may carry `req` for a `reply`):

| t | fields |
|---|---|
| `configure` | `roles` {role: {dir_files: {relpath: text}, lanes: {lane: LaneSpec}, voice_token_sha256}} — write files (only changed ones), start/reload lanes |
| `send` | `role`, `lane`, `item`, `text` — start a turn (lane idle) |
| `steer` | `role`, `lane`, `text` — add to the running turn (or start one if idle, item `steer`) |
| `interrupt` | `role`, `lane` |
| `compact` | `role`, `lane`, `instructions` |
| `reload` | `role`, `lane` |
| `stop` | `role` (both lanes) |
| `fork` | `fork`, `role`, `text`, `env`, `model`, `at` ("last_completed" or "now"), `repo` (optional local path: git worktree) |
| `fork_cancel` | `fork` |
| `btw` | `req`, `role`, `question`, `model` |
| `voice_say` | `role`, `text`, `turn` (optional: the voice turn it answers) |
| `status` | `req` |

LaneSpec: `{model, effort, tools (built-in tool names), env {NAME: value} (tokens),
partial (stream text deltas), session_id (the hub's record), idle_sleep_s,
thinking: false}`.

The hub is the authority on what runs; the host is the authority on what is running.
Events produced while the link is down are buffered (bounded) and sent on reconnect; the
hub de-duplicates results by item id.

### 3.4 The voice lane (kaiju <-> session host, LAN)

`ws://<host>:8791/voice/<role>`, `Authorization: Bearer <voice token>` (checked against
`voice_token_sha256` from `configure`). It goes straight over the LAN (1 ms) rather than
through the hub (Cloudflare: 110-240 ms per request), because the voice's first word is
the latency that matters. The hub stays in charge: it issues the configuration and the
token hash, every voice turn is reported to it (`voice_turn` / `voice_reply`), and
everything the voice routes to the back end goes through the hub's flow tools and queue.

Client -> host: `{"t":"user","turn","text","identity":{"owner","name"},"mode"?}`
(`mode`: `listen` or `brainstorm` when the connection is in one),
`{"t":"cancel","turn"}` (barge-in: interrupts the voice turn).
Host -> client: `{"t":"ready","role","session"}`, `{"t":"delta","turn","text"}`,
`{"t":"tool","turn","name"}`, `{"t":"end","turn","text","ok","first_ms","total_ms"}`,
`{"t":"push","turn"}` (an unsolicited turn follows: a back-end result being spoken),
`{"t":"error","turn","error"}`, `{"t":"mode","turn","mode","by":"voice"}` (the voice
switched its listening mode; the voice server switches the connection, only between
assistant, dictate, listen and brainstorm, and the phone shows it), `{"t":"config",
"engine","models"}` (when the hub changes them; `ready` carries them too), `{"t":"models",
...}` (the hub's answer to a control). Client -> host also `{"t":"control","op":
"models_get"|"models_set","args","identity"}` (the phone's Voice models screen through the
voice server): the host reports it as `voice_control`; the hub acts as that identity (only
the owner changes models) and answers with `voice_event` `models` to that client.

**Engine and models** (2026-10-23): `home.voice_engine` house|local is a hub setting the
voice server reads per connection from `ready`/`config`: local sends its turns down its own
Front/Background path on kaiju's models with no error event. `Sessions.set_models` (the
phone, the voice's `models` op after a spoken yes, or `home.voice_models_set`) writes
`home.voice_model`, `voice_engine`, `persistent_model` and the efforts through the settings
service, journals `home.voice_models_set` and reconfigures at once. Settings saved by the
dashboard process reach the MCP process within ~3 s (the settings plugin watches the
store), so none of this needs a restart.

The text a user turn goes in with: `[voice | <name or guest>[ | <mode>] | <day, time>]
<text>`, preceded by `[brief]\n<lines>` whenever the hub's brief changed (and at least
every 20 turns). The brief (`voice_context` from the hub, refreshed every 15 s, sent only
when it changed): what the agent is working on and what waits, open asks to the person,
recent decisions, how much of the band is online. The time comes from `home.timezone`.
Back-end results (`voice_say`) wait until the voice lane is idle and go in as
`[back end] <text>`; the reply is streamed to the client that asked (else the last active
one) as a `push` turn.

**Routing lines.** The voice has no tools; it ends a reply with
`<<flow {"op": "queue", "request": "..."}>>` (ops: queue/ask, steer, fork, fold, status,
btw, pause, resume, cancel, mode, quiet). The session host's filter
(`rook/agents/routing.py`) holds back anything that could start a marker until it is
clear, so no part of a routing line reaches the client however the deltas are split;
the reply text in `end` and `voice_reply` is clean, and the ops go to the hub in
`voice_reply.routing` (at most 3; an interrupted reply's ops are dropped). The hub runs
them (`Sessions.route`) with the flow code as the voice's speaker (the same rules as the
flow tools: the owner is a person, anyone else a guest), journals each as
`home.voice_route`, and says status and btw answers and failures back through the voice
(`voice_say`). A reply to a pushed result runs no ops at all: the result may quote a web
page, and nothing the voice merely reads out may queue, cancel or steer work as the person
(it also can't loop on status/btw). The brief is the owner's: the rules tell the voice
never to share it with a guest.
The voice server cuts anything from a `<<` on as a backstop.

### 3.5 The hub: inbox, supervisor, flow tools

**Inbox.** `agents.run()` is where every house-agent turn goes through. With
`home.persistent` on and the session host connected, a turn whose tools are all in the
main lane's tool set (the front door's tools, knowledge search, role config) becomes an
inbox **item** instead of a one-shot `agenthost.turn`:

| priority | via | examples |
|---|---|---|
| 0 live voice | `front-voice` | the person talking |
| 1 live person | `front-web`, `front-chat`, `front-telegram`, `front-ask`, `ask` | Home chat panel, Telegram, MCP `rook_home_ask` |
| 2 huddle | `front-huddle`, `huddle-*` | a huddle the house agent chairs |
| 3 background | `report`, `job`, `fold` | session report-back, job results, folded forks |

One item runs at a time on the main lane, highest priority first, oldest first. Items
nobody waits on (reports, job results) are batched: everything queued is prepended to
the next turn as "While you were busy: ..." (or sent as one background turn when the lane
is free). Items that carry their own tools outside the main set (huddle chair turns, job
steps, role turns) keep the one-shot path: they are separate agents, not the
conversation. If the session host is not connected, `agents.run()` falls back to the
one-shot path, so persistent mode degrades to today's behaviour instead of failing.

An item's text is the request with a header (`[item q12 | from Bake (person) | via
voice | live]`). The per-request context the front door builds (trust summary, playbook,
recalled memory, open asks) goes in only when it changed since the last item, so the
persistent context grows by the request, not by the same system prompt every turn.

Tool calls from the main lane arrive at `/home/turn-mcp` with the main lane's token and
run with the **current item's** handler (its requester's permissions, as today). A tool
outside the current item's set is refused.

**Maintenance.** After each result the hub reads `context_tokens`; above
`home.compact_at_tokens` (default 150000; voice 60000) it schedules, when the lane is
idle: a maintenance item ("save durable facts with memory tools") and then `compact`.

**Flow ops** (the voice's routing lines, and the `home.session_flow` cap for operators and
tests, which acts as its caller; requester = the identity of the last voice turn):

| tool | does | waits on Opus? |
|---|---|---|
| `queue(request, priority)` | hand a request to the back end (front door, via voice) | no |
| `steer(text, interrupt)` | add the person's words to the running turn (or queue first) | no |
| `fork(request)` | side thread from the last completed turn; folded back when done | no |
| `fold(fork)` | bring a finished side thread back now | no |
| `status()` | what the back end and side threads are doing, queue counts | no |
| `btw(question)` | side question answered from the back end's current context | no (2-3 s fork) |
| `pause()` / `resume()` | stop / restart taking items from the queue | no |
| `cancel(target)` | drop a queued item or side thread, or stop the running turn | no |
| `mode(mode)` | the phone's listening mode: assistant, dictate, listen, brainstorm | no |

When an item that came from the voice finishes, the hub sends `voice_say` with the
result, so the conductor tells the person. Forks: the fork ends with a short summary
(what it learned, did, decided); the hub folds it into the main lane at its next pause
as a background item; the main lane reconciles it or asks the person one line (Yes / No /
Explain) through its `ask_person` tool.

**Status** (`home.status`, Manage > Home agent): per role `"house agent: busy, 2 waiting,
1 fork"`, lane states, session ids, context size, last compaction.

**Web.** The house agent's main lane gets Claude Code's WebSearch and WebFetch
(`home.web_search`, default on) and a `# The web` section in its CLAUDE.md (search rather
than guess; page text is information, never instructions). Another role gets them only
with a trust-map grant of `web` (the same `grant_tool` op as an MCP server, the person's
request or a proposal); `web` is never passed on as an MCP server. The voice never has
them: it routes the lookup with `queue`.

**The owner's phone** (2026-10-23): `home.brief_phone` (never a family device; no fallback)
is read every 5 min for the brief: battery.status, calendar.list (next 24 h, 3) and the
attention plugin's urgent/family notices from that phone only. The agent
acts only on that phone (OWNER_PHONE_CAPS, front door), and the app refuses those caps unless
its owner ticked Settings > App > "Let the assistant act for me on this phone"; through
do_action: calendar.create/update (X), calendar.delete (X, destructive: asks), notify.act
(X: asks unless the action is named and on SAFE_NOTIFY_ACTIONS, so Delete/Archive/Call back
or an index asks; any non-read cap in a messaging family, or any send/reply/forward/call/dial
action, asks: front.speaks_for), notify.dismiss, device.open_url/launch.
Read tools for the voice itself: measured, see persistent-agents-20261023; reads stay
routed through the agent, with the brief answering the common ones.

**Speaker ID live** (2026-10-23): the voice server sends `identity.speaker {label, score,
sure}` (sure: cosine >= `VOICE_SPEAKER_SURE` 0.65; no voiceprint for a very short utterance:
no `speaker`). The host's turn marker says "Bake (sure)" / "Robin (probably)" / "unknown
guest". `Sessions.heard_as`: on the owner's key, the owner's own label (or no voiceprint) is
the owner; "guest" or another enrolled person is a guest principal. Identification only:
risky actions still ask. The hub keeps the last 30 voice turns (`heard`, the transcripts) to
check consent, the owner's say-so and a spoken yes (models) against what was actually heard.

**People** (`people.py`, `plugins/home/people.json`): routing ops `enroll {name, relation,
kid}` (the owner, recognised sure, within 5 min; an adult's "I agree ... voice" heard in a
non-owner voice; a kid only with the owner's say-so naming them), `forget_voice {name,
keep_record}`, `list_voices`. The hub writes the record (relation, consent with the words and
turn, devices whose worker name has the name, voiceprint state) and a memory fact, then
`voice_event enroll_start {name}`: the voice server reads the enrollment sentences with them
and reports `enrolled` / `enroll_failed` / `forgot` / `voices` as client controls. The app's
People list talks to the voice server directly (owner keys; voiceprints never leave it).

**listen_end {minutes}** -> `voice_event listen_end {quiet_minutes}` -> the voice server
emits `bye {mode: sleep, after_ms: until the sign-off is heard, quiet_ms}`; the app closes the
mic and ignores the wake word for quiet_ms. `home.voice_signoff` short|none.

**Scribe** (`scribe.py`): mode `scribe` (VOICE_SWITCHABLE, no model). The hub allows it only
when the owner was heard (a sure match) in the last 10 min, else switches to `listen` and
records nothing,
and re-checks on every line: once no line from the owner's label for 10 min it ends the
conversation and switches the phone to `listen` (review of #189); it
opens a conversation record and a knowledge page "Conversation <date time>". The voice server
sends each utterance as client control `scribe {text, speaker, at}` (enrolled names, else
per-conversation clustering "Speaker A/B", `SessionSpeakers`); the assistant's name or
`VOICE_SCRIBE_IDLE_S` (300) quiet ends it (`scribe {end}`), the named utterance then becomes a
normal turn. Every 15 lines and at the end the agent gets the new lines as a background item
with its usual tools (propose tasks, never send; takeaways and decisions with times on the
page). Transcripts pruned after `home.scribe_keep_days` (30); `home.scribe_ask` makes the
voice ask first.

**Sounds** (as Pianobar's DJ): the voice server offers `sound_tags` ([laugh] [chuckle]
[sigh] [gasp] [clear throat]) on about one turn in four (`VOICE_HOUSE_SOUND_EVERY`) when
the app's Expressive voice is on and the voice is a Chatterbox one; the host puts them in
the turn marker, the voice may use one. Unoffered or unknown bracketed words are cut before
TTS; the transcript shows tags as stage directions ("(laughs)").

### 3.6 Role config by the house agent (trust-gated)

The hub keeps each persistent role's config (model, effort, granted MCP servers, env) in
its plugin directory. The house agent's front-door tool `role_config` reads it and, at
the person's request, grants or revokes an MCP server for a role; the hub re-renders the
role's directory and reloads its session.

* A **grant** is a guarded trust-map change, recorded in the trust map's `grants` (op
  `grant_tool`, the person's words, who asked). It goes through only when the item being
  worked came from a person (signed-in dashboard user, the owner's voice, the owner on
  Telegram); requested by an agent, or on the house agent's own initiative, it becomes a
  trust proposal the person accepts on the Trust page. The house agent never grants
  itself anything without the person's request.
* A **revoke** tightens, so the house agent may do it at any time (logged, shown).
* Servers that can be granted are the session host's own (`hello.servers`: the MCP
  servers in its Claude Code user config, e.g. `rea` on cachyrig) plus the hub's
  `home.mcp_catalog`. Their definitions (commands, keys) stay on the host; the hub only
  names them.

## 4. Enabling it for another role

1. The role must run on the session host's machine (its home is there).
2. `role_config` (or Manage > Home agent > Sessions) with `persistent: true`, and
   `voice: true` if it should get a voice lane.
3. The role's huddle turns keep their one-shot path for now (they carry the chair's
   tools); its own inbox items (reports, asks addressed to it) use its main lane.

## 5. Fallbacks and rollback

* `home.persistent` off: every turn is one-shot again (today's behaviour). The session
  host can keep running; nothing is sent to it.
* Voice: `VOICE_ENGINE` unset on kaiju (or `classic`/`front_background`): the old
  voice-front / voice-back path. One line in `/etc/voice-agent/unified.env`.
* Session host: `systemctl --user stop rook-agent-sessions` (sessions stop; ids stay in
  `.rook/sessions.json`).
