# Tool and capability reference

The tables are generated from the code by `tools/gen_skill_reference.py`; don't edit them by hand. Args: `name` is required, `name?` optional, `name=x` has a default. Descriptions are the first sentence of each docstring; the full text is in the MCP tool description or `caps.describe`.

## Notes that matter

- `rook_call` needs `worker` (name or id). Reply: compact `{ok, id, from, result|error}` (`id` = journal id, `from` = worker name) plus `_tips`/`_task`/`_unread_chat` only when new; `text=true` for plain text.
- `rook_workers` defaults to name/description/build/hb/age per worker; filter with `name`, `cap_prefix`, `online`, and pick columns with `fields` (`"all"` = full rows). `rook_caps` maps each cap to `"*"` (every worker), `{all_but:[…]}` or names; narrow with `prefix` or `worker`. Unfiltered, both still grow with the fleet.
- `rook_journal` listings omit reply bodies; `call_id=` returns the full stored reply.
- `rook_console_read`: pass the previous `last_seq` back as `since_seq`; lower `limit`.
- `rook_task(action="deck")` unfiltered returns every project's lists (tens of KB on a busy band): narrow with `id=<project>` and `data {states: ["in_progress","blocked"], done_days: 1, fields: […], project_fields: ["id","title","state"]}`. Page over projects with `data {limit: N, offset: next_offset}` until `next_offset` is null; the reply leads with `total_projects`, `total_tasks`, `loose_tasks` (tasks under no project, listed as `loose` on the first page) and the `filters` applied.
- `rook_task`, `rook_project`, `rook_concept`, `rook_knowledge`: writes need a unique `request_id`. Reusing the same `request_id` for the identical write returns the first result (safe retry); reusing it for a different write fails. Search returns 5 excerpt rows by default (`data={"limit":N, "fields":[…]}`, `fields:"all"` for whole records); list returns 20 (`data` keys `parent,state,limit,offset,attention,worker,fields`). `get` returns the full record.
- `shell.exec` has **no output cap**; trim in the command. `file.read` defaults to 8 MiB; set `max_bytes`. `file.list` with `include_hidden=false` and a small `max_entries`.
- Android workers (the app) add the caps under *Android app capabilities* below: `ui.text` (on-screen text, the cheapest way to "see"), `sms.*`, `calllog.list`, `contacts.search`, `notify.*`, `location.get`, `device.*`, and native `battery`/`hid`/`screenshot` in place of the desktop ones.
- Custom caps appear as `cmd.<name>` on the worker that defined them.

## MCP tools

Every tool also takes `receipt`: repeat the receipt from the session brief (`_brief`, on your first reply) so the hub knows you still hold it.

<!-- BEGIN GENERATED: mcp-tools -->
| Tool | Args | Does |
|---|---|---|
| `rook_approvals` | action='summary', item?, relayed_by?, reason?, answer?, kind? | What waits on the person (holds, cards, questions, trust proposals). summary \| list (kind?). approve/deny ite… |
| `rook_call` | cap, why, args?, worker_id?, worker?, timeout?, hint?, text? | Run a cap on one worker. worker= (name or id) is required. |
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
| `rook_console_open` | worker, task, cmd?, argv?, cwd?, env?, pty?, task_id? | Run a slow, interactive or worth-keeping command as a console room, searchable after it exits. |
| `rook_console_read` | room, since_seq?, tail?, limit=300 | Console output after since_seq (page with last_seq), or tail=true for the last limit lines. state says live o… |
| `rook_console_search` | query, worker?, limit=20 | Full-text search of all console sessions; titles and summaries rank highest (search for the task, not the com… |
| `rook_console_signal` | room, sig='TERM' | Signal a live console's process group: TERM, KILL, INT (ctrl-C) or HUP. |
| `rook_console_write` | room, text, newline=true, literal? | Type into a live console's stdin, verbatim (no shell); {{secret:x}} types a vault value unless literal=true. |
| `rook_handoff_get` | thread_id | A thread's current handoff plus history. |
| `rook_handoff_list` | limit=20, active_only=true | Recent handoff threads (latest per thread) with goal and freshness. |
| `rook_handoff_save` | goal?, thread_id?, state?, decisions?, next_steps?, artifacts?, supersedes?, transcript_ref?, task?, status='active' | Save a handoff (state, not a transcript) so another agent can continue without asking: goal, state, decisions… |
| `rook_huddle` | action='read', project?, text?, data? | Huddle: a project's group room the person watches (project=; none: the band's). status data {working_on, bloc… |
| `rook_install_claude_code` | installed? | Install or update the Rook mod for Claude Code. installed= the output of `claude plugin list --json`; returns… |
| `rook_jobs` | action='list', id?, data?, query? | Scheduled hub jobs. |
| `rook_journal` | call_id?, worker?, cap_prefix?, since_secs?, only_failures?, limit=30 | Recorded rook_call replies. call_id=<reply id> returns that call's full output (recover lost or timed-out out… |
| `rook_knowledge` | action='search', band?, id?, query?, data?, request_id? | Shared wiki: search\|get\|list\|context\|status\|create\|update\|link\|retract\|bands. search: 5 excerpts (data {limit… |
| `rook_presence` | — | Agents seen over the MCP recently (online within ~90s) and live band workers. |
| `rook_project` | action='list', band?, id?, query?, data?, request_id? | Projects (outcomes) under concepts: list/search/get/create/update/link. create data {title, body, parent: con… |
| `rook_secret` | action='list', name?, value?, description? | Vault: list (names only) \| get name (logged) \| set name value description \| delete name \| log name?. |
| `rook_task` | action='deck', band?, id?, query?, data?, request_id? | Tasks. deck (id=project narrows): in progress with claimants and latest handoff, blocked, paused, todo, recen… |
| `rook_trust` | action='summary', data? | Hub-enforced trust map: sacred asks the person first; the rest go. summary\|get\|history\|check data {cap, worke… |
| `rook_whoami` | — | Your identity as this hub records it (agent_id, key_id, kind, identity). |
| `rook_workers` | name?, cap_prefix?, online?, fields? | Workers on the band. |
<!-- END GENERATED: mcp-tools -->

## Hub capabilities (worker `rook`)

The hub appears on the band as the reserved worker `rook`, serving the caps of hub-placed plugins: `rook_call(cap="hub.info", worker="rook")`. Caps marked `tool=True` also get their own MCP tool (listed above). Calls arriving over the band (not through the MCP) may only reach `read` caps. `fields?` means the cap takes a `fields` projection (`"*"` for every key).

<!-- BEGIN GENERATED: hub-caps -->
| Cap | Args | Risk | Does |
|---|---|---|---|
| `approvals.list` | kind?, summary_only=false | read | What is waiting on the person: held calls, held job calls, huddle cards, pinned questions, house asks and tru… |
| `approvals.relay` | item, decision, relayed_by?, reason='', answer='' | write | Approve or deny one waiting item on the person's behalf: relayed_by {who, quote, session} is their own decisi… |
| `attention.log` | limit=30, since=0 | read | The last 30 days of decisions and reminders, newest first: one plain line each (what happened, the summarized… |
| `attention.notices` | limit=20 | read | Recent short summaries (at most 140 characters) of urgent and family notifications the phones reported, newes… |
| `attention.send` | text, title='', urgency='normal', channel='digest', explain='' | write | Put an alert on the person's phones (attention.devices): text, title?, urgency urgent\|normal (urgent rings on… |
| `attention.status` | — | read | Per phone: online, last poll, last event, queued alerts, open cards, and when the person last saw, opened or… |
| `caps.describe` | prefix='' | read | Arg schema, docstring and declared risk/limit/fields for every hub cap. |
| `ccmod.activity` | limit=50 | read | What the rules did recently: posts, notifications, capability runs. |
| `ccmod.delete` | id, note='' | admin | Delete a rule (push afterwards to take it off the workers). |
| `ccmod.get` | id | read | One rule. |
| `ccmod.history` | id='', limit=20 | read | Attributed changes to the rules, newest first (`id`: one rule). |
| `ccmod.list` | — | read | Every Claude Code rule, with a one-line summary and its risk tier. |
| `ccmod.preview` | worker, remove=false | admin | What a push would write on `worker`: per settings file, the action and a unified diff (nothing is written). |
| `ccmod.push` | worker='', remove=false | admin | Send the rules to `worker` (default: every online worker). |
| `ccmod.set` | rule, note='', dry_run=false, expect_rev? | admin | Create or replace a rule. |
| `ccmod.status` | — | read | Each worker: whether it can take rules, what was last pushed and whether that is still current. |
| `change.catalog` | action='list', category='', words='' | write | The kinds that go as standard changes: list, or add (the person's "that kind is fine", with their words). |
| `change.decide` | id, yes=false | write | A person's answer to a change request that waits for them (the card does the same). |
| `change.digest` | weekly=false | read | What the next digest would say (the last day, or the last 7 days). |
| `change.get` | id | read | One change request with its checks and its step log. |
| `change.irreducible` | id, why='' | write | A normal change the huddle can't make standard: one card to the person now. |
| `change.list` | states?, days=7 | read | Change requests updated in the last `days`, newest first (states: filter). |
| `change.reduce` | id, into?, note='' | write | A normal change made into standard change requests (their ids): it is closed. |
| `change.revert` | id | write | Roll a kept change back with its own rollback (the person's "revert that"). |
| `change.submit` | data? | write | A change request: data {kind standard\|emergency\|normal, category, what, why, machines, plan, scripts [{worker… |
| `change.update` | id, data? | write | Write the words of a change request afterwards (an emergency's what, why, plan); what ran never changes. |
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
| `help` | topic='', detail=false | read | Rook help for agents: no topic gives the core model and the topic menu; `topic` (a name, a tool, a cap, a hub… |
| `home.ask` | question, context='', source='' | write | Ask the hub's home agent a question or give it a request; returns its answer. |
| `home.digest` | limit=20, since=0.0 | read | What the house agent decided for the person without asking, newest first, each with the entry it cited (spoke… |
| `home.explain` | ask, more=false | read | One of the house agent's asks explained: 2-3 plain sentences, and more detail with more=true. |
| `home.ledger` | host='' | read | Borrowed state: what changed away from a host's normal state, why, whose words, how to restore it (spoken: a… |
| `home.overturn` | decision, words, relayed_by? | write | Overturn a decision the house agent made for the person (digest): it tightens the entry it cited (trust entry… |
| `home.playbook` | — | read | The house agent's playbook: its routing rules by section (Just do it, Ask me first, coding sessions, huddles,… |
| `home.playbook_edit` | op, words, section='', text='', id='' | write | Change the house agent's playbook on the person's correction: op add (section, text) or remove (id); words: t… |
| `home.projects` | — | read | Projects the house agent set up from a conversation, and its brainstorm and intake threads (stage, plan read… |
| `home.restore` | what, words='', relayed_by? | write | Bring borrowed state back ("bring Strata back"): what = the resource, project or lease; runs the recorded res… |
| `home.session_ask` | text, role='house' | write | Send text to a persistent role's main session through its inbox, with no hub tools (tests, operators); for th… |
| `home.session_compact` | lane='main', role='house' | write | Compact a persistent session now (lane main or voice): the main lane saves durable facts to memory first; the… |
| `home.session_flow` | tool, args?, role='house' | write | Run one of the voice conductor's flow tools (queue, steer, fork, fold, status, btw, pause, resume, cancel) as… |
| `home.session_note` | text, via='report', role='house' | write | A background note for a persistent role (a session report, a job result): batched into its next turn ("While… |
| `home.sessions` | — | read | The house agent's persistent sessions: the session host, each role's line ("house agent: busy, 2 waiting, 1 f… |
| `home.settle` | question, positions?, cap='', worker='', repo='', branch='', project='', ruling='', rule='', room='' | write | A disagreement between agents for the house agent to settle: question, positions [{who, says}], the action at… |
| `home.status` | — | read | Whether the home agent is on, its identity, endpoint host and model, and the last error (never the key). |
| `home.voice_models` | — | read | The house agent's models: the voice (front: Haiku 5.5, Haiku 4.5, Sonnet, or local on kaiju), the agent (back… |
| `home.voice_models_set` | front='', back='', front_effort?, back_effort?, words='' | admin | Change the house agent's models (the person's call): front (the voice: claude-haiku-5-5, claude-haiku-4-5, so… |
| `hub.info` | — | read | What the hub runs: version, core API, roles, facts and plugins. |
| `hub.plugin.disable` | module | admin | Unload a hub plugin now (its caps go, its stop() runs) and keep it off. |
| `hub.plugin.enable` | module | admin | Load a hub plugin module now (placement, settings, available(), bind_host, start). |
| `hub.plugin.reload` | module | admin | Restart one hub plugin with its current settings (stop, load again, start). |
| `hub.plugins` | limit=50, fields? | read | Full manifests of the hub's loaded plugins (placement, settings schema, guidance slots, source). |
| `huddle.archive` | huddle, archived=true | write | Archive a huddle (hidden from the list, kept; deleted after huddle.retention_days) or unarchive it. |
| `huddle.card` | huddle, card='', action='approve', data? | write | An action card: approve (runs its call as you, under the band's policy) or dismiss; people only. propose (dat… |
| `huddle.list` | archived=false | read | Every huddle: chaired ones (roles, open cards) and project/band status huddles; archived ones with archived=t… |
| `huddle.pin` | huddle, kind, repo='', number=0, sha='', path='', url='', text='', title='', note='', sha256='' | write | Pin an artifact on a chaired huddle's table at its exact version: pr (repo, number), commit (repo, sha), file… |
| `huddle.post` | huddle, text, mention?, wait=false | write | Post in a chaired huddle. |
| `huddle.read` | action='read', room?, project?, band?, since_seq=0, limit=30 | read | Read huddles: read (room\|project\|band: statuses, questions, messages after since_seq), list, mine. |
| `huddle.start` | title='', roles?, band? | write | Start a chaired huddle: you, the house agent in the chair and roles (ids; default huddle.default_roles; [] fo… |
| `huddle.table` | huddle, action='list', artifact='', what='summary', path='', offset=0 | read | A chaired huddle's conference table: list (pinned artifacts, and votes with ballots sealed until all are in),… |
| `huddle.vote` | huddle, action='call', kind='', artifact='', reason='', target='', skip_vote=false, motion='', self_review='' | write | Votes on a chaired huddle's table: call (kind merge\|qa\|deploy\|ship, artifact, reason?, target? for deploy/shi… |
| `huddle.write` | action, room?, project?, band?, text?, data?, mention? | write | Post in a huddle: status (data {working_on, blocked_on?, needs_decision?, task?}), ask (text, data {options?,… |
| `job.read` | action='list', id?, query='', data? | read | Read jobs and runs: list\|get\|runs\|run_get\|next\|validate\|guardrails_preview\|describe_schema. |
| `job.write` | action, id?, data? | write | Write jobs: create\|update\|delete\|enable\|disable\|run\|cancel\|set_guardrails\|settings\|lock\|unlock. |
| `knowledge.read` | action='search', band?, id?, query='', data? | read | Read the shared wiki: search\|get\|list\|context\|status\|bands\|deck. |
| `knowledge.write` | action, band?, id?, data?, request_id? | write | Write the shared wiki: create\|update\|link\|retract (needs request_id). |
| `llm.check` | provider='' | read | llm.check — whether a provider (or every one) answers, with latency. |
| `llm.models` | provider, live=false | read | llm.models — model ids one provider offers (live=true asks its /models). |
| `llm.providers` | fields? | read | llm.providers — the model providers this hub can use: id, kind, host, offered models, last check. |
| `manual.get` | id, source='auto' | read | One manual page by id (`getting-started`; `manual-` prefix optional): title and Markdown body. |
| `manual.list` | source='auto' | read | The manual's table of contents: id, title, since, deprecated, preview, and whether the page is read from the… |
| `manual.status` | — | read | The bundled manual and its knowledge copy: version, page count, what an update would add, change and remove (… |
| `manual.update` | action='update', overwrite? | admin | Update the knowledge copy (`action` update; `overwrite` = page ids whose local edits may be replaced), put th… |
| `memory.consolidate` | force=false | write | Tidy up now: summarise conversations, roll up old ones, merge, fade, re-embed. |
| `memory.edit` | id, text?, kind?, pinned?, about?, approved? | write | Change a memory: new text or kind replaces it (history kept). pinned keeps it from fading; approved lets a sk… |
| `memory.forget` | id, purge=false, reason='' | write | Forget a memory. purge=true also erases its text and every earlier version (kept otherwise, as forgotten). |
| `memory.list` | kind?, about?, state='active', query='', limit=50, offset=0 | read | List memories, newest first, by kind, person and state (active\|superseded\|archived\|forgotten\|all); query filt… |
| `memory.recall` | query='', kind?, about?, limit=5, include_archived=false | read | Search memory (keywords, and meaning when embeddings are on). |
| `memory.remember` | text, kind='fact', about?, pinned=false, supersedes? | write | Remember one statement (write rules apply: no secrets, dedupe, a changed fact replaces the old one). kind: pr… |
| `memory.show` | id | read | One memory in full: text, provenance, what it replaced and its history. |
| `memory.status` | — | read | Counts by kind and state, embeddings and the last tidy-up. |
| `notify.channels` | — | read | Which notification channels are running on this hub. |
| `notify.send` | text, channel='all' | write | Send a notification to the chat integrations. |
| `persona.assign` | scope, profile='', target='', note='' | admin | Assign a profile at a scope: `default` (everyone), `band` (band id), `family` (claude-code, codex, hermes, vo… |
| `persona.delete` | id, note='' | admin | Delete an unassigned profile (its history stays). |
| `persona.get` | id='', family='', user='', band='', harness='' | read | A persona profile with its rendered `text`. |
| `persona.history` | id='', scope='', target='', limit=20 | read | Attributed changes, newest first. |
| `persona.list` | — | read | Every profile (id, name, rev) and every scoped assignment. |
| `persona.render` | harness='', profile='', user='', band='' | read | Just the rendered persona for a harness: `{text, profile, rev, sha}`. |
| `persona.set` | profile, note='', dry_run=false, expect_rev? | admin | Create or replace a profile (band owners and operator tokens). |
| `plugin.marketplace.install` | id | admin | Install a catalog plugin: NOT IMPLEMENTED (always refuses). |
| `plugin.marketplace.list` | refresh=false, limit=50, fields? | read | Plugin catalog entries (preview): id, name, version, author, risk, caps, placement, package and whether this… |
| `policy.explain` | principal, cap, worker?, role? | read | Would this principal be allowed to call `cap` on `worker`? |
| `policy.get` | — | read | The current policy document with its revision, source, mode, lint and last load error (if the file on disk is… |
| `policy.set` | policy, note='' | admin | Replace the policy document (band owners and operator tokens only). |
| `policy.status` | — | read | Hub permission status: policy mode/revision, whether calls carry tickets (root key present), the op-key id an… |
| `role.ask` | id, question, context='' | write | One turn of a role: it answers `question` (with `context`) on its agent host, in its home directory, with its… |
| `role.delete` | id, note='' | admin | Delete a role (its history stays; its home on the agent host is left in place). |
| `role.get` | id | read | A role document: name, description, category, charter, concerns, checklist, examples, seed_memory, sources, t… |
| `role.history` | id='', limit=20 | read | Attributed changes to one role (or all), newest first. |
| `role.list` | — | read | Every role (id, name, rev, enabled, host, backend, autonomy). |
| `role.memory` | id, query='', limit=50 | read | A role's memory (scope role:<id>), read from its home on its agent host: the memories matching `query`, or th… |
| `role.memory_edit` | id, action, text='', kind='fact', memory_id='' | admin | Change a role's memory on its host: `action` remember (`text`, `kind` fact, procedure or episode) or forget (… |
| `role.move` | id, host, note='' | admin | Move a role to another agent host: its home directory (memory and config) is packed on the old host, unpacked… |
| `role.presets` | id='', category='', full=false | read | The starting roles, grouped by category (product and planning, software engineering, operations, ...): `categ… |
| `role.session_send` | id, text | write | More work for a stopped role session (it resumes on its branch). |
| `role.session_start` | role, branch, instructions, location='', title='', room='' | write | Start a coding session for a role (on a branch, in a worktree of a code location; it may open a pull request,… |
| `role.session_stop` | id | write | Stop a role's running coding session (its terminal is closed; its worktree and branch stay). |
| `role.sessions` | role='', id='', live=false, limit=20 | read | The coding sessions roles started: state, newest status, branch, worktree, pull request (`role` narrows, `liv… |
| `role.set` | role, note='', dry_run=false, expect_rev? | admin | Create or replace a role (band owners and operator tokens). |
| `role.workspace` | id, path='' | read | A role's workspace, read-only (its drafts, notes and working documents, in its home on its agent host): the f… |
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
| `trust.check` | cap='', worker='', repo='', branch='', project='', tier='' | read | The ruling for an action, with its citation: a cap on a worker (its tier is the band's), or repo + branch (a… |
| `trust.get` | section='' | read | The trust map: summary (what agents get in context) and the map; section: entries\|exceptions\|baseline\|ledger\|… |
| `trust.history` | limit=30, entry='' | read | Changes to the trust map, newest first: who, when, the person's words, before and after (entry: one entry's). |
| `trust.propose` | change?, why='' | write | Ask the person to change the trust map: change {op, data} (set_entry, delete_entry, set_unlisted, add_excepti… |
| `trust.tighten` | op, data?, words='', session='' | write | Make the trust map stricter (a person only; agents, roles and the house agent are refused): op set_entry (new… |
| `updates.apply_hub` | tag | admin | Update the hub to release `tag` (restarts it). |
| `updates.check` | — | admin | Check GitHub for new releases now. |
| `updates.history` | n=30 | read | Recent update events: hub updates, rollouts, rollbacks, APK publishes. |
| `updates.prepare_hub` | tag | admin | For a `rook hub` install the hub cannot switch itself: download and verify release `tag`, leave a signed hub… |
| `updates.rollout` | workers?, build=0 | admin | Staged worker rollout of `build` (default: the newest the hub has) to `workers` (ids or names; default all),… |
| `updates.status` | — | read | Hub version against the newest release, how many workers are behind, the running rollout, and the mode/channe… |
| `users.explain` | cap, worker?, user? | read | Whether a person (default: you) may call `cap` on `worker`, and which grant decides. |
| `users.grant` | user, kind, words, ref='', caps?, tier? | admin | Give a member access. |
| `users.history` | user?, limit=50 | read | Who changed whose access, when, and in whose words (newest first). |
| `users.list` | — | read | Everyone with an account or access: role, grants, in plain words. |
| `users.me` | — | read | Your own role and grants, in plain words. |
| `users.request` | words | write | Ask the hub's owners for access, in your own words ("the living room PC and the recipes folder"). |
| `users.revoke` | grant, words | admin | Take a grant away (kept in the history with `words`). |
| `users.role` | user, role, words | admin | Make someone an `owner` or a `member`. |
| `users.settle` | what, words | admin | Close what waits on you, with your words: a new machine or band (`worker:<id>` / `band:<id>`, from `users.lis… |

### approvals (relayed)
`rook_approvals(action="summary")` says what is waiting on the person (held calls, held job calls, huddle cards, pinned questions, trust proposals); `action="list"` gives each item's name, who asked, what exactly would happen, risk, source and age. Read it back tersely: count, then name + one line each. When the person tells you their decision (also on something you did on their word), record it: `rook_approvals(action="approve"|"deny", item=<name or id>, relayed_by={"who": <person>, "quote": <their exact words>, "session": <conversation URL or id>}, reason=<why, for a deny>)`. Never relay your own judgement, never decide items they did not name, and leave held calls, trust proposals and dashboard-only items to them; withdraw your own question that no longer needs them (rook_huddle withdraw).

### attention (the person's phone)
`attention.send {text, title?, urgency: urgent|normal, channel?, explain?}` on worker `rook` puts an alert on the person's phone (queued while it is offline). Use it sparingly: only what they must know now. `attention.status` says whether they saw, opened or unlocked the phone since (no answer is not unseen); `attention.notices {limit}` lists short summaries of their urgent and family notifications. Approvals reach the phone by themselves: they queue quietly and the phone reminds the person out loud when the room is quiet. `attention.log {limit}` reads the 30-day log of decisions and reminders.

### Claude Code rules
Hub-managed triggers and actions for Claude Code on the workers (dashboard: Manage > Claude Code). `rook_call(worker="rook", cap="ccmod.list")` lists them; `ccmod.status` shows what each worker holds. Changing or pushing rules writes people's Claude Code settings: ask the user first.

### change requests
A change that could hurt is a change request: `rook_call(worker="rook", cap="change.submit", args={"data": {kind, category, what, why, machines, plan, scripts:[{worker, run, detach?}], tests:[{worker, run, expect_rc?, expect?}], rollback:{worker, run, dry_run, point}}})`. standard (routine deploys, reversible config or database changes): the hub checks it, runs it, tests it and rolls back by itself. emergency (a service is down): fix standard-ly (rollback, restart, failover) and it runs at once; write the rest with `change.update` afterwards. normal (risky or permanent): the huddle gets one round to reduce it to standard changes (soft delete, narrow scope, stage, time-box, snapshot first; `change.reduce` / `change.irreducible`), then the person gets one card. `change.get`, `change.list`. Reads and routine job runs need no change request.

### chat rooms
Persistent rooms shared by agents, people and workers. MCP: `rook_chat_*`. Over the band, on worker `rook`: `chat.read` (action rooms|read), `chat.write` (action start|send), `chat.delete`, `chat.presence`. Band callers are recorded as `band:<identity>`; posting over the band needs `ROOK_HUB_BAND_MAX_RISK=write`.

### decide
One-pass decision model (worker `rook`). `decide.run(state, questions)` answers a batch of `{id, type: choice|score|noul, question, options|levels}` in one pass; probabilities are uncalibrated. `decide.drive(goal, screen_worker, input_worker?, dry_run?, texts?, keys?)` runs screenshot -> decide -> `hid.*` with confirmation gates and returns a `run_id`; dry-run is the default. Watch it with `decide.runs(run_id=...)`, approve with `decide.confirm(run_id, approve)`, kill with `decide.stop()`.

### discord
When the Discord integration is on: `discord.send` (text) posts to the configured channel; `discord.status` shows whether it is connected. Prefer `notify.send` to reach every configured channel.

### help
`rook_call(worker="rook", cap="help")` returns the core model and the topic menu; `args={"topic": …}` one topic (a name, a tool, a cap, a hub plugin or a few words); `"detail": true` adds its reference pages.

### home agent
The hub's own LLM (when the operator has set one up). Ask it with `rook_call(worker="rook", cap="home.ask", args={"question": "..."})` (or `rook_home_ask`); `home.status` says whether it is on. People reach it in chat as `@home` (or its configured name).

### hub
`rook_call(cap="hub.info", worker="rook")` returns the hub's version, core API, roles, facts and plugins. `hub.plugins` lists full plugin manifests; pass `fields="*"` for every key.

### huddles
A huddle is a project's (or band's) group room in the person's view. `rook_huddle(action="status", project=..., data={working_on, blocked_on?, needs_decision?, task?})` when you start, get stuck or finish; `action="ask"` with `text` (and `data.options`) for a decision only the person can make: it stays pinned until answered, and the answer comes back as a chat message (and wakes you if your worker can be woken). `read` shows the status strip, questions and recent messages.
A *chaired* huddle (`action="start"`, data {title, roles}) is the person's standup with the house agent in the chair and roles (pm, qa, lead): your posts there are a guest's and start a round only when they mention @<role> or the chair. You are never called on. Action cards are approved by the person, not by agents.

### jobs
Scheduled, branching work the hub runs. `rook_jobs(action="describe_schema")` gives the job JSON schema; `validate` checks a draft (`data` = the job); `create` saves it (you are its owner and runs act as you). A job is `steps` (cap, fanout, tool, wait, notify, ask, agent, join, noop) wired by `on: {success, failure, hang}` from `entry`, fired by `triggers` (cron in the hub zone unless `tz`, at, after, manual). `run` starts one now; `runs` / `run_get` show masked step output; `next` lists fire times. Over the band: `job.read` / `job.write` on worker `rook`. Use `{{secret:name}}` in step args, never values. Guardrails block admin-tier, update, deauth, policy and delete caps in job steps (`blocked_by_guardrail` on a job; `guardrails_preview` shows what a change would block); editing someone else's job makes it yours and resets its identity.
Watchers and one-offs must end themselves: give the step that reports `complete: "success"` (or let an agent step `finish` with `complete: true`) and set `limits` (`max_runs`, `until`, `max_failures`). Never rely on a job disabling itself through `rook_jobs`. The same message to the same target is sent at most once per `limits.notify_window`. A job with `locked: true` can be changed only by a person on the dashboard; agents get `locked:` errors and must ask the user.

### knowledge
The shared wiki. Use the `rook_knowledge` tool: `search` before starting (5 hits, each with the passage that matched and its `section`; `data.limit`/`data.fields` for more), `get` a page by id or slug (its [[links]], backlinks and semantically `related` pages come as `{slug, title, gist}` stubs: get one by slug, no new search), `create` a page with a unique `request_id`. Over the band the same actions are `knowledge.read` (search/get/list/context/status/bands) and `knowledge.write` (create/update/link/retract) on worker `rook`; band callers reach only the read cap by default. Semantic search needs an embedding service (setting `embedder`).

### model providers
The language models this hub can reach: `llm.providers` (ids, hosts, models), `llm.models(provider=…)`, `llm.check(provider=…)` on worker `rook`. Read only; the home agent uses one of them.

### manual
The Rook user manual (plain-language help for every feature). In the knowledge wiki it is the page `rook-manual` and its children `manual-<topic>`; `manual.list` and `manual.get(id)` on worker `rook` read it even when the wiki is off. Check it before explaining a Rook feature to a person.

### memory
What the hub's home agent remembers (people, facts, conversations, skills). `memory.recall(query)` on worker `rook` searches shared memory (and your own); `memory.remember(text, kind)` (kind fact, or profile about yourself) adds one; `memory.forget(id)` removes one. Other people's memory, skills (procedures) and pinning are the operator's. Never store secrets. People browse and edit it on Manage > Memory.

### notify
`rook_call(worker="rook", cap="notify.send", args={"text": "..."})` posts a notification to every configured chat integration (Telegram, Discord); `channel="telegram"` picks one.

### persona
One persona for every harness. `rook_call(cap="persona.get", worker="rook", args={"family": "claude-code"})` returns the persona that applies to you (user > family > band > default) with its rendered `text`. To install it in a harness file on a machine: `rook_call(cap="persona.apply", worker="<name>", args={"harness": "claude-code", "dry_run": true})` (then without `dry_run`; `remove=true` takes it out). It only edits between its own markers. `persona.set` / `persona.assign` are admin: ask the user first.

### plugin marketplace (preview)
`plugin.marketplace.list` on worker `rook` lists catalog entries (name, risk, caps, package). Installing is not implemented yet: `plugin.marketplace.install` always refuses. Switch installed plugins with `worker.plugin.enable/disable` on a worker or `hub.plugin.enable/disable`.

### policy
`rook_call(cap="policy.explain", worker="rook", args={"principal": "role:agent", "cap": "shell.exec", "worker": "<name>"})` shows whether a call would be allowed and which rule decides it. `policy.get` returns the document; changing it (`policy.set`) is for band owners and operator tokens.

### roles
Huddle roles (project manager, QA, lead developer, ...) each have a charter, a tool scope and their own memory, kept in the role's home directory on its agent host. `rook_call(worker="rook", cap="role.list")` lists them; `role.ask` with `{"id": "qa", "question": ...}` runs one turn of a role. `role.set`, `role.move` and `role.memory_edit` are admin: ask the user first.

### serves
What each worker hosts: `serves` on a `rook_workers` row is `{sites: [{name, url}], services: [{name, url}]}`, written by hand. `rook_call(worker="rook", cap="serves.list")` returns all of it; `serves.set(worker, sites?, services?)` replaces the lists it is given; `serves.clear(worker)` removes the entry.

### settings
Hub, band, worker and user settings with their source (default / hub / band / worker / user / file / env) on worker `rook`: `settings.get(key=…)` or `settings.get(prefix="core.", scope="hub")`, `settings.history`, `settings.describe`. Writes (`settings.set`, `settings.reset`, `settings.apply_worker`) are admin actions: ask the user first. A key set by an environment variable is locked; the reply says which.

### tasks
Tasks, projects and concepts. Use `rook_task(action="deck")` to see what is on; `claim` a task before working (your calls, consoles and handoffs then link to it); finish with `update` state done + `attrs.outcome` + an evidence `link`, or leave a handoff. Over the band: `task.read` (deck/search/list/get) and `task.write` (create/update/link/retract/claim/release) on worker `rook`, with `kind=task|project|concept`. Hygiene nudges ride replies as `_hygiene` and show on the deck; `rook_task(action="hygiene")` lists open ones (`data {mine: true}`). A commit message with `rook: <task id, slug or name>` links the commit to that task as evidence when you hold a claim on it. Tasks and projects have speakable names (`brave-otter-lamp`): any `id` takes one; a write needs it exact.

### telegram
When the Telegram integration is on: `telegram.send` (text) posts to the configured chat; `telegram.status` shows whether it is connected. Prefer `notify.send` to reach every configured channel.

### trust map
The hub enforces a trust map: a `sacred` host, project, repo or resource changes only after the person says so (you may read, propose and prepare); `guarded` and `ship-it` entries just go unless the advanced security plugin is on. A held call cites its entry. `rook_trust(action="summary")` before acting on something you are unsure of; `action="check", data={"cap":…, "worker":…}` gives the ruling. Never try to get around a trust hold. To change the map, `action="propose", data={"change": {"op": "set_entry", "data": {...}}, "why": …}`: the person decides on the dashboard. Only the person changes it; a proposal that only makes it stricter is refused.

### updates
`updates.status` on worker `rook`: hub version vs the newest release, workers behind, the running rollout. Applying updates (`updates.apply_hub`, `updates.prepare_hub`, `updates.rollout`) is for operators: ask the person who runs the hub first.

### users
People on this hub are owners (reach everything) or members (reach only what a grant names: a band, a machine, the hub's caps, a knowledge folder, or anything; optional cap patterns and a tier ceiling, default exec). New people reach nothing. `users.me` shows your own access; `users.explain` whether a call would be allowed. Owners change access with `users.grant` / `users.revoke` / `users.role`, always passing `words`: the person's own words asking for it. Change access only when a person asks you to in conversation. A new person asks for access with `users.request` (their words); owners see what waits in `users.list` and close a new machine or band as owners-only with `users.settle`.
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
| `worker.plugin.reload` | module | Restart one loaded plugin with its current settings. |

**agenthost**

| Cap | Args | Does |
|---|---|---|
| `agenthost.cancel` | turn_id | Stop a running turn. |
| `agenthost.models` | harness='claude' | Models a harness here can use (claude: aliases; codex: its catalog; opencode: `opencode models`). |
| `agenthost.role_export` | role | Pack a role's home (memory and config, not host-local scratch) to move it to another host. |
| `agenthost.role_import` | role, archive, sha256, replace=false | Unpack a role's home exported from another host. |
| `agenthost.role_memory` | role, query='', limit=50 | A role's memory (scope role:<id>, in its home): the memories that match `query`, or the newest. |
| `agenthost.role_memory_edit` | role, action, text='', kind='fact', id='', by='' | Change a role's memory: `action` remember (`text`, `kind` fact, procedure or episode) or forget (`id`). |
| `agenthost.role_set_aside` | role | Move a role's home out of the way on this host (after a move to another host); it is renamed, never deleted. |
| `agenthost.roles` | — | The role homes on this host: {ok, base, roles: [{role, path, rev, tools, memory_mode, memories, bytes}]}. |
| `agenthost.status` | — | The agent harnesses on this host (claude, codex, opencode): version and login, re-read now; and the running t… |
| `agenthost.turn` | turn_id, prompt, system, mcp_url, mcp_token, harness='claude', model='', timeout_s=180.0, max_tool_calls=20, effort?, oauth_token?, role?, role_name='', role_rev=0, tools?, builtin?, memory='write', workspace=false | Run one house agent turn with a local harness; returns its reply. |

**battery**

| Cap | Args | Does |
|---|---|---|
| `battery.status` | — | Current battery: `{percent, charging, plugged, status, ...}`. |

**camera**

| Cap | Args | Does |
|---|---|---|
| `camera.list` | — | List the cameras on this worker (use one as the `camera` arg to snap). |
| `camera.snap` | camera=0, quality=85, resolution='1280x720' | Capture a still JPEG from a camera. |

**ccmod**

| Cap | Args | Does |
|---|---|---|
| `ccmod.apply` | config, dry_run=false | Put the hub's Claude Code rules into effect here (the hub pushes them). |
| `ccmod.events` | after=0, wait=0, boot='' | Hook events for the hub's remote actions, those after `after` (from this `boot`; a different boot starts over… |
| `ccmod.remove` | dry_run=false | Take Rook's managed hooks and status line out of every Claude Code installation here and delete the rules (th… |
| `ccmod.status` | — | Which Claude Code installations here carry Rook's managed hooks, and the revision of the rules this worker ho… |

**cec**

| Cap | Args | Does |
|---|---|---|
| `cec.ping` | — | Check the Pico-CEC bridge answers; returns its reply (logical/physical address). |
| `cec.raw` | cmd | Send one literal Pico-CEC firmware command line (e.g. `"send 0 36"`) and return its reply. |
| `cec.send` | addr, opcode, operands? | Transmit a CEC frame: `addr` destination (0-f, 0 = TV), `opcode` byte, optional `operands` (ints or hex strin… |

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
| `claude-history.follow` | session_id, offset=0, version='', tail=0 | Check the selected log and return only its changed tail, in stable pages. |
| `claude-history.pull` | machine?, path?, limit=50, offset=0 | List session metadata under `path` (default `~/.claude/projects`). |
| `claude-history.read` | session_id, path?, machine?, max_messages=1000, offset=0 | Read a session transcript. |
| `claude-history.read_page` | session_id, path?, offset=0, content_offset=0, max_chars=6000, snapshot? | Read a bounded transcript page, including partial large messages. |
| `claude-history.read_snapshot` | session_id, path?, offset=0, content_offset=0, snapshot='' | Read a bounded page of a stable, worker-owned conversation snapshot. |
| `claude-history.resume` | session_id, path?, name?, cwd?, remote_control=true, machine? | Relaunch a stored Claude Code session here (deprecated: use `work.stream.open` with resume=). |
| `claude-history.resumed` | — | Sessions this worker relaunched and whether they're still up. |
| `claude-history.search` | query, path?, machine?, limit=20, ignore_case=true | Regex-search across all session messages. |
| `claude-history.send` | session_id, text, command_id | Send to this exact existing session (deprecated: use `sessions.send`). |
| `claude-history.transcript` | session_id, offset=0, max_chars=6000, path? | A transcript page in the stable `rook.transcript/1` export format. |

**codex_history**

| Cap | Args | Does |
|---|---|---|
| `codex-history.analyze` | pattern='tool_usage', path?, machine?, limit=20 | Extract a knowledge pattern across all sessions. |
| `codex-history.export` | session_id, format='markdown', path?, machine? | Export a session as `markdown`, `json`, or `html`. |
| `codex-history.follow` | session_id, offset=0, version='', tail=0 | Check the selected log and return only its changed tail, in stable pages. |
| `codex-history.pull` | machine?, path?, limit=50, offset=0 | List session metadata under `path` (default `~/.claude/projects`). |
| `codex-history.read` | session_id, path?, machine?, max_messages=1000, offset=0 | Read a session transcript. |
| `codex-history.read_page` | session_id, path?, offset=0, content_offset=0, max_chars=6000, snapshot? | Read a bounded transcript page, including partial large messages. |
| `codex-history.read_snapshot` | session_id, path?, offset=0, content_offset=0, snapshot='' | Read a bounded page of a stable, worker-owned conversation snapshot. |
| `codex-history.resume` | session_id, path?, name?, cwd?, machine? | Resume Codex in a Rook PTY without a prompt (deprecated: use `work.stream.open` with resume=). |
| `codex-history.resumed` | — | Sessions this worker relaunched and whether they're still up. |
| `codex-history.search` | query, path?, machine?, limit=20, ignore_case=true | Regex-search across all session messages. |
| `codex-history.send` | session_id, text, command_id | Send to this exact existing session (deprecated: use `sessions.send`). |
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
| `deluge.pause` | torrent_id='*' | Pause a torrent by id or name, or all with `*`. |
| `deluge.remove` | torrent_id, data=false | Remove a torrent by id or name. |
| `deluge.resume` | torrent_id='*' | Resume a torrent by id or name, or all with `*`. |
| `deluge.status` | — | Whether the daemon is running, plus a session summary if reachable. |

**dongle**

| Cap | Args | Does |
|---|---|---|
| `dongle.consumer` | usage | Tap a USB consumer usage, e.g. 0xE9 volume up or 0xCD play/pause. |
| `dongle.display` | text | Show up to 80 UTF-8 bytes of text on the dongle's display. |
| `dongle.display_probe` | — | Probe whether the dongle has a working display attached. |
| `dongle.keyboard` | keys, mods=0 | Tap up to six USB HID usage codes with modifier bitmask (US layout). |
| `dongle.mouse` | x=0, y=0, buttons=0, wheel=0, pan=0 | Relative move (-127..127) and/or button tap (bitmask 0..31). |
| `dongle.release` | — | Release every key and button the dongle holds on the USB host. |
| `dongle.status` | — | Identity and state of the attached Rook RP2040 dongle (device, protocol, firmware). |

**enrollment**

| Cap | Args | Does |
|---|---|---|
| `worker.enrollment_finish` | grant | Finish enrollment with the server's one-time `grant`: store the device certificate and bands. |
| `worker.enrollment_move_prepare` | server | Advertise support for authenticated cross-band configuration changes. |
| `worker.enrollment_prepare` | server | Start device enrollment with the HTTPS enrollment `server`: make a device key and CSR. |
| `worker.enrollment_prove` | migration_id, epoch, nonce | Sign a band-migration proof (`migration_id`, `epoch`, `nonce`) with the device key. |
| `worker.enrollment_status` | — | Whether this worker is enrolled, with its device id, active band and migration state. |

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
| `info.host` | — | Hostname, OS, release, machine, Python version and process id of this worker. |
| `info.ping` | — | Liveness check; returns `"pong"`. |
| `info.uptime` | — | Seconds since this worker process started. |

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
| `screenshot.capture_region` | x, y, w, h, quality=85 | Capture a rectangular region of the screen (x/y may be negative on multi-monitor desktops). |

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
| `worker.status` | — | Build, version, supervisor and update state of this worker (band key redacted). |
| `worker.update` | url?, hub?, psk?, name?, manifest? | Install a worker bundle, optionally repoint at a new band, then restart. |

**session_mirror**

| Cap | Args | Does |
|---|---|---|
| `sessions.mirror` | agent, native_id, cursor=0, wait=0, max_events=500 | Live events of a session the Rook Claude Code mod mirrors on this host, those after `cursor` (the last `seq`… |

**session_shim**

| Cap | Args | Does |
|---|---|---|
| `sessions.shim.install` | agents?, shells? | Install the session shim on this host (opt-in; off by default). |
| `sessions.shim.status` | — | Whether the session shim is installed here, for which agents and shells, where the real binaries are, and how… |
| `sessions.shim.uninstall` | — | Remove the session shim: its rc blocks, its fish file and its folder. |

**sessions**

| Cap | Args | Does |
|---|---|---|
| `sessions.follow` | agent, native_id, offset=0, version='', tail=0 | The transcript tail of a Claude or Codex session: pages from `offset` when the log changed since `version` (e… |
| `sessions.list` | limit=20, offset=0, query='', live_only=false | This host's agent sessions, live and idle first, then closed, newest first within each (record shape: docs/de… |
| `sessions.send` | agent, native_id, text, command_id='' | Put `text` in front of the agent as the user. |
| `sessions.stop` | agent, native_id | End a session Rook started: close its Rook terminal (or the `proc.*` process an older resume started). |

**shell**

| Cap | Args | Does |
|---|---|---|
| `shell.env.get` | name, default? | One environment variable (values pushed as secrets read `***`). |
| `shell.env.list` | prefix='' | The environment (values pushed as secrets or credentials read `***`). |
| `shell.exec` | cmd?, argv?, stdin?, timeout=30.0, cwd?, env? | Run a command. |
| `shell.which` | name | Full path of an executable on this worker's PATH, or null if it is not found. |

**terminals**

| Cap | Args | Does |
|---|---|---|
| `work.export` | agent, session_id, offset=0, max_chars=6000 | A page of a historical Claude/Codex transcript in the stable `rook.transcript/1` format: `{format, session, m… |
| `work.sessions` | limit=20, offset=0, history=true, query='' | One catalog of this host's work: live terminals plus Claude/Codex history, newest first. |
| `work.stream.close` | id | Stop the terminal's process (SIGHUP, then SIGKILL) and drop it. |
| `work.stream.list` | — | Live and recently finished terminals on this worker. |
| `work.stream.open` | harness='shell', cwd='', title='', model='', resume='', persona='', mcp_url='', mcp_token='', session='', remote_control='', cols=120, rows=32, buffer_bytes=262144, handoff_pid=0, argv?, cmd='', env?, task='', room='', force=false | Start a harness (shell\|claude\|codex\|hermes) under a PTY and return its terminal `id` immediately. |
| `work.stream.read` | id, cursor=0, max_bytes=16384, wait=0, accept='tbz' | Output from byte `cursor` on. |
| `work.stream.resize` | id, cols, rows | Set the terminal size in character cells (sends SIGWINCH). |
| `work.stream.signal` | id, sig='INT' | Signal the terminal's process group: INT, TERM, HUP, KILL. |
| `work.stream.write` | id, data, enc='t' | Write raw input to the terminal (keystrokes, pastes; send "\r" for Enter, "\x03" for Ctrl-C). |

**wake**

| Cap | Args | Does |
|---|---|---|
| `agent.wake` | room, thread_id='', title='', transcript?, woken_by='', note='' | Spawn the configured local agent to attend `room`. |
| `agent.wake_info` | — | Report whether this host can wake an agent and which one. |

**webos**

| Cap | Args | Does |
|---|---|---|
| `webos.discover` | timeout=3.0 | Find LG webOS TVs on this worker's network (SSDP), for adding to webos.tvs. |
| `webos.list` | — | Every configured TV: `{items: [{name, host, on, app, reachable, paired}]}`. |

**webos devices**

| Cap | Args | Does |
|---|---|---|
| `tv.apps` | — | Installed apps the TV can launch: `{items: [{id, title}]}`. |
| `tv.button` | name | Press a remote button: HOME, BACK, UP, DOWN, LEFT, RIGHT, ENTER, MENU, EXIT, digits… |
| `tv.input` | input | Switch to an external input by id or label (`"HDMI 1"`, `"HDMI_2"`). |
| `tv.inputs` | — | External inputs (HDMI…): `{items: [{id, label, connected}]}`. |
| `tv.launch` | app, params? | Launch an app by id or title (`"YouTube"`, `"netflix"`), with optional launch `params`. |
| `tv.media` | action | Playback control for the current app: play, pause, stop, rewind, fast_forward. |
| `tv.mute` | on=true | Mute (`on=true`) or unmute (`on=false`). |
| `tv.open_url` | url | Open an http(s) URL in the TV's web browser. |
| `tv.pair` | key?, timeout=60 | Pair with the TV. |
| `tv.power_off` | — | Turn the TV off. |
| `tv.power_on` | — | Turn the TV on with Wake-on-LAN. |
| `tv.screen` | on | Turn just the screen off (sound keeps playing) or back on. |
| `tv.status` | — | Live state: `{on, power, app, app_id, volume, muted, screen, reachable, paired}`. |
| `tv.toast` | message | Show a short notification on the TV screen. |
| `tv.volume` | level?, step? | Set the volume to `level` (0-100), or move it by `step` (e.g. +3, -2). |

**work**

| Cap | Args | Does |
|---|---|---|
| `work.adopt_page` | session_id, digest, offset, data, final=false | Archive and adopt legacy web state before the web removes its copy. |
| `work.command` | session_id, command | Send one command (`{id, op, ...}`) to a work session; deduplicated by `command.id`. |
| `work.create` | session_id, command_id, cwd, title='', model='' | Create (or reopen) a host-owned Codex work session in `cwd` (absolute) and open it. |
| `work.status` | sessions | Metadata (title, status, running, needs_input, has_error) of up to 20 work sessions. |
| `work.view_page` | session_id, since=0, token='', offset=0 | A stable, bounded page of a view delta; content never enters web storage. |
<!-- END GENERATED: worker-caps -->

## Android app capabilities

Native plugins of the Android app's embedded worker, in place of (or beside) the worker plugins above. Most need an Android permission or the app's accessibility service; a cap without one returns an error saying which.

<!-- BEGIN GENERATED: android-caps -->
**attention_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `attention.feed` | limit=50 | exec | The phone's quiet feed, newest first: summaries of notifications it sorted as urgent, family or normal (never… |
| `attention.poll` | since=0 | admin | Events after seq `since`: {events, last}; pass `last` next time. |
| `attention.post` | id, kind='card', line='', explain='', urgency='normal', channel='approvals', actions?, source='', expires_at=0, body='' | admin | Put a decision card on the phone; re-posting the same id updates it without ringing again. |
| `attention.remind` | id, text, sample_ms=3000, speak_unchecked=false | admin | Speak a short reminder on the phone, but only if the room is quiet. |
| `attention.rules_get` | — | exec | The notification rules in use: source default\|override and the rules document. |
| `attention.rules_set` | rules? | admin | Replace the phone's notification rules. |
| `attention.update` | id, state, message='' | admin | Show what happened after a decision: state done (brief confirmation, then the card closes), failed (shows mes… |

**battery_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `battery.status` | — | read | Current battery: `{percent, charging, plugged, status, temp_c?}`. |

**calendar_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `calendar.create` | title, start, end?, all_day=false, location='', description='', calendar='' | exec | Add an event to a calendar on the phone (the owner's own calendar). |
| `calendar.delete` | event_id | exec | Delete an event on the phone by `event_id` (from calendar.list). |
| `calendar.list` | start?, end?, limit=20 | read | Calendar events on the phone (Google Calendar, Outlook if it syncs calendars, others). |
| `calendar.update` | event_id, title?, start?, end?, all_day?, location?, description? | exec | Change an event on the phone by `event_id` (from calendar.list): only the fields given. |

**calllog_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `calllog.list` | limit=30 | exec | Recent calls, newest first: number, name, direction, duration_s, ts. |

**contacts_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `contacts.search` | query='', limit=40 | exec | Contacts whose name/number matches `query` (empty = first N). |

**device_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `device.clipboard_get` | — | exec | Read the clipboard. |
| `device.clipboard_set` | text | exec | Put text on the clipboard. |
| `device.find` | seconds=30 | exec | Ring at maximum alarm volume for 1–120 seconds; restores volume after. |
| `device.find_stop` | — | exec | Stop the find-device ring and restore the previous alarm volume. |
| `device.info` | — | read | Model, Android version, screen, uptime, and battery-independent bits. |
| `device.keep_awake` | on, minutes=30 | exec | Keep the screen on (so it never locks) for `minutes` (1–1440), then release; on=false releases now. |
| `device.keep_awake_settings` | auto?, idle_minutes? | exec | Change the passive setting now (same as the app's Settings screen): auto = "Keep phone awake while agent is w… |
| `device.keep_awake_status` | — | read | Keep-awake state: on, mode (manual\|auto), until (epoch s), remaining_s, overlay/wake_lock in use, screen_on,… |
| `device.launch` | package | exec | Launch an installed app by package name (e.g. com.android.settings). |
| `device.open_url` | url | exec | Open a URL (or any Uri) in the default handler app. |
| `device.torch` | on=true | exec | Flashlight on/off (no permission needed via CameraManager). |
| `device.unlock` | pin='', hold_ms=9000, pre_swipe=true | exec | Unlock a PIN-secured screen: wake, surface the PIN bouncer, and tap the digits at keypad positions computed f… |
| `device.unlock_status` | — | read | Whether an unlock PIN is saved in the app (never the PIN itself), and whether passive keep-awake may use it (… |
| `device.update` | channel='' | admin | Queue an APK update from the official Rook feed. |
| `device.update_status` | — | read | APK updater progress, installed/target versions, update channel, and required action. |
| `device.vibrate` | ms=300 | exec | Buzz the phone for `ms` milliseconds. |
| `device.wake` | dismiss_keyguard=false, hold_ms=1200 | exec | Turn the screen ON (and show over the keyguard) so the accessibility service can interact with the lock scree… |

**hid_a11y**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `hid.backend` | — | read | Input backend on this phone (Android accessibility service) and whether it is enabled. |
| `hid.key_combo` | keys | exec | Map a few common combos to global accessibility actions. |
| `hid.mouse.click` | x, y | exec | Tap the screen at `x`, `y` (pixels). |
| `hid.mouse.drag` | x1, y1, x2, y2, duration=0.3 | exec | Swipe from `x1`,`y1` to `x2`,`y2` over `duration` seconds. |
| `hid.type` | text | exec | Type `text` into the focused editable field (accessibility service). |

**location_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `location.get` | timeout=8.0 | exec | Get lat/lon, accuracy, age, stale flag and Maps URL. |

**notify_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `notify.act` | key, action=0, text='' | exec | Run one of a notification's own actions (by `key` from notify.list; `action`: its index or title from notify.… |
| `notify.actions` | key | read | The actions a notification offers (by `key` from notify.list): index, title ("Reply", "Mark as read"), and wh… |
| `notify.dismiss` | key | exec | Dismiss a notification by its `key` (from notify.list). |
| `notify.list` | limit=40, packages? | exec | Recent notifications, newest first. |
| `notify.post` | title='rook', text='', channel='' | exec | Post a local notification on the phone (needs POST_NOTIFICATIONS). |

**screen**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `screenshot.capture` | quality=85 | read | Capture the full screen as a JPEG via MediaProjection. |
| `screenshot.capture_preview` | — | read | Low-quality grab for quick visual checks. |

**sms_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `sms.list` | box='inbox', limit=30, since_ts=0.0 | exec | Recent SMS. |
| `sms.send` | to, text | exec | Send an SMS to `to`. |

**speak_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `voice.replies` | since=0 | read | Recent answers to voice.speak(reply=true), newest last: id (the speech id), text, via (voice/text), at_ms, li… |
| `voice.speak` | text, voice='', rate=1.0, pitch=1.0, interrupt=false, chat=true, wait=true, timeout=60, reply=false, reply_timeout=8 | write | Say `text` aloud on the phone. |
| `voice.speak_status` | id | read | State of a voice.speak id: queued, submitted, speaking, done, stopped or error. |
| `voice.speak_stop` | — | write | Stop speaking now and drop everything queued by voice.speak. |
| `voice.speak_voices` | locale='' | read | On-device TTS voices: name, locale, quality, network, installed. |

**ui_android**

| Cap | Args | Risk | Does |
|---|---|---|---|
| `ui.back` | — | exec | Press Back (the global accessibility action). |
| `ui.click` | text='', desc='', id='', contains=false, index=0, long=false | exec | Click the node whose text, desc (content description) or id (view id, with or without the package: prefix) ma… |
| `ui.nodes` | all=false, contains='', limit=300 | exec | Visible nodes on screen, top-to-bottom: text, desc, id, class, clickable, scrollable, checked, bounds [l,t,r,… |
| `ui.scroll` | direction='down', text='', id='', contains=false | exec | Scroll one page down (forward) or up (backward). |
| `ui.text` | — | exec | All visible text on the current screen (top-to-bottom). |
<!-- END GENERATED: android-caps -->
