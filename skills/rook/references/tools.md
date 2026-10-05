# Tool and capability reference

The tables are generated from the code by `tools/gen_skill_reference.py`; don't edit them by hand. Args: `name` is required, `name?` optional, `name=x` has a default. Descriptions are the first sentence of each docstring; the full text is in the MCP tool description or `caps.describe`.

## Notes that matter

- `rook_call` needs `worker` (name or id). Reply: compact `{ok, id, from, result|error}` (`id` = journal id, `from` = worker name) plus `_tips`/`_task`/`_unread_chat` only when new; `text=true` for plain text.
- `rook_workers` defaults to name/description/build/hb/age per worker; filter with `name`, `cap_prefix`, `online`, and pick columns with `fields` (`"all"` = full rows). `rook_caps` maps each cap to `"*"` (every worker), `{all_but:[…]}` or names; narrow with `prefix` or `worker`. Unfiltered, both still grow with the fleet.
- `rook_journal` listings omit reply bodies; `call_id=` returns the full stored reply.
- `rook_console_read`: pass the previous `last_seq` back as `since_seq`; lower `limit`.
- `rook_task`, `rook_project`, `rook_concept`, `rook_knowledge`: writes need a unique `request_id`. Search returns 5 excerpt rows by default (`data={"limit":N, "fields":[…]}`, `fields:"all"` for whole records); list returns 20 (`data` keys `parent,state,limit,offset,attention,worker,fields`). `get` returns the full record.
- `shell.exec` has **no output cap**; trim in the command. `file.read` defaults to 8 MiB; set `max_bytes`. `file.list` with `include_hidden=false` and a small `max_entries`.
- Android workers (the app, not in these tables) add `ui.text` (on-screen text, the cheapest way to "see"), `sms.*`, `calllog.list`, `contacts.search`, `notify.*`, `location.get`, `battery.status`, `device.*`.
- Custom caps appear as `cmd.<name>` on the worker that defined them.

## MCP tools

<!-- BEGIN GENERATED: mcp-tools -->
| Tool | Args | Does |
|---|---|---|
| `rook_call` | cap, args?, worker_id?, worker?, timeout?, hint?, text? | Run a cap on one worker. worker= (name or id) is required. |
| `rook_caps` | prefix?, worker? | Caps and their holders: "*" = every worker, {all_but:[…]}, or names. prefix filters (e.g. "shell."); worker=… |
| `rook_chat_delete` | room | Delete a room and its messages (participants only; final). |
| `rook_chat_read` | room, since_seq? | Messages after since_seq (0 = all), marked read. |
| `rook_chat_rooms` | — | Your chat rooms, newest first, with unread counts. |
| `rook_chat_send` | room, text, mention?, expects_reply? | Post to a room. mention (list/comma): in rooms of 3+ only mentioned participants are expected to reply; a men… |
| `rook_chat_start` | title, invite? | Start a chat room; invite = identities (list or comma string). |
| `rook_chat_wake` | room, worker, note?, timeout=20.0 | Make an agent on worker answer in room now (via hermes.chat or agent.wake); note is passed along. |
| `rook_concept` | action='search', band?, id?, query?, data?, request_id? | Concepts (why) above projects: search/list/get/create/update/link. create data {title, body, slug?}. |
| `rook_config_apply` | worker, settings, confirm_within=120.0 | Commit-confirmed config push: settings {name, announce_interval, log_level, hub, psk, env:{…}}. |
| `rook_config_get` | worker | A worker's config overrides and pending/confirm state. |
| `rook_console_close` | room, summary?, kill? | Freeze a console room. |
| `rook_console_list` | worker?, state?, limit=50 | Console rooms, newest first; filter by worker or state (live\|closing\|frozen). |
| `rook_console_open` | worker, task, cmd?, argv?, cwd?, env?, pty? | Run a slow, interactive or worth-keeping command as a console room, searchable after it exits. |
| `rook_console_read` | room, since_seq?, tail?, limit=300 | Console output after since_seq (page with last_seq), or tail=true for the last limit lines. state says live o… |
| `rook_console_search` | query, worker?, limit=20 | Full-text search of all console sessions; titles and summaries rank highest (search for the task, not the com… |
| `rook_console_signal` | room, sig='TERM' | Signal a live console's process group: TERM, KILL, INT (ctrl-C) or HUP. |
| `rook_console_write` | room, text, newline=true | Type into a live console's stdin, verbatim (no shell, no escaping). |
| `rook_handoff_get` | thread_id | A thread's current handoff plus history. |
| `rook_handoff_list` | limit=20, active_only=true | Recent handoff threads (latest per thread) with goal and freshness. |
| `rook_handoff_save` | goal?, thread_id?, state?, decisions?, next_steps?, artifacts?, supersedes?, transcript_ref?, task?, status='active' | Save a handoff (state, not a transcript) so another agent can continue without asking: goal, state, decisions… |
| `rook_journal` | call_id?, worker?, cap_prefix?, since_secs?, only_failures?, limit=30 | Recorded rook_call replies. call_id=<reply id> returns that call's full output (recover lost or timed-out out… |
| `rook_knowledge` | action='search', band?, id?, query?, data?, request_id? | Shared wiki: search\|get\|list\|context\|status\|create\|update\|link\|retract\|bands. search: 5 excerpts (data {limit… |
| `rook_presence` | — | Agents seen over the MCP recently (online within ~90s) and live band workers. |
| `rook_project` | action='list', band?, id?, query?, data?, request_id? | Projects (outcomes) under concepts: list/search/get/create/update/link. create data {title, body, parent: con… |
| `rook_secret` | action='list', name?, value?, description? | Vault: list (names only) \| get name (logged) \| set name value description \| delete name \| log name?. |
| `rook_task` | action='deck', band?, id?, query?, data?, request_id? | Tasks. deck (id=project narrows): in progress with claimants and latest handoff, blocked, paused, todo, recen… |
| `rook_whoami` | — | Your identity as this hub records it (agent_id, key_id, kind, identity). |
| `rook_workers` | name?, cap_prefix?, online?, fields? | Workers on the band. |
<!-- END GENERATED: mcp-tools -->

## Hub capabilities (worker `rook`)

The hub appears on the band as the reserved worker `rook`, serving the caps of hub-placed plugins: `rook_call(cap="hub.info", worker="rook")`. Caps marked `tool=True` also get their own MCP tool (listed above). Calls arriving over the band (not through the MCP) may only reach `read` caps. `fields?` means the cap takes a `fields` projection (`"*"` for every key).

<!-- BEGIN GENERATED: hub-caps -->
| Cap | Args | Risk | Does |
|---|---|---|---|
| `caps.describe` | prefix='' | read | Arg schema, docstring and declared risk/limit/fields for every hub cap. |
| `chat.delete` | room | write | Delete a room and all its messages (participants only; final). |
| `chat.presence` | — | read | Identities seen recently, newest first, with online flags. |
| `chat.read` | action='rooms', room?, since_seq=0, limit=200, mark=true | read | Read chat rooms: action=rooms (yours, newest first, unread counts) or read (room, since_seq). |
| `chat.write` | action, room?, text?, title?, invite?, mentions?, expects_reply=false | write | Write chat rooms: action=start (title, invite) or send (room, text, mentions). |
| `decide.confirm` | run_id, approve, note='' | exec | Approve (`approve=true`) or refuse the action a drive is waiting on. |
| `decide.drive` | goal, screen_worker, input_worker='', dry_run?, max_steps?, max_seconds?, texts?, keys?, screen_size='', wait=0.0 | exec | Drive a screen towards `goal`: screenshot -> one decision pass -> hid input. |
| `decide.health` | — | read | Is the configured decision model reachable and loaded? |
| `decide.info` | — | read | The configured model: name, adapter, calibration status and limits. |
| `decide.run` | state, questions, temperature? | read | Answer a batch of typed questions about `state` in one pass. |
| `decide.runs` | run_id='', steps=5, limit=20 | read | Recent drives, or one drive with its last `steps` journaled frames (answers, decision, gates, executed calls)… |
| `decide.stop` | run_id='' | write | Kill switch: stop one drive (`run_id`) or every live drive. |
| `discord.send` | text, chat? | write | Post a message to the configured Discord channel. |
| `discord.status` | — | read | Discord integration status: connected, channel and token configured (never the token), bridged rooms, counter… |
| `hub.info` | — | read | What the hub runs: version, core API, roles, facts and plugins. |
| `hub.plugins` | limit=50, fields? | read | Full manifests of the hub's loaded plugins (placement, settings schema, guidance slots, source). |
| `knowledge.read` | action='search', band?, id?, query='', data? | read | Read the shared wiki: search\|get\|list\|context\|status\|bands\|deck. |
| `knowledge.write` | action, band?, id?, data?, request_id? | write | Write the shared wiki: create\|update\|link\|retract (needs request_id). |
| `notify.channels` | — | read | Which notification channels are running on this hub. |
| `notify.send` | text, channel='all' | write | Send a notification to the chat integrations. |
| `persona.assign` | scope, profile='', target='', note='' | admin | Assign a profile at a scope: `default` (everyone), `band` (band id), `family` (claude-code, codex, hermes, vo… |
| `persona.delete` | id, note='' | admin | Delete an unassigned profile (its history stays). |
| `persona.get` | id='', family='', user='', band='', harness='' | read | A persona profile with its rendered `text`. |
| `persona.history` | id='', scope='', target='', limit=20 | read | Attributed changes, newest first. |
| `persona.list` | — | read | Every profile (id, name, rev) and every scoped assignment. |
| `persona.render` | harness='', profile='', user='', band='' | read | Just the rendered persona for a harness: `{text, profile, rev, sha}`. |
| `persona.set` | profile, note='', dry_run=false, expect_rev? | admin | Create or replace a profile (band owners and operator tokens). |
| `policy.explain` | principal, cap, worker?, role? | read | Would this principal be allowed to call `cap` on `worker`? |
| `policy.get` | — | read | The current policy document with its revision, source, mode, lint and last load error (if the file on disk is… |
| `policy.set` | policy, note='' | admin | Replace the policy document (band owners and operator tokens only). |
| `policy.status` | — | read | Hub permission status: policy mode/revision, whether calls carry tickets (root key present), the op-key id an… |
| `serves.clear` | worker | write | Remove a worker's hosting entry. |
| `serves.list` | worker='' | read | What workers host: `{worker: {sites, services, updated, by}}`. |
| `serves.set` | worker, sites?, services?, by='' | write | Write what a worker hosts. |
| `settings.apply_worker` | worker, confirm_within=120.0 | admin | Push a worker's stored settings to it (commit-confirmed restart). |
| `settings.describe` | prefix='', limit=100, fields? | read | The settings schema: key, type, scope, default, env names, apply mode. |
| `settings.fetch` | namespace | read | A service's own settings, secrets included, for its scoped token. |
| `settings.get` | key='', prefix='', scope='hub', target='', limit=100, fields? | read | Effective value of a setting, where it came from and what it hides. |
| `settings.history` | key='', scope='', target='', limit=20 | read | Attributed changes, newest first (secrets as fingerprints). |
| `settings.report` | namespace, env? | write | A service reports which of its settings its environment sets. |
| `settings.reset` | key, scope='', target='', note='' | admin | Remove a stored value so the key inherits again (in history). |
| `settings.set` | key, value, scope='', target='', note='', dry_run=false | admin | Store a setting (validated, attributed, in history). |
| `settings.worker_secret` | worker_id, names | read | Vault secrets a stored setting assigns to this worker (fetch at use). |
| `task.read` | action='deck', kind='task', band?, id?, query='', data? | read | Read tasks/projects/concepts: deck\|search\|list\|get\|context\|status\|hygiene. |
| `task.write` | action, kind='task', band?, id?, data?, request_id? | write | Write tasks/projects/concepts: create\|update\|link\|retract\|claim\|release\|note\|batch. |
| `telegram.send` | text, chat? | write | Post a message to the configured Telegram chat. |
| `telegram.status` | — | read | Telegram integration status: connected, chat and token configured (never the token), bridged rooms, counters… |

### chat rooms
Persistent rooms shared by agents, people and workers. MCP: `rook_chat_*`. Over the band, on worker `rook`: `chat.read` (action rooms|read), `chat.write` (action start|send), `chat.delete`, `chat.presence`. Band callers are recorded as `band:<identity>`; posting over the band needs `ROOK_HUB_BAND_MAX_RISK=write`.

### decide
One-pass decision model (worker `rook`). `decide.run(state, questions)` answers a batch of `{id, type: choice|score|noul, question, options|levels}` in one pass; probabilities are uncalibrated. `decide.drive(goal, screen_worker, input_worker?, dry_run?, texts?, keys?)` runs screenshot -> decide -> `hid.*` with confirmation gates and returns a `run_id`; dry-run is the default. Watch it with `decide.runs(run_id=...)`, approve with `decide.confirm(run_id, approve)`, kill with `decide.stop()`.

### discord
When the Discord integration is on: `discord.send` (text) posts to the configured channel; `discord.status` shows whether it is connected. Prefer `notify.send` to reach every configured channel.

### hub
`rook_call(cap="hub.info", worker="rook")` returns the hub's version, core API, roles, facts and plugins. `hub.plugins` lists full plugin manifests; pass `fields="*"` for every key.

### knowledge
The shared wiki. Use the `rook_knowledge` tool: `search` before starting (5 excerpts; `data.limit`/`data.fields` for more), `get` a page by id or slug, `create` a page with a unique `request_id`. Over the band the same actions are `knowledge.read` (search/get/list/context/status/bands) and `knowledge.write` (create/update/link/retract) on worker `rook`; band callers reach only the read cap by default. Semantic search needs an embedding service (setting `embedder`).

### notify
`rook_call(worker="rook", cap="notify.send", args={"text": "..."})` posts a notification to every configured chat integration (Telegram, Discord); `channel="telegram"` picks one.

### persona
One persona for every harness. `rook_call(cap="persona.get", worker="rook", args={"family": "claude-code"})` returns the persona that applies to you (user > family > band > default) with its rendered `text`. To install it in a harness file on a machine: `rook_call(cap="persona.apply", worker="<name>", args={"harness": "claude-code", "dry_run": true})` (then without `dry_run`; `remove=true` takes it out). It only edits between its own markers. `persona.set` / `persona.assign` are admin: ask the user first.

### policy
`rook_call(cap="policy.explain", worker="rook", args={"principal": "role:agent", "cap": "shell.exec", "worker": "<name>"})` shows whether a call would be allowed and which rule decides it. `policy.get` returns the document; changing it (`policy.set`) is for band owners and operator tokens.

### serves
What each worker hosts: `serves` on a `rook_workers` row is `{sites: [{name, url}], services: [{name, url}]}`, written by hand. `rook_call(worker="rook", cap="serves.list")` returns all of it; `serves.set(worker, sites?, services?)` replaces the lists it is given; `serves.clear(worker)` removes the entry.

### settings
Hub, band, worker and user settings with their source (default / hub / band / worker / user / file / env) on worker `rook`: `settings.get(key=…)` or `settings.get(prefix="core.", scope="hub")`, `settings.history`, `settings.describe`. Writes (`settings.set`, `settings.reset`, `settings.apply_worker`) are admin actions: ask the user first. A key set by an environment variable is locked; the reply says which.

### tasks
Tasks, projects and concepts. Use `rook_task(action="deck")` to see what is on; `claim` a task before working (your calls, consoles and handoffs then link to it); finish with `update` state done + `attrs.outcome` + an evidence `link`, or leave a handoff. Over the band: `task.read` (deck/search/list/get) and `task.write` (create/update/link/retract/claim/release) on worker `rook`, with `kind=task|project|concept`. Hygiene nudges ride replies as `_hygiene` and show on the deck; `rook_task(action="hygiene")` lists open ones (`data {mine: true}`). A commit message with `rook: <task id or slug>` links the commit to that task as evidence.

### telegram
When the Telegram integration is on: `telegram.send` (text) posts to the configured chat; `telegram.status` shows whether it is connected. Prefer `notify.send` to reach every configured channel.
<!-- END GENERATED: hub-caps -->

## Worker capabilities

Grouped by plugin module. A plugin only loads where its backend is present (display, camera, service), so not every worker has every cap.

<!-- BEGIN GENERATED: worker-caps -->
**core**

| Cap | Args | Does |
|---|---|---|
| `caps.describe` | prefix='' | Arg schema + docstring for every capability on this worker (for the UI). |
| `customcap.add` | name, command, args?, description='', timeout=30.0 | Define (or replace) a custom cap `cmd.<name>` that runs `command`. |
| `customcap.list` | — | List defined custom command-caps (name, command template, args). |
| `customcap.remove` | name | Delete a custom cap and unregister it. |
| `worker.description_get` | — | Read this worker's persistent, human-written role description. |
| `worker.description_set` | description | Save a short role description (max 280 characters); empty text clears it. |
| `worker.plugin.disable` | module | Unload a plugin now and keep it unloaded across restarts. |
| `worker.plugin.enable` | module | Load a plugin now and keep it loaded across restarts. |
| `worker.plugin.list` | — | List every plugin module and whether it's currently loaded, with the caps each loaded plugin provides. |

**battery**

| Cap | Args | Does |
|---|---|---|
| `battery.status` | — | Current battery: `{percent, charging, plugged, status, ...}`. |

**camera**

| Cap | Args | Does |
|---|---|---|
| `camera.list` | — | List the cameras on this worker (use one as the `camera` arg to snap). |
| `camera.snap` | camera=0, quality=85, resolution='1280x720' | Capture a still JPEG from a camera. |

**cec**

| Cap | Args | Does |
|---|---|---|
| `cec.ping` | — |  |
| `cec.raw` | cmd |  |
| `cec.send` | addr, opcode, operands? |  |

**chat**

| Cap | Args | Does |
|---|---|---|
| `chat.open` | room, me?, title='rook chat' | Pop a chat window on this worker for `room` and return whether one was spawned. |
| `chat.poll` | room, since=0.0 | Return messages in `room` newer than `since` (unix ts). |
| `chat.rooms` | — | List chat rooms on this worker with a last-message preview — the band CLI aggregates these across workers int… |
| `chat.send` | room, text, sender='operator' | Append a message to the room transcript (the worker's window shows it). |

**claude_history**

| Cap | Args | Does |
|---|---|---|
| `claude-history.analyze` | pattern='tool_usage', path?, machine?, limit=20 | Extract a knowledge pattern across all sessions. |
| `claude-history.export` | session_id, format='markdown', path?, machine? | Export a session as `markdown`, `json`, or `html`. |
| `claude-history.follow` | session_id, offset=0, version='' | Check the selected log and return only its changed tail, in stable pages. |
| `claude-history.pull` | machine?, path?, limit=50, offset=0 | List session metadata under `path` (default `~/.claude/projects`). |
| `claude-history.read` | session_id, path?, machine?, max_messages=1000, offset=0 | Read a session transcript. |
| `claude-history.read_page` | session_id, path?, offset=0, content_offset=0, max_chars=6000, snapshot? | Read a bounded transcript page, including partial large messages. |
| `claude-history.read_snapshot` | session_id, path?, offset=0, content_offset=0, snapshot='' | Read a bounded page of a stable, worker-owned conversation snapshot. |
| `claude-history.resume` | session_id, path?, name?, cwd?, remote_control=true, machine? | Relaunch a stored Claude Code session on this machine. |
| `claude-history.resumed` | — | Sessions this worker relaunched and whether they're still up. |
| `claude-history.search` | query, path?, machine?, limit=20, ignore_case=true | Regex-search across all session messages. |
| `claude-history.send` | session_id, text, command_id | Send to this exact existing session; transcripts stay on this host. |
| `claude-history.transcript` | session_id, offset=0, max_chars=6000, path? | A transcript page in the stable `rook.transcript/1` export format. |

**codex_history**

| Cap | Args | Does |
|---|---|---|
| `codex-history.analyze` | pattern='tool_usage', path?, machine?, limit=20 | Extract a knowledge pattern across all sessions. |
| `codex-history.export` | session_id, format='markdown', path?, machine? | Export a session as `markdown`, `json`, or `html`. |
| `codex-history.follow` | session_id, offset=0, version='' | Check the selected log and return only its changed tail, in stable pages. |
| `codex-history.pull` | machine?, path?, limit=50, offset=0 | List session metadata under `path` (default `~/.claude/projects`). |
| `codex-history.read` | session_id, path?, machine?, max_messages=1000, offset=0 | Read a session transcript. |
| `codex-history.read_page` | session_id, path?, offset=0, content_offset=0, max_chars=6000, snapshot? | Read a bounded transcript page, including partial large messages. |
| `codex-history.read_snapshot` | session_id, path?, offset=0, content_offset=0, snapshot='' | Read a bounded page of a stable, worker-owned conversation snapshot. |
| `codex-history.resume` | session_id, path?, name?, cwd?, machine? | Resume Codex interactively in a Rook PTY without sending a prompt. |
| `codex-history.resumed` | — | Sessions this worker relaunched and whether they're still up. |
| `codex-history.search` | query, path?, machine?, limit=20, ignore_case=true | Regex-search across all session messages. |
| `codex-history.send` | session_id, text, command_id | Send to this exact existing session; transcripts stay on this host. |
| `codex-history.transcript` | session_id, offset=0, max_chars=6000, path? | A transcript page in the stable `rook.transcript/1` export format. |

**config**

| Cap | Args | Does |
|---|---|---|
| `worker.config_apply` | settings, epoch, confirm_within=120.0, restart=true | Apply config overrides and restart under them (commit-confirmed). |
| `worker.config_confirm` | epoch | Confirm a pending config so it commits (the auto-revert watchdog is cancelled on the next boot since nothing… |
| `worker.config_get` | — | Return this worker's active config overrides + pending/confirm state. |
| `worker.config_revert` | restart=true | Force a revert to the previous config and restart. |

**deluge**

| Cap | Args | Does |
|---|---|---|
| `deluge.add` | torrent | Add a torrent by magnet link, .torrent URL, or local path. |
| `deluge.files` | torrent_id | A torrent's save path + files — pull them over the band with file.read. |
| `deluge.list` | — | Current torrents: name, state, progress, size, ratio (+ raw output). |
| `deluge.pause` | torrent_id='*' | Pause a torrent by id, or all with `*`. |
| `deluge.remove` | torrent_id, data=false | Remove a torrent. |
| `deluge.resume` | torrent_id='*' | Resume a torrent by id, or all with `*`. |
| `deluge.status` | — | Whether the daemon is running, plus a session summary if reachable. |

**dongle**

| Cap | Args | Does |
|---|---|---|
| `dongle.consumer` | usage | Tap a USB consumer usage, e.g. 0xE9 volume up or 0xCD play/pause. |
| `dongle.display` | text |  |
| `dongle.display_probe` | — |  |
| `dongle.keyboard` | keys, mods=0 | Tap up to six USB HID usage codes with modifier bitmask (US layout). |
| `dongle.mouse` | x=0, y=0, buttons=0, wheel=0, pan=0 | Relative move (-127..127) and/or button tap (bitmask 0..31). |
| `dongle.release` | — |  |
| `dongle.status` | — |  |

**enrollment**

| Cap | Args | Does |
|---|---|---|
| `worker.enrollment_finish` | grant |  |
| `worker.enrollment_move_prepare` | server | Advertise support for authenticated cross-band configuration changes. |
| `worker.enrollment_prepare` | server |  |
| `worker.enrollment_prove` | migration_id, epoch, nonce |  |
| `worker.enrollment_status` | — |  |

**file_ops**

| Cap | Args | Does |
|---|---|---|
| `file.exists` | path | Check if a path exists; reports the kind if so. |
| `file.list` | path, recursive=false, include_hidden=true, max_entries=5000 | List a directory. |
| `file.read` | path, encoding='utf-8', max_bytes=8MiB | Read a file. |
| `file.search` | pattern, path='.', recursive=true, max_results=100, ignore_case=false, glob='*' | Grep `pattern` (regex) across files under `path`. |
| `file.write` | path, content, encoding='utf-8', append=false, create_parents=false | Write `content` to `path`. |

**hermes**

| Cap | Args | Does |
|---|---|---|
| `hermes.chat` | message, session_id?, timeout=120.0 | Send a conversational message to Hermes and return its response. |
| `hermes.mcp.list` | — | List configured MCP servers. |
| `hermes.memory.read` | entry='MEMORY.md' | Read a built-in memory file (MEMORY.md or USER.md). |
| `hermes.memory.status` | — | Report persistent-memory provider status. |
| `hermes.run` | prompt, model?, provider?, timeout=120.0 | Run a one-shot prompt through Hermes (clean, final-answer-only). |
| `hermes.sessions.list` | — | List recent conversation sessions. |
| `hermes.sessions.read` | session_id='', limit=50 | Read a session transcript. |
| `hermes.skills.list` | — | List installed skills. |
| `hermes.skills.search` | query | Search skill registries. |
| `hermes.status` | — | Report Hermes install status: config, binary, version. |

**hid**

| Cap | Args | Does |
|---|---|---|
| `hid.backend` | — | Report which input backend was selected. |
| `hid.key_combo` | key, modifiers? | Press a key with optional modifier list (e.g. `["ctrl","shift"]`). |
| `hid.mouse.click` | button=1, x?, y? | Click `button` (1=left, 2=middle, 3=right). |
| `hid.mouse.drag` | start_x, start_y, end_x, end_y, button=1, duration_ms=200 | Press at (start_x, start_y), move to (end_x, end_y), release. |
| `hid.mouse.move` | x, y | Move pointer to absolute pixel (x, y) — top-left origin. |
| `hid.type` | text | Type `text` as if on a real keyboard. |

**info**

| Cap | Args | Does |
|---|---|---|
| `info.host` | — |  |
| `info.ping` | — |  |
| `info.uptime` | — |  |

**log**

| Cap | Args | Does |
|---|---|---|
| `log.audit` | limit=50, cap_prefix?, identity?, since? | Return recent audit entries for this worker (newest last). |
| `log.tail` | limit=20 | The last `limit` audit entries, unfiltered (newest last). |

**memory**

| Cap | Args | Does |
|---|---|---|
| `memory.entities` | name? | List current-state entity notes (`entities/*.md`), or read one by name. |
| `memory.get` | path | Read a markdown note from the vault (read-open across the band). |
| `memory.note` | claim, subjects?, kind='fact', supersedes?, thread_id?, provenance? | Drop an atomic post-it — one dated claim about some entities. |
| `memory.put` | path, text, append=false | Write (or append to) a markdown note. |
| `memory.search` | query, limit=20, include_notes=true | Search the vault and return a *pile* of relevant post-its in temporal order (oldest first) plus matching note… |

**msg**

| Cap | Args | Does |
|---|---|---|
| `msg.clear` | — | Empty this worker's inbox. |
| `msg.read` | limit=20 | Return the most recent messages from this worker's inbox. |
| `msg.send` | text, sender='operator' | Deliver a text message to this worker: store it in the inbox and try a desktop notification. |

**persona**

| Cap | Args | Does |
|---|---|---|
| `persona.apply` | harness, path='', content?, profile='', remove=false, dry_run=false | Write (or with `remove=true` take out) the persona's managed block in a harness file: claude-code -> ~/.claud… |
| `persona.status` | harness='', path='' | Whether each harness file holds a persona block, with its profile, rev and hash. |

**pikvm**

| Cap | Args | Does |
|---|---|---|
| `pikvm.api.get` | path, query? | GET any /api/* endpoint on the PiKVM. |
| `pikvm.api.post` | path, query?, body?, body_b64?, body_type='application/json' | POST to any /api/* endpoint. |
| `pikvm.key` | key, mods? | Press+release a key with optional modifier list (each is a key name). |
| `pikvm.mouse.click` | button='left' | Press and release a mouse button. button = left\|right\|middle. |
| `pikvm.mouse.move` | x, y | Absolute mouse position. |
| `pikvm.power` | action | Trigger ATX power. action in {on, off, off_hard, reset, reset_hard}. |
| `pikvm.power.status` | — | Read ATX state (powered, online, etc). |
| `pikvm.snap` | preview=true, quality? | Grab a still JPEG from the PiKVM streamer. |
| `pikvm.type` | text, slow=false | Type a string. |

**proc**

| Cap | Args | Does |
|---|---|---|
| `proc.close` | handle | Kill the process if it's still running and drop the session. |
| `proc.list` | — | Every session on this worker — live and recently finished. |
| `proc.read` | handle, cursor=0, max_bytes=8192 | Read output from `cursor` onward (0 = from the beginning). |
| `proc.signal` | handle, sig='TERM' | Send a signal to the process group: TERM (polite), KILL (hard), INT (ctrl-C), HUP. |
| `proc.start` | cmd?, argv?, label?, cwd?, env?, pty=false, buffer_bytes=262144 | Start a process and return a handle immediately — it keeps running after this call returns. |
| `proc.write` | handle, data, newline=true | Write to the process's stdin. |

**screenshot**

| Cap | Args | Does |
|---|---|---|
| `screenshot.capture` | quality=85 | Capture the full primary display as a JPEG. |
| `screenshot.capture_preview` | — | Low-quality full-screen grab for quick visual checks. |
| `screenshot.capture_region` | x, y, w, h, quality=85 | Capture a rectangular region of the screen. |

**selfupdate**

| Cap | Args | Does |
|---|---|---|
| `worker.apply` | manifest | Apply a signed update manifest pushed in-band by the controller. |
| `worker.check` | force=false | Check the signed manifest now and update if a newer build is published (bypasses the poll interval). |
| `worker.deauth` | payload | Remove/ban this worker from the band — but ONLY on an ed25519-signed order from the controller. |
| `worker.hold` | enable=true | Pin this node so it won't auto-update (survives restarts). |
| `worker.ota_begin` | manifest, drop_id=0 | Arm this worker to receive a bundle pushed **in band** over telesthete Drop (§8), for the given `drop_id`. |
| `worker.reconfigure` | hub?, psk?, name?, restart=true | Repoint this worker at a new band (hub/psk) and/or rename it, then restart. |
| `worker.restart` | — | Restart this worker with its current config, via the service manager (or re-exec). |
| `worker.status` | — |  |
| `worker.update` | url?, hub?, psk?, name?, manifest? | Install a worker bundle, optionally repoint at a new band, then restart. |

**shell**

| Cap | Args | Does |
|---|---|---|
| `shell.env.get` | name, default? | One environment variable (values pushed as secrets read `***`). |
| `shell.env.list` | prefix='' | The environment (values pushed as secrets or credentials read `***`). |
| `shell.exec` | cmd?, argv?, stdin?, timeout=30.0, cwd?, env? | Run a command. |
| `shell.which` | name |  |

**terminals**

| Cap | Args | Does |
|---|---|---|
| `work.export` | agent, session_id, offset=0, max_chars=6000 | A page of a historical Claude/Codex transcript in the stable `rook.transcript/1` format: `{format, session, m… |
| `work.sessions` | limit=20, offset=0, history=true, query='' | One catalog of this host's work: live terminals plus Claude/Codex history, newest first. |
| `work.stream.close` | id | Stop the terminal's process (SIGHUP, then SIGKILL) and drop it. |
| `work.stream.list` | — | Live and recently finished terminals on this worker. |
| `work.stream.open` | harness='shell', cwd='', title='', model='', resume='', persona='', mcp_url='', mcp_token='', session='', cols=120, rows=32, buffer_bytes=262144 | Start a harness (shell\|claude\|codex\|hermes) under a PTY and return its terminal `id` immediately. |
| `work.stream.read` | id, cursor=0, max_bytes=16384, wait=0, accept='tbz' | Output from byte `cursor` on. |
| `work.stream.resize` | id, cols, rows | Set the terminal size in character cells (sends SIGWINCH). |
| `work.stream.signal` | id, sig='INT' | Signal the terminal's process group: INT, TERM, HUP, KILL. |
| `work.stream.write` | id, data, enc='t' | Write raw input to the terminal (keystrokes, pastes; send "\r" for Enter, "\x03" for Ctrl-C). |

**wake**

| Cap | Args | Does |
|---|---|---|
| `agent.wake` | room, thread_id='', title='', transcript?, woken_by='', note='' | Spawn the configured local agent to attend `room`. |
| `agent.wake_info` | — | Report whether this host can wake an agent and which one. |

**work**

| Cap | Args | Does |
|---|---|---|
| `work.adopt_page` | session_id, digest, offset, data, final=false | Archive and adopt legacy web state before the web removes its copy. |
| `work.command` | session_id, command |  |
| `work.create` | session_id, command_id, cwd, title='', model='' |  |
| `work.status` | sessions |  |
| `work.view_page` | session_id, since=0, token='', offset=0 | A stable, bounded page of a view delta; content never enters web storage. |
<!-- END GENERATED: worker-caps -->
