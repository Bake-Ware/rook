# Changelog

All notable changes to Rook are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are
tagged `vX.Y.Z` (the Python package version); workers identify themselves by
build number (for example `111.stinky.goat`). Before the first tag the package
version was `0.1.0`.

## Unreleased

### Added
- The dashboard chat knows your page: each message from the @home dock (and the Home agent
  page's chat) carries the page you are on, the text you selected and what is on screen, and
  the house agent gets page tools that run in your browser with your own sign-in: `page_go`
  (open a page), `page_point` (scroll to and highlight), `page_fill` (fill a field, never
  submit), `page_click` (press a button) and `page_read`. Actions ride the chat's poll and
  answer with a fresh snapshot, only in the tab the message came from; each is journaled as
  `home.page` (typed values only by length). Filling and pressing work only on Overview,
  Start page, Work, Huddles, Knowledge, Jobs, Memory and Help (elsewhere it opens and
  points), never on delete/approve/send/update-style buttons or links off the dashboard; it
  never reads password fields or shown-once secrets (now marked `data-secret`: new tokens,
  invite links, pairing codes). Setting `home.page_tools` (on). Works on the front door, the
  plain chair and the persistent session. (manual: home-agent)

### Changed
- LG webOS TVs: `tv.power_on` now wakes TVs on Wi-Fi (it sent Wake-on-LAN only to the wired
  MAC, so it never woke them), waits until the TV answers and says why when it doesn't (including
  the TV's own "Turn on via Wi-Fi" setting); a request on a dropped connection is retried once;
  `tv.launch` understands names as people say them (`disney plus`); `tv.status` shows where the
  sound goes and whether the TV can be woken. (manual: devices)
- Dashboard sidebar in five sections (site rework from Bake's 2026-10-26 roadmap): **Feed**
  (Overview, Start page), **Work** (Work, Huddles, Sessions, Knowledge, Jobs, Memory),
  **Intelligence sources** (Home agent, Roles, Model providers, Voice, Agent instructions),
  **Trust** (Trust map, Approvals, Decisions, People, Account & access, API tokens, Secrets,
  Bands) and **Settings** (Settings, Workers, Install a worker, Plugins, Updates, Claude Code,
  Help). Every page keeps its link; `#feed` and `#intelligence` open a section's first page.
  The Trust page is labelled Trust map. Page references in the dashboard, hub messages and
  docs say the new section ("Intelligence sources > Home agent", "Settings > Updates").
  Model providers also lists the hub's other engines, the voice server and the decision
  engine, with their state and where each is set up. (manual: overview-and-start-page,
  model-providers)

### Added
- **Ready-made jobs** (Bake's 2026-10-26 roadmap: "Canned jobs: 5-minute security scan (open
  sessions, logs); attention aggregation (email, phone notifications, chats); security camera
  review (Ring, others)"). `rook_jobs action=templates` lists them and `install` makes one an
  ordinary job you own (owners only; nothing is installed by default); the Jobs page has a
  **Ready-made** tab. **Security scan** (every 5 minutes, zero config): workers, console rooms,
  MCP identities, dashboard sign-ins, denied calls, refused keys, API keys and access changes
  against the last scan. **Attention digest**: phone notifications, chat rooms (Telegram and
  Discord included) and optional email (IMAP with a vault password, or a worker cap) as one
  message. **Camera review**: snapshot URLs, RTSP via ffmpeg on a worker or `camera.snap`, a
  change test on the hub and a vision model only for changed frames; Ring through a bridge
  (ring-mqtt, Scrypted). All report quietly, only on change: the attention log and the phone's
  digest channel, never a card; urgent only for a burst of refused keys (cameras: opt-in
  `may_ring`). New hub caps `routine.security_scan`, `routine.attention_digest`,
  `routine.camera_review`. (manual: jobs)
- Tasks and their pull requests (hygiene, docs/design/hygiene.md): an agent's `update` to `done`
  is refused while a pull request the task links is still open, with what to do (get it merged
  through the huddle, or retract the link); a person gets the same text as `pr_check` and the
  update goes through; GitHub unreachable never blocks. A sweep every `hygiene_github_minutes`
  (knowledge, default 10) raises `pr_merged` (merge commit linked as evidence), `pr_ci_failed`
  and `pr_open_done` findings; the hub's own merge tells the linked tasks at once. Pinning a PR
  says where its CI stands, and a vote's `artifact` may be the PR's URL or `#N`.
  (manual: work, conference-table)
- A `bad args` reply from a hub cap lists the arguments the cap takes and the closest name
  for a mistyped one (`did you mean 'since_seq' for 'since'?`). (manual: n/a)
- Home Assistant devices on the band (`homeassistant` plugin): each chosen HA device (lamp, plug,
  thermostat, camera, fridge) becomes a device worker with live state and `ha.status`, `ha.on`,
  `ha.off`, `ha.set` and the admin `ha.service`; HA scenes from the delegate. LG TVs gain
  `tv.type` (type into the open search box) and `tv.sleep` (a sleep timer). (manual: devices)
- Browser control for agents (roadmap, Machine caps: "Playwright/browser caps"): a worker
  plugin `browser.*` on Playwright + Chromium, off until Playwright and its Chromium are
  installed in the worker's Python. Named throwaway sessions (fresh profile, never the
  person's own browser) that live across calls, close when idle (`browser.idle_timeout`) and
  are capped (`browser.max_sessions`); `browser.snapshot` gives a bounded page outline with refs
  (`e12`) that `click`/`type`/`press`/`select` take back; `read`, `wait`, `screenshot`,
  `tabs`, `eval` (exec tier), `sessions`, `close`. Refs are never reused within a session,
  sessions belong to their opener, only http(s) loads, local-network addresses are refused
  unless `browser.allow_private` is on, downloads are refused and Chromium's sandbox is on. A
  default agent tip for `browser.*`. (manual: browser)
- Devices by asking (Bake's roadmap, 2026-10-26: "add my front door lock" and the house agent
  builds the caps itself): the `devkit` worker plugin turns an agent-written definition (name,
  type, caps as http/tcp/udp/argv recipes with typed args and `extract`, a state check) into a
  device worker on that delegate, with honest risk floors (only a GET/HEAD named as a reading is a read, actions
  are physical, programs are exec, any action on a lock, alarm, valve, garage, gate or door is
  admin). `devkit.define` · `devkit.test` · `devkit.list` ·
  `devkit.remove` · `devkit.discover` (SSDP + mDNS). Credentials stay in the vault as
  `{secret:name}`, fetched at use only when granted in the delegate's `devkit.secrets` setting
  (the hub's `settings.worker_secret` honours it). Help topic `devices`; the house agent's
  instructions point to it. (manual: devices)
- Phone caps that drive the phone in fewer calls (roadmap, APK: "caps that drive the phone
  better"): `ui.screen` (a compact screen: things to act on with kind, label, value and center,
  the other text, a revision), `ui.wait` (until a node shows or goes, an app is in front, the
  screen changed or settled), `ui.type` (into a field named by its hint, label or caption, with
  optional submit), `wait=` on `ui.click` (for the node to appear first) and `screen=true` on
  `ui.click`/`ui.scroll`/`ui.back`/`ui.type`/`device.launch`/`device.open_url`/`device.intent`
  (the new screen in the same reply). The `ui.*` caps now read dropdowns and popup menus that
  open over an app (they are their own window). `device.launch label=` opens an app by name,
  `device.apps` lists them, `device.intent` starts common actions (share, dial, directions, map,
  timer, alarm, settings pages, web search, draft email or text, camera; never sends or calls),
  `device.sound` reads and sets volume, ringer and Do Not Disturb, and `media.now` /
  `media.control` show and drive what is playing. `device.open_url`, `device.launch` and
  `device.intent` add `may_be_blocked` when Android 10+ may silently refuse a start from the
  background (no overlay permission and no accessibility service). (manual: phones-and-tablets)
- Phone app **Chat with <assistant>** widget (Bake, 2026-10-27: "push to talk, but it shows
  the conversation, like it does in the app"): a resizable widget with a Talk bar under the
  recent conversation, drawn like the app's Chat tab (bubbles, interrupted replies faded, newest
  at the bottom, scrollable) and updating live as replies stream in. It replays the app's own
  conversation log, so it shows exactly what the app would. (manual: phones-and-tablets)
- Watch app: the screen shows the conversation as it goes (the phone's recent chat, then each
  heard line and reply), and the talk button and tile show a small Rook whose color is the
  state (idle, connecting, listening, thinking, speaking, error) instead of status words. One
  derived state drives both, and speaking lasts until the watch has played the reply. Watch
  turns join the phone's chat. Link message `HISTORY` (22). (manual: phones-and-tablets)
- Phone app home-screen widgets: **Talk to <assistant>** (push-to-talk to the house agent; a
  tap while it speaks stops it) and **Waiting on you** (the cards waiting on you, urgent and
  failed first, each opening its card; a mic button; more rows when made taller). Both read the
  phone's own card book and voice state, so they work offline and update as cards change.
  (manual: phones-and-tablets)
- Agent whiteboards (agent sessions, Bake's design decision 13): each claimed task gets a
  living page the hub keeps from the agent's own calls (each call's why, the machine, the
  result; repeats collapse), consoles and their summaries, handoffs, secrets used (by name) and
  commits. Agents jot goals, decisions, open items and done marks with
  `rook_task(action="jot")`; `rook_task(action="whiteboard")` reads it and a task's page under
  Work shows it. It rides the session brief when an agent lost its context, and is saved as the
  task's handoff when the agent releases or stops the task, goes idle or closes its session,
  so nobody has to ask for one. Calls now go to the claim of the MCP session that made it, not
  the newest claim of everyone sharing the token. (manual: agents-and-mcp)
- Agent help on demand: hub plugin `help`, cap `help` on worker `rook`. The skill shrinks to a
  stub; the core model and one topic per subsystem (machines, tasks, handoffs, knowledge, the
  person, chat, secrets, jobs, holds, admin) live in `skills/rook/help/` and are served in
  layers (core and menu, a topic, its reference pages). Tool replies carry a one-line `_help`
  note the first time a tool, an action or a held or timed-out call comes up in a session.
  (manual: agents-and-mcp)
- People and access (access v2): several owners per hub, everyone else a member who reaches
  only what a grant names (a band, a machine, the hub's tools, a knowledge folder, or anything;
  narrowed by caps and a tier ceiling). New people reach nothing; enforced hub-side in every
  policy mode. Caps `users.me/explain/list/grant/revoke/role/history` on worker `rook`;
  `access.db` beside `policy.json`; existing owners and band members carried over once.
  (manual: accounts-and-access)
- Access set in conversation (access v2, item 6): tell the house agent who gets what and it
  writes the grants with your words; anyone else's ask, and a new person's request in their
  own words (`users.request`), becomes one question only an owner answers (never a relayed
  answer). When a machine or band joins that some members don't reach, it asks once "Let
  Robin and Sam use the new machine attic-pc?" (setting `home.access_prompts`). Manage >
  People: everyone's access in plain words, what waits on the owners, the history, and the
  changes; members see their own access and ask there. `users.settle` closes what waits.
  (manual: accounts-and-access)
- Telegram and Discord per person (access v2, design item 5): each person links their own
  account with a one-time code (`users.link_code`, then `/link CODE` or `!link CODE` to the
  bot); a linked account acts as its person (their grants apply to commands, bridged messages
  and the house agent) and unlinked accounts are ignored. Direct messages, and the hub chat
  with no bridged room, go to the house agent and its answer comes back. The hub's bot is the
  default; `users.chat_bot` names a vault secret holding a person's own bot token. Shared
  Discord servers and Telegram groups are hub resources (`users.space`) granted like machines
  (`users.grant kind=space`). `notify.send user=` messages a person directly.
  (manual: integrations, accounts-and-access)

### Changed
- Approvals, simplified (access v2, Bake's design decision 9): only the hard stops and the
  trust map hold an agent's call. Nothing expires: an ask waits until you answer it, and an
  approval covers the one identical call it was for with no time limit
  (`approvals.approval_secs` is gone). The phone's cards, clearing cards by voice and relaying
  your quoted words work as before. (manual: accounts-and-access, security)

### Fixed
- `users.link_code`, `users.grant`, `users.role`, `users.explain` and `users.token` refuse a
  user that matches no account, instead of making a code or grant for an account that does not
  exist (a Telegram linked that way reached nothing). A unique name, user name or email now
  resolves to the account id. A member naming someone else still gets only "not allowed".
  `users.links`/`users.unlink` still take a stale id that has a link, so it can be removed.
  (manual: integrations)
- Discord: a bot without the Message Content intent no longer stops after Discord closes the
  gateway with 4014. It reconnects without the intent and hears direct messages and mentions
  (`discord.status` shows `message_content: false`). In a server channel, `@Bot !link CODE`
  and `@Bot !help` work like the same commands sent in a direct message. (manual: integrations)

### Removed
- The blanket holds, with the advanced security plugin on or off: admin and destructive calls
  (`approvals.enabled`, `approvals.hold`, `ROOK_APPROVALS`), operator `hold` rules in the
  policy (now ignored, and the policy lint says so), workers on the hub's own machine
  (`approvals.hub_host_workers`) and the spread limit with its grants (`approvals.spread_*`,
  *Approve + spread*). The operator contact channels for each hold (`approvals.contact`,
  `contact_worker`, `voice_max_tier`, `notify_every_secs`: Telegram, notify, voice and the
  per-hold spoken question); `rook/hub/plugins/security/holds.py` and `contact.py` are
  deleted. (manual: accounts-and-access, security)

## [0.4.0] - 2026-10-25

The integration release: everything in flight merged to one master, one hub build, one
stable Android app (0.4.23, code 40) for every phone and tablet, and the first Wear OS app.
Since 0.3.0: the always-on house agent and its voice, plain-words decision cards that queue
and are cleared by voice, the Rook reset (four hard stops in core, change requests, the old
guardrails moved to the advanced security plugin, off), trust proposals as cards, and a
watch push-to-talk app.

### Added
- Hard stops (`rook/hub/hardstops.py`): only four things ask the person first: a change on a
  sacred machine, erasing data for good, revealing a secret's value, sending as the person.
  Everything else goes and is journaled. The blanket holds, guarded tier, spread limits and
  votes moved verbatim into the `security` hub plugin (`security.enabled`, off).
  (manual: security)
- Change requests: hub plugin `changes`, caps `change.*` on worker `rook`. Standard changes
  (routine deploys, reversible config) are checked, run, tested and rolled back by the hub; the
  runner waits for a step's worker after a hub restart and logs results in the home timezone.
  (manual: trust)
- Wear OS push-to-talk app (`android/wear`, `android/wearlink`): tap on the watch to talk; the
  phone app runs the watch's voice session over the Wear Data Layer. Ships as `rook-wear.apk`,
  same package and signer as the phone app. (manual: phones-and-tablets)
- Decision cards queue, and the phone reminds you out loud: cards are no longer emergencies, so
  they land quietly in the app (the digest channel: no pop-up, no takeover on unlock) and the hub
  asks the phone to say "Need your input: <title>" for one card or "Hey boss, you have 6
  decisions pending." for several, paced by `attention.remind_*` settings (wait 2 min before a new
  card, 15 min between reminders, again hourly, retry 5 min after a busy room, optional quiet
  hours). The phone speaks only after its own quiet check (`attention.remind`, app
  0.4.22-beta.3). Urgent alerts still ring at once. Pending trust proposals count as decisions.
  Every card, reminder and decision is one plain line in a 30-day log (`rook/hub/eventlog.py`,
  `attention.log`), reasons summarized, never content. (manual: phone-attention)
- Clear your cards by voice: "clear my cards" makes the house voice read each waiting card (held
  calls first, then oldest) and take a spoken yes / no / skip / explain. A spoken answer counts
  only from the owner recognised by a sure voiceprint match, from the heard transcript, in the
  normal mode, after the card was asked; it is relayed as `voice:house` ("approved by Bake via
  voice"), once the person ticks the voice's row on the Tokens page. Loosening the trust map
  (any proposal that isn't strictly stricter, sacred included) and sacred holds are read back word
  for word by the voice and need the person's next words to be "yes" plus a random check word;
  the words, voiceprint score and read-back go into the trust history (`Actor.evidence`).
  Dashboard-only cards are named and left. `trust.describe_change` writes the sentence.
  (manual: phone-attention)
- Approval asks in plain words: every ask that reaches the person (Approvals page, phone decision
  card, `rook_approvals`, huddle cards and questions, job, trust, hub-host and spread holds, house
  asks, the operator's Telegram/notify/voice messages) reads Title / For / Who / I want to / Why /
  Risk, built by one summariser (`rook/hub/plain.py`) with cap-specific phrasing (shell commands
  read as what they do and whether they only read). The technical detail (cap, args, trust
  entry, id and three-word name) is under Explain. `rook_call` takes `why=`; holds keep the
  caller's claimed task, its project, the console room, job or huddle. The Android decision card
  shows the body under the title (0.4.22); older apps get it folded into the line and Explain.
  (manual: phone-attention, security)
- Always-on house agent: with `home.persistent` the house agent runs as persistent Claude Code
  sessions on a session host (`python -m rook.agents`, on the machine with the Claude login): Opus
  in its role directory (`CLAUDE.md`, `.claude/settings.json`, `.mcp.json`, rendered by the hub)
  and Haiku as its voice in `voice/`. Sessions resume by id after restarts (a fresh start is
  reported, never silent), save memory then compact at a threshold, and sleep when idle if set.
  The hub owns a per-role inbox (voice, person, huddle, background; notes batched "While you were
  busy"); without a session host, turns fall back to one-shot runs. The voice conductor's flow
  tools: queue, steer, fork, fold, status, btw (a throwaway fork; Claude Code's `/btw` is not
  available headless), pause, resume, cancel. The house agent's `role_config` tool grants or
  revokes a role's MCP servers as trust-map `grant_tool` entries, only on the person's own
  request. Caps `home.sessions`, `home.session_ask`, `home.session_flow`, `home.session_note`.
  Design: docs/design/persistent-agents.md. (manual: persistent-agents, trust)
- Dashboard chat dock: once the house agent is set up, an **@home** button at the bottom right of
  every page opens the same chat as Manage > Home agent (the operator's 1:1 huddle). It stays open
  across page switches and reloads until closed, polls only while open, and steps aside on the
  Home agent page. (manual: home-agent)
- Settings > **Restart** (top right, operator only): the hub process exits with code 75 and its
  service manager starts it again, so no root is needed; with two processes the dashboard
  restarts after the MCP side. Refused, with the reason, unless systemd would restart it
  (`Restart=always`/`on-failure`) or `ROOK_RESTART_SUPERVISED=1`. The page reloads when the hub
  is back. (manual: settings)
- Spoken reminders on the phone (Android app 0.4.22-beta.3, code 29): `attention.remind {id,
  text, sample_ms=3000, speak_unchecked=false}` (admin, physical; only the hub calls it) returns
  at once, then the app skips the reminder in Do Not Disturb or bedtime, during a call, its own voice session, another app
  recording, media or speech playing, listens for `sample_ms` (1-8 s) and speaks `text` (at most
  300 characters) in the app's voice only when nobody is talking. Each one ends with a `reminded`
  event in `attention.poll` (outcome spoken, dnd, busy, call, voice_session, mic_in_use, media,
  speaking, unchecked, off or error, plus two rounded level numbers). The sound stays in memory on
  the phone: nothing is recorded, kept or sent. When wake-word standby is on, the check reads the
  frames standby already reads instead of opening a second recorder. The Attention tab has a
  **Spoken reminders** switch (on by default) and the microphone grant.
  (manual: phone-attention)

### Changed
- Huddle note cards never wait on the person: a note runs nothing, so it is recorded as noted when
  proposed (migration `004_notes_never_wait` settles the old waiting ones). `home.session_note`
  (a background note into a persistent role's inbox) is a "prepare" action in the trust map, free
  on any level, like `home.ask`.
- A task card whose `attrs.criteria` (or tags, workers, dependencies) is one string is fixed when
  proposed; anything else that is not a list of strings is refused then, not when the person taps
  Yes. (manual: huddles)

- A decision card is the one decision record: decided anywhere (phone, voice, dashboard, an
  agent relaying the person's quoted words) clears it everywhere; a moot card is gone. Trust
  proposals are cards too, and only the person decides them. Waiting cards post as a normal
  notification and come back on unlock (app 0.4.22-beta.6).
- Phone decision cards: swipe between them, answers apply at once (sent in the background, put
  back with a short error if the hub refuses), short titles, and More details shows the full why,
  target, who asked and the undo.
- Voice: a named speaker match counts as the owner when more than half sure
  (`VOICE_SPEAKER_SURE` 0.45).

### Fixed
- First install no longer points at localhost: `rook worker` without `--hub`, `ROOK_HUB`, a
  pairing or a pushed config used to dial 127.0.0.1:7474, and `rook` (the terminal dashboard)
  http://127.0.0.1:7005. The worker now uses the hub of its `--ws-url`, else a hub running on
  the same machine (one answering /health on 127.0.0.1:7005), else exits saying how to pair
  (`rook pair <code> --hub http://<hub address>:7005`). The dashboard uses the hub this
  machine paired with, else a local hub, else asks for the address. The "using hub ... from
  saved state, not the --hub/--psk given" warning no longer fires when none were given.
- Phone cards are only sent in kinds the app takes (trust proposals and house asks were never
  posted).
- House voice: a new person's "I agree to have my voice remembered" comes marked "unknown guest"
  (their voice isn't known yet) and is now taken as their consent, so enrolling someone by
  conversation routes `enroll` instead of asking "is that Quinn?" forever; the hub still checks
  the voice isn't the owner's. "That's all" ends listening with minutes 0: minutes (which mute
  the wake word) only when a quiet spell is asked for. (manual: persistent-agents)
- Enrolling a voice (review of the fix above, which made the hub's checks the only gate):
  the consenting voice must be unknown to speaker ID or already known as that person, so
  someone else it knows can't agree for them and a turn with no voiceprint never counts;
  a "no", a question or a hedge around "I agree ... voice" isn't consent; one yes enrolls
  one person; the owner calling them a son, daughter or kid (or an earlier record saying
  so) makes it the child path whatever the voice routed; the owner's request must say the
  name as a word. Quiet minutes on `listen_end` are kept only when the words heard asked
  for minutes or hours. (manual: persistent-agents)

## [0.3.0] - 2026-10-23

The house agent release: about 180 builds since 0.2.0. The house agent as the front
door, trust map, huddles with the conference table and votes, role agency, jobs, beta
app channel, attention on the phone. A fresh hub now serves the Android app from the
first start.

### Added
- Hub releases carry what the hub serves besides its code. `rook hub release build` puts the
  worker bundle (on a live hub the one it serves, so a hub deploy never changes what new workers
  get; `--worker-bundle build` builds it from the same commit in a throwaway worktree, signed with
  the same key; also `DIR|none`), the general APK and the beta channel's APK (`--apk`,
  `--apk-beta`: by default the ones the live hub serves and approves) into the signed manifest
  (`feed`, path + sha256 + size per file), stored once by content beside the tarball.
  `rook hub deploy` fetches and checks each file and links it into the release, carries any kind
  the release lacks from the running release (`--no-carry-feed`), refuses a release whose APK
  `ROOK_PUBLIC_APK_SHA256` or whose beta `ROOK_BETA_APK_SHA256` would not approve (the gates are
  never changed), and after the restart checks `/band-worker.json`, `/band-worker.pyz`, `/apk.json`,
  `/apk` and `/apk.json?channel=beta`, rolling back when one went dark (`--skip-feed-check`).
  Deploy config keys `feed_service`, `feed_url`, `feed_check`. Docs: docs/operations/hub-deploy.md.
  (manual: running-a-hub)
- A fresh hub serves the Android app from the first start: `install-hub.sh` fetches the release's
  generic APK (checked against its SHA256SUMS and its manifest) into `DATA_DIR/updates/apk`, the
  place Manage > Updates publishes to (`--no-apk` to skip; with `--git-ref` the latest release's).
  A published release APK is served at `/apk` without the dashboard login; an operator's copy in
  `rook/remote` still needs `ROOK_PUBLIC_APK_SHA256`. The beta channel can also come from the
  release (`rook/remote/apk-beta`) when the gate approves it. (manual: phones-and-tablets,
  running-a-hub)
- Clean-room harness: `--release latest|TAG` installs a published release the way the README
  says, and the phone installs the APK it downloads from the fresh hub's `/apk`. (manual: n/a)

### Changed
- The Android app's default version is 0.4.20 (code 25), the next stable after the 0.4.20-beta.1
  (24) beta.
- The conference table and votes in chaired huddles. The chair pins artifacts (a pull request,
  commit, file, document, test output, screenshot or preview link) at an exact version (commit
  SHA or content hash), and every role reads them through the hub at that version (`table_read`:
  summary, diff, files, file, checks, comments), not its own checkout. The chair calls a vote on
  merge, ready for QA, deploy or ship; the trust map decides whether one is needed (ship-it may
  skip, sacred always asks you). Gates run first and are never voted on (still at the pinned
  commit, merges cleanly, CI green, a test run passed, no secret in the diff, rollback dry run
  for deploys). The required roles (data: merge = lead, QA, security, critic) vote blind, each
  ballot sealed with a posted commitment until all are in and bound to the commit (a new commit
  voids it); couldn't verify counts as no, a yes needs evidence and a read of the pinned version,
  authors never vote (a stand-in takes the seat). Any no blocks; the chair gets one re-vote,
  then you get one Yes / No / Explain line with each side. A passed vote is carried out by the
  hub with a vault credential sessions never see (GitHub merges only at the voted SHA), recorded
  with the vote, versions and gates, and listed in the house agent's digest. Merges and ready
  for QA go on their own after a pass; deploys and ships still ask you (`huddle.vote_auto`). New
  caps `huddle.table`, `huddle.pin`, `huddle.vote`; `rook_huddle` actions `table`, `pin`,
  `vote`; settings `huddle.votes`, `huddle.vote_voters`, `huddle.vote_alternates`,
  `huddle.vote_auto`, `huddle.vote_github_secret`, `huddle.vote_merge_method`,
  `huddle.vote_require_ci`, `huddle.vote_test_checks`, `huddle.vote_gate_wait_minutes`,
  `huddle.vote_deploy_targets`. (manual: conference-table, huddles)
- Starting a project by talking to the house agent, and the borrowed-state ledger (with the
  front door on). While you brainstorm it keeps running notes; when the talk turns to building
  it asks once "Sounds like this wants to be a project. Want me to set it up?", then asks where
  it lives, how protected it is and what it touches, recording each answer in your words (it
  never fills one in), reads the plan back, and on your yes creates the folder or local git repo,
  a project record, a wiki page from its notes, a trust entry with your words (guarded and sacred
  at once; ship-it waits for your tap on the Trust page) and a starter team. A toy on a stricter
  host (kaiju) gets a proposed scoped exception naming only that host's recorded resources (new
  trust map `baseline` and `resource:` selector: only the house agent's own recorded stop or
  restore call for it matches). Borrowing ("shut down Strata on GPU 1 for it") is recorded as a
  lease with what changed, why, your words and how to restore it; "bring Strata back" restores
  it; the house agent offers once to restore when the project finishes, goes quiet or its
  exception ends, and asks "Still a toy?" on the review date. Decisions page: **Borrowed now**
  and **New projects**; Trust page: the ledger with your words and **Normal state**. New caps
  `home.ledger`, `home.projects`, `home.restore`; `rook_house` actions `ledger`, `restore`,
  `projects`; settings `home.onboard_review_days`, `home.onboard_quiet_days`,
  `home.onboard_concept`, `home.onboard_sweep_minutes`. (manual: onboarding, trust)
- The house agent as front door (`home.front_door`, off by default). Every request that reaches
  it (the phone's voice background through `home.ask`, the web chat panel, chat rooms and bridged
  Telegram, MCP clients, huddles it chairs) is routed one way and the reply says which: it
  answers; it does it (quick, reversible, and `go` on the trust map; "Done:" is written by the
  hub only when the action ran); it starts a coding session for the lead developer and tracks it,
  or opens a huddle ("I've got the team on it: <name>"); or it asks you one line with Yes, No and
  Explain, listed with the relayed approvals (kind `house`) so the phone's decision card and any
  client can answer, then acts and offers to remember the answer. Disputes between agents are
  ruled from the trust map or the playbook with a citation (`home.settle`, the chair's `settle`),
  and asked otherwise. A playbook file in the house agent's home holds its routing rules
  (corrections tighten at once, loosening is asked). The dashboard's **Decisions** page lists its
  asks, what it decided for you with the entry each relied on and a one-tap Overturn (which only
  tightens), and the playbook editor. New caps `home.digest`, `home.explain`, `home.overturn`,
  `home.playbook`, `home.playbook_edit`, `home.settle`; MCP tool `rook_house`; settings
  `home.front_door`, `home.front_dispatch_role`, `home.ask_ttl_hours`, `home.offer_remember`,
  `home.front_max_calls`. (manual: front-door, home-agent)
- Decisions and alerts on your phone. The new hub plugin **attention** puts everything waiting
  on you (held calls, held job calls, huddle cards, pinned questions) on the phones named in
  `attention.devices` as decision cards with Yes, No and Explain, and withdraws a card once it
  is decided elsewhere; dashboard-only items get Explain only. The hub pulls taps and read
  signals from the phone (`attention.poll`; the phone never calls the hub). A tap counts only
  after you tick "May relay my approvals" for the phone on the Tokens page (listed there, off
  by default) and is recorded as "approved by <attention.person> via <phone> (phone)". New
  caps `attention.send` (alerts, queued while the phone is offline), `attention.status`
  (whether you saw, opened or unlocked it) and `attention.notices` (short summaries of urgent
  and family notifications; full notification text never leaves the phone). Settings
  `attention.devices`, `attention.person`, `attention.poll_secs`, `attention.push_rules`.
  Inert while `attention.devices` is empty. (manual: phone-attention)
- Beta channel for the Android app: `/apk.json?channel=beta` and `/apk?channel=beta` serve the
  APK in `<updates dir>/apk-beta/` while `updates.apk_beta_sha256` (`ROOK_BETA_APK_SHA256`)
  approves it and it is newer than the published APK; every other request, including every
  installed phone's plain `/apk.json`, gets exactly the stable answer, and the hub never
  pushes a beta. (manual: updates)
- Roles act inside their own space. Every role has a **workspace** (`workspace/` in its home) for
  drafts, notes and working files, which it reads, writes, searches and deletes with no card
  (Claude Code's own file tools where the agent host supports it, else hub tools for every
  harness), shown read-only on the role's page. An interim **action tier**, kept by the hub per
  cap whatever the policy mode: reads in scope and the workspace always run; a role with the new
  autonomy `routine` (the architect and lead presets) starts a Claude Code session on its own
  branch in a code location (`start_session`), which may open a pull request and never merge;
  everything else (merges, deploys, deletes and writes outside the workspace, settings, jobs,
  protected devices, unknown caps) becomes an action card by itself, and secrets always do.
  Sessions **report back** on their own: progress in the huddle, then DONE, BLOCKED or anything
  unexpected wakes the role through the chair, with no watcher job. New caps `role.workspace`,
  `role.sessions`, `role.session_start`, `role.session_send`, `role.session_stop`; card kind
  `call`; settings `role.protected_workers`, `role.session_model`, `role.session_stall_minutes`,
  `role.session_max_minutes`, `role.sessions_per_role`. (manual: role-agency, huddles)
- Voice host for the dashboard. With `voice.engine = remote` the hub reads huddle messages
  aloud through the voice server even on a two-process hub (it then mounts no voice paths;
  phones keep the server's own address). Voice pickers with a Preview button on every
  role (now in the main form, no longer folded under Model), on the house agent (new
  `home.voice`, Manage > Home agent), and for the band default (`voice.tts_default`, now
  labelled *Band default voice*, on Manage > Voice and in Settings). Voices are listed
  Chatterbox first, then Kokoro. Spoken audio is kept per (voice, text) up to
  `voice.speech_cache_mb` (32 MB), so replays start at once, and a busy voice server is
  retried. Play on a device sends the house agent's voice too. (manual: voice)
- Long huddle messages are read in full. The hub cuts a message into chunks of whole
  sentences (at most 280 characters, the first shorter): Chatterbox stops after about
  32 seconds of audio per request and dropped the rest of the old 600-character parts.
  The page asks for the plan (`speak` with `plan`), plays the first chunk as soon as it
  arrives with the next two loading, plays them without gaps (Web Audio), keeps played
  chunks per (voice, text) for instant replays, retries a failed chunk once and then
  skips it with a short note, and stops pending requests on Stop; a thin line under the
  button shows progress. Up to 10,000 characters are read (was 4,000). The hub sends its
  voice server one line at a time instead of drawing "busy" answers. Play on a device
  sends the chunks in order (the device queues them). The Android app cuts its own lines
  at 280 characters too (next app release). A voice picker that got no voices says why and
  the next one asks again, instead of showing only *Band default*. (manual: voice)
- Relayed approvals. One list of everything waiting on a person: held calls, held job
  calls, huddle action cards (every huddle) and pinned huddle questions, each with a
  three-word name, who asked, what exactly would happen, the risk tier, where it came from
  and its age (`approvals.list`, MCP `rook_approvals` `summary`/`list`, with a line short
  enough to speak). A client the person enables on the Tokens page (*Relays*, "May relay
  my approvals"; off for everyone, only a person signed in to the dashboard turns it on)
  may approve or deny an item with `relayed_by {who, quote, session}`: the person's own
  words. Secrets and the vault, tokens, policy, settings, approvals and job locks are never
  relayed, nor anything on the new setting `approvals.relay_dashboard_only` (conservative
  by default: admin tier, commands on the hub's machine, physical actions, worker and band
  changes, plugins, custom commands, roles, deleting jobs). Every relayed decision is kept
  append-only with the relaying identity, the person, the quote and the session, and shows
  beside the hold, card or answer ("approved by Bake via Claude web: ..."). A deny's reason
  goes back to the requester: a denied call's reply carries it (and an identical repeat
  gets the denial at once), a dismissed card's proposer is told in the room and the chair
  sees it. The huddle chair answers "what's waiting?" from the list and, if the house agent
  is enabled, relays a decision the person states in their message, quoting it. Holds and
  huddle questions now have speakable names. Permissions 3.12. (manual:
  accounts-and-access, huddles, agents-and-mcp, security)
- Device workers. A worker can be the *delegate* for things that run no worker of their
  own, such as TVs and later light switches and thermostats. Each one joins the band as a
  worker in its own right, with its own name, caps and live heartbeat, plus `delegate`
  (the worker answering for it) and `device` (`type`, `driver`, `model`). Agents call it by
  name (`rook_call(worker="office-tv", cap="tv.toast")`), `rook_workers` shows its state
  in `hb`, and the Workers page shows it with a **via** tag and a status in place of the
  battery. Devices stay out of updates, deauth, settings, ccmod and hygiene, and don't
  count toward `rook_caps` `"*"`. A delegate lists its devices on its own announce so the
  hub drops a removed one at once, and calls to a device are addressed to the delegate's
  route. Core spec 14.6. (manual: devices, workers)
- `webos` worker plugin: LG webOS TVs as device workers (`WEBOS_TVS="office-tv=192.0.2.10"`).
  Each TV keeps live state over the TV's control API (on/off, app or input, volume) and
  offers `tv.status`, `tv.apps`, `tv.inputs`, `tv.toast`, `tv.launch`, `tv.input`,
  `tv.volume`, `tv.mute`, `tv.media`, `tv.button`, `tv.open_url`, `tv.screen`,
  `tv.power_off`, `tv.power_on` (Wake-on-LAN) and `tv.pair`; the delegate adds
  `webos.list` and `webos.discover`. (manual: devices)
- Jobs end themselves. A step's `complete` (`success`, `failure`, `finish`) or an agent step's
  `finish(complete=true)` switches the job off for good when the run ends, and the hub does it,
  so the agent needs no `job.write` and no tool calls left. New `limits`: `complete_on`,
  `max_runs`, `until` (an expiry time) and `max_failures` (failed runs in a row; hub setting
  `job.max_failures`, default 10). A job that switches off shows `paused_reason` `completed`,
  `max_runs`, `expired` or `max_failures`, its waiting runs are cancelled, and its history
  names the rule. Repeated messages are dropped: a job sends the same voice, notification or
  Telegram text to the same target at most once per `limits.notify_window` (hub setting
  `job.notify_window`, default 6h). This applies to notify and ask steps, alerts, cap and
  fanout steps, and the agent's own sends. Texts are compared without case, punctuation or
  numbers. (manual: jobs)
- Locked jobs. A person can lock a job (🔒 in the Jobs page's ⋯ menus, or `lock` / `unlock`
  in the `rook band` jobs panel). While it is locked, agents and every API token, the
  operator token included, cannot edit, enable, disable, delete it or cancel its runs; they
  get a `locked:` error. Its runs cannot change it either. Running it stays open. Only a
  person signed in to the dashboard may lock or unlock, and each change is a history row.
  A locked job keeps its own limits; the hub's default `max_failures` doesn't apply to it, and
  an agent can complete it only from a step with `complete: "agent"`. (manual: jobs)
- Speak button on huddle messages. Every message and action card in a huddle (Huddles and
  the Work huddle board) has a compact Speak button: the hub reads the message aloud in the
  browser, in the speaking role's voice (new role field `voice`, picked on the Roles page from
  the voice engine's voices), else the band's default voice, with markdown, code blocks and
  links left out and record ids read by their three-word names. With no voice engine the hub
  can reach, the browser's own speech reads it and the tooltip says so. Tap again to stop; one
  message at a time. A long press offers "Play on <device>" (`voice.speak`) for devices that
  are not protected (`role.protected_workers`), narrowed by the new setting
  `huddle.speak_devices`. (manual: voice)

### Changed
- The dashboard works on a phone. At 800px wide or less the sidebar becomes a drawer (☰, or
  More in a new bottom bar with Home, Huddles, Work and Sessions). The header is compact and
  stays at the top. Inputs are 16px so iOS does not zoom, tap targets are at least 44px, and
  safe-area insets are respected. Tables stack into labelled rows, every dialog opens as a
  full-screen sheet, and long ids and paths are shortened with "…" and copy when tapped. Each
  page's description moved behind an i next to its title. Pages were condensed on every size:
  secondary actions sit in ⋯ menus, there is one primary button per view, rows of buttons
  became segmented controls, and labels, empty states and toasts are shorter. Explanations
  moved behind i toggles, and the manual-update notice is one line. A Playwright test
  (tests/test_mobile_layout.py) checks every page at 390px for sideways scrolling; it skips
  where Playwright is not installed. (manual:
  overview-and-start-page, workers, work, knowledge, huddles, home-agent, jobs, sessions,
  updates, bands-and-keys, help)

### Added
- Task deck paging: `rook_task`/`task.read` deck takes `project_fields` (trim project
  headers), `project` (one project) and `offset`/`limit` over projects. Every deck reply
  starts with `total_projects`, `total_tasks`, `loose_tasks`, the filters applied and
  `next_offset`; tasks under no project are listed as `loose`. A plain deck read returns the
  same `deck` as before. The PM preset reads the deck page by page. (manual: work)
- Huddle roles are told where code lives: the `role.code_locations` setting (one line per
  repository) is in every role turn, and a session card must start in one of those folders.
  Per-role `max_tool_calls` (engineering presets 30, operations 24, others the setting), with
  the remaining budget shown on a role's last tool results. (manual: huddles)
- Huddle role presets by category: 25 starting roles in nine categories (product and
  planning, software engineering, operations, hardware and fabrication, writing, research,
  media and promotion, critique, home and family), each with a charter (what it owns, what
  it never does, how it decides, when it asks a person), concerns, a checklist, example
  questions, starting memory, sources and a read-only tool scope. Categories and presets are
  data (one TOML file each under `rook/hub/plugins/roles/presets/`); a category can require
  every preset in it to only propose and add patterns its roles never call. Media and
  promotion roles draft and propose: nothing is posted, published or sent unless a person
  approves that card. `role.presets` returns the presets grouped by category (`id` for
  one whole preset, `category`, `full`). Roles gain `description`, `category`, `checklist`,
  `examples` and `seed_memory`; the chair routes on the description and examples, the
  checklist is in every turn, and the starting memory is saved into the role's memory when
  its home is created. **Manage > Roles** picks a preset from one dropdown grouped under
  category headings and shows the picked preset's one-line description. (manual: huddles)
- Huddle roles (beta, huddles H2): a `roles` hub plugin (`role.*` caps: store with history,
  presets for project manager, QA and lead developer, `role.ask`, `role.memory`,
  `role.move`). Each role has a home directory on its agent host holding its memory
  (`role:<id>`) and the Claude Code, Codex and OpenCode config that declares exactly its
  tools; every role turn runs there. Worker setting `agenthost.roles_dir`.
  (manual: home-agent)
- Chaired huddles (beta, huddles H3): `huddle.start`, `huddle.post`, `huddle.card`,
  `huddle.archive` and `huddle.list`, and the same actions on `rook_huddle`. The home agent
  chairs (call_on, answer, propose), called roles answer in parallel in their homes, the
  chair closes the round; `@role` asks one directly. Action cards are approved by people and
  run as the approver, or run directly by roles with autonomy `act`. Settings
  `huddle.reply_chars`, `huddle.max_role_turns`, `huddle.retention_days`,
  `huddle.default_roles`. (manual: huddles)
- Huddles on the dashboard (beta, huddles H4): the Chat tab is now **Huddles**: a list of
  huddles and the thread, role and chair markers, "answering" lines while a round runs,
  action cards inline with Approve and Dismiss, `@role` completion, new huddles with the
  default roles, archive, and the old chat rooms read-only under a filter (`#chat` links
  land there). **Manage > Roles** edits, moves and deletes roles, adds them from the
  presets and shows their history; the Memory page shows and edits a role's memory. The
  Home agent page's chat panel is the 1:1 huddle with the house agent, and the page shows
  the last 24 hours of agent host turns and tokens per role. Phone layout for all of it.
  (manual: huddles, home-agent, chat, work, desktop-app)
- Jobs and huddles (beta, huddles H5): agent steps take `agent: "role:<id>"` (a role works
  the step with its own sources, tools and memory), and a new `huddle` step kind holds one
  round and delivers the chair's synthesis with the cards waiting (Telegram, voice or
  notification). A role's sources are read at the hub before its huddle and job turns; the
  lead developer reads live sessions on every host and proposes `session.send` /
  `session.start` cards. (manual: jobs, huddles)

### Changed
- Huddles: the chair asks a failed role again (`huddle.role_retries`) and follows up with
  roles until the thread is settled or needs you (`huddle.max_followups`); a single settled
  answer gets no extra summary. Role answers are posted whole instead of being cut at
  `huddle.reply_chars` with "…". (manual: huddles)
- Huddle cards are checked when proposed (the worker has the cap; task cards run
  `task.write` with a parent project; a session card's task is a task id), a near-identical
  waiting card is revised instead of added again, the chair lists the cards it actually made,
  and a failed card shows its reason on the card, in the room and on the web.
  (manual: huddles)
- Every place that shortens text for an agent or a person marks the cut with
  `[truncated: N of M chars]` (role tool results, now 12000 characters
  (`role.tool_result_chars`) instead of a silent 6000, sources, chat messages, the agent
  host's final text, card results). (manual: huddles)
- New chat rooms are off by default (`chat.new_rooms`): `rook_chat_start` and `chat.write`
  start point agents at huddles, tasks and handoffs; existing rooms keep working.
  (manual: huddles)

### Security
- Failed sign-ins are throttled per client address: after 5 wrong passwords or tokens in
  10 minutes (dashboard login and Basic auth, account sign-in, MCP bearer tokens, the OAuth
  `/token` endpoint), the address gets 429 with `Retry-After` for 30 s, doubling up to
  15 min, without the credential being checked (`ROOK_LOGIN_*` settings). The dashboard
  password is now compared in constant time.
- Approval holds, on by default and independent of the policy mode: an agent's or
  integration's admin-tier or destructive call (worker updates and config pushes, secret,
  settings and policy writes, closing a console, deleting chat) waits up to 45 s
  (`approvals.wait_secs`) for a person to approve it. Not answered in time, the agent gets
  `{"ok": false, "held": true, "hold": "<id>"}` and is told to ask the user and repeat the
  same call; one approval covers one identical call (same cap, worker and args) for 10
  minutes. A deny always wins; a policy `allow` rule at least as specific as the built-in
  hold is an exception; people (`human:*`) and hub code are never held. Settings
  `core.approvals.*`, `ROOK_APPROVALS=0` turns it off.
- Spread limit: an agent's 6th distinct machine within 10 minutes for exec, admin or
  destructive calls is held (`approvals.spread_max`, `approvals.spread_window_secs`). When
  approving you can let that agent reach more machines for 15 minutes.
- No way around a hold: hub tools on `rook`, hub plugin tools, consoles, chat wake,
  `rook_config_apply`, integration commands and job steps all wait the same way, and a job
  an agent sets up to run as someone else keeps the agent in its on-behalf-of chain.
  Worker-to-worker calls do not pass the hub (known limit).
- A sign-in made with a password that is stored as a band secret can't approve held calls:
  an agent could use that password too (vault substitution, `secret.use`). Checked at
  sign-in against the vault over a new internal `POST /internal/is-secret` endpoint (returns
  only a boolean), or directly in `rook hub serve`; the session is flagged and the Approvals
  page explains why the buttons are disabled. Fails closed if the vault can't be reached.
- Built-in holds now also cover exec-tier and admin calls to a worker that runs on the hub's
  own machine (it could edit `approvals.db`, `policy.json` and settings directly). Detected
  by a hashed machine-id fact workers announce (`approvals.hub_host_workers`: `auto`, a list
  of names, or `off`); an operator allow rule naming the worker is an exception.
- An agent tidying up after itself is not held: signalling or closing a console it opened, or
  deleting a chat room it started with no people in it. Operator hold rules and the spread limit still apply;
  `rook_secret` get/set, memory forget and `updates.check` stay held.

### Added
Manual: accounts-and-access, security, agents-and-mcp
- **Approvals** page on the dashboard (Workspace), with a count in the sidebar: pending
  holds (who, call, worker, arguments with secrets hidden, why, how long) with Approve /
  Deny, recent decisions and active spread grants. API: `GET /api/approvals`,
  `POST /api/approvals/<id>`; only a signed-in band owner or admin can decide.
- Policy rules and default cells can say `hold` (`{"who": "role:agent", "hold": "shell.*",
  "on": "nas"}`).
- The hub tells the operator about held calls (`approvals.contact`, tried in order):
  `telegram` (the Telegram/Discord integrations' `notify.send`, the default), `notify` or
  `voice` on `approvals.contact_worker`, at most one message a minute, never with arguments.
- Opt-in voice approval (`ask`): the phone asks "Rook: claude code wants to run worker
  update on nas. Approve?"; a clear yes approves that one call, a clear no denies it,
  anything else leaves it on the dashboard and tries the next channel.
  `approvals.voice_max_tier` limits it; admin calls to the hub itself always need the
  dashboard.
- Agent instructions and the Rook skill explain holds: tell the user, retry the same call
  after approval, never work around a hold.
- The Approvals page shows the workers treated as running on the hub's machine, and warns
  when the sign-in can't approve because its password is a band secret.
- A hashed `host_id` fact (`rook/core/facts.py`): a stable per-machine id the hub and workers
  announce, so the hub can tell which workers share its machine. The raw machine id never
  rides the band.
- Android app 0.4.19 (23): `ui.nodes`, `ui.click`, `ui.scroll` and `ui.back` find and
  drive what's on screen by label, id or description (works in apps that block
  screenshots). `device.keep_awake`, `device.keep_awake_status` and
  `device.keep_awake_settings` keep the screen on; with **Keep phone awake while agent is
  working** (app Settings, on by default) any `ui.*`, `hid.*`, `screenshot.*`,
  `device.launch` or `device.open_url` call wakes the phone and holds it awake until 10
  idle minutes pass. An unlock PIN can be saved in the app (Android Keystore, never sent
  to the hub); `device.unlock` with no `pin` uses it, `device.unlock_status` says whether
  one is saved. All eight new caps are `exec` (`ui.nodes` sensitive, `device.keep_awake`
  physical), with hub-side floors in `authz.CORE`.

### Fixed
- The dashboard's cap buttons (and `rook band` calls) now run on the MCP side as the signed-in
  person (`POST /internal/band-call`, gated by the internal `mask.token`), like an agent's
  `rook_call`: the hub's own caps above `read` work from the dashboard of a two-process hub
  instead of failing with "not callable over the band", replies are masked and every call is
  journaled. Only a call that never reached the MCP side (no connection, or an MCP side without
  the route) falls back to the band, so a call can't run twice.
- `MultiBandClient.call` accepts the decision an integration command already made
  (`_decision`), so `/call` from Telegram or Discord no longer fails with a TypeError on a
  hub serving bands through the multi-band client.

### Dashboard
Manual: overview-and-start-page
- The sidebar scrolls when the window is too short for every entry (the
  footer with Sign out stays at the bottom), and the Workspace and Manage
  groups fold: their labels are buttons, each group's state is remembered in
  this browser, and a folded group still shows the current page's tab.

### Voice settings
Manual: voice
- Settings > Voice now covers what a GPU voice server is configured with,
  under the same variables the voice service reads: Chatterbox cloned voices
  (`VOICE_CHATTERBOX_DEVICE`, `_PYTHON`, `_VOICES_DIR`, `_START_TIMEOUT_S`),
  the starting voice and loudness (`VOICE_TTS_DEFAULT`,
  `VOICE_TTS_TARGET_DBFS`, `VOICE_TTS_THREADS`), Home Assistant
  (`VOICE_HASS_URL`, `VOICE_HASS_TOKEN`, `VOICE_HASS_VERIFY_TLS`), music
  (`VOICE_PIANOBAR_WORKER`), location and weather (`VOICE_HOME_LAT`,
  `VOICE_HOME_LON`, `VOICE_TZ`, `VOICE_WEATHER_UNITS`, `VOICE_WEATHER_URL`),
  the Front/Background and hosted models (`VOICE_FRONT_*`,
  `VOICE_BACKGROUND_*`, `LLM_PRIMARY_URL`, `_MODEL`, `_API_KEY`,
  `_TIMEOUT_S`) and the agent's limits (`VOICE_TOOL_*`). Tokens and keys go
  to the vault. Defaults are the service's own, and they apply when speech
  runs on the hub (`voice.engine=here`); a remote voice server keeps its own
  environment. Docs: docs/operations/voice.md.
- A voice setting given in the hub's environment as `ROOK_VOICE_<NAME>`
  (e.g. `ROOK_VOICE_HASS_URL`) now reaches the voice service on the hub
  under the variable it reads (`VOICE_HASS_URL`); before, Settings showed it
  locked but the service never saw it. The service's own variable, when
  set, still wins. `voice.llm_model` shows the service's default model id.

### Added
Manual: updates
- Manage > Updates can prepare a hub update for a `rook hub` install whose
  system units run as an unprivileged user (the hub cannot switch those
  itself). **Prepare the hub update to vX** (`updates.prepare_hub`, operator
  only) runs the same `SHA256SUMS`, provenance and breaking-update checks as
  an update the hub runs itself (a breaking update needs the acknowledgement
  first), leaves a signed `rook hub` release in
  `$ROOK_UPDATES_DIR/hub/<version>/`, records a `prepared` history event and
  shows the exact `sudo ... rook hub --config ... deploy <manifest> --yes`
  command to copy and run on the hub machine. History records the update
  once the hub runs the new release. The hub gains no new privileges: no
  sudoers rule, root helper unit or polkit rule. Detecting a `rook hub`
  install needs `ROOK_RELEASE` (set by the release drop-in) and the deploy
  config (`/etc/rook/hub-deploy.json` or `ROOK_HUB_DEPLOY_CONFIG`), readable
  by the hub's user. Docs: docs/operations/updates.md.

## [0.2.0] - 2026-10-14

The first tagged release. Preparation for public use.

### Voice and model provider plugins
Manual: voice, model-providers, home-agent
- Voice is a hub plugin: `rook hub serve` serves the voice service at
  `/voice/` (`/voice/ws`, `/voice/api/transcribe`, `/voice/api/voice`, the
  browser client, voice keys at `/voice/admin`; `/api/transcribe`,
  `/api/voice` and `/voices` at the root too, for the Android app). Speech
  runs on the hub (`voice.engine=here`, needs `pip install 'rook[voice]'`
  and the model files) or on another machine the hub forwards to
  (`voice.engine=remote`, `voice.remote_url`). Without what that needs the
  plugin stays off and the Plugins page says what is missing. The voice
  settings keep their keys and variables. The standalone server
  (`python -m services.voice.server`) is unchanged. Docs:
  docs/operations/voice.md.
- Voice on the hub only loads under `rook hub serve`; in the two-process
  layout the Plugins and Voice pages say so. With `voice.engine=remote` the
  hub forwards only the voice admin cookie (never the dashboard session or
  `Cf-Access-*` headers), passes back only that cookie under `/voice/admin`,
  keeps the voice server's own `Host` and presents the hub's origin as the
  voice server's so voice admin sign-in works through the hub (with failed
  sign-ins counted per client),
  refuses paths with `.`/`..` segments and caps voice WebSockets at 16 per
  client address. Changing a voice setting while the models load waits for
  that load instead of running two. Dashboard routes always win over plugin
  paths. Addresses with a user name or password are refused for the voice
  server, model providers and llmanifold; `llm.models`/`llm.check` are
  attributed to and rate limited per caller.
- Manage > Voice: state, the address for the apps, where speech runs, and a
  microphone check that transcribes a few seconds recorded in the browser.
- Model providers (`llm` hub plugin, Manage > Model providers): name the
  OpenAI-compatible model servers the hub can use, check and list their
  models; `llm.providers`, `llm.models`, `llm.check` on worker `rook`. The
  home agent can use one by name (`home.llm_provider`).
- llmanifold hub plugin: once `llmanifold.base_url` is set, llmanifold is the
  model provider `llmanifold`, and `llmanifold.status` reports its flows and,
  with its admin address, lanes and queue.
- Plugins: a plugin that cannot load says why in plain words
  (`Plugin.unavailable_reason()`), and is retried when one of its settings
  changes. Hub plugins can serve paths on the hub site (`rook/hub/site.py`).

### Worker CLI and onboarding
Manual: worker-commands, adding-machines, terminal-and-commands, workers, getting-started, accounts-and-access, running-a-hub, agents-and-mcp, chat, work, knowledge
- `rook` on a worker machine gained commands for the person sitting at it:
  `status`, `doctor` (plain-language fixes; exit 1 on a problem), `ask` and
  `chat` (the home agent, through an API token saved with `rook login
  --token` and the hub's MCP endpoint; reads also work through the saved
  dashboard login or the worker's band link), `tasks`, `rooms`, `knowledge search|get`, `plugins
  list|enable|disable` (the hub's protected plugins stay on), `logs [-f]
  [--audit]`, `pair <code>`, `login` and `completion bash|zsh|fish|powershell`.
  Both the bundle's launcher and the package's `rook` have them. See
  docs/operations/worker-cli.md.
- The worker serves a local control link for them on 127.0.0.1 (token in
  `<state dir>/control.json`, mode 600; `ROOK_CONTROL=0` turns it off) and
  keeps its recent log lines in memory for `rook logs`.
- Enrollment prints numbered steps and "Done: <worker> joined <band>", prompts
  only at a terminal (`--yes` / `ROOK_YES=1`, `--band` / `ROOK_BAND`), sends
  the worker's `--name`, and reports failures in one plain sentence.
- Worker installers: plain step names, details in `install.log`, "Done:
  <worker> joined <band>"; unattended runs (piped, cloud-init, systemd, CI,
  `--yes`) never prompt and get a user service; keyboard/mouse setup is
  skipped on machines without a screen (`ROOK_HID=0|1`); Windows installs a
  missing Python 3.12 from python.org for the current user after checking a
  pinned SHA-256 (then `winget --scope user`, then one clear instruction).
  The `/install` and `/hub` installers no longer die under cloud-init at a
  prompt.
- Run as root (cloud-init, `sudo`), the Linux worker installer stops unless
  told whose worker it is: `ROOK_USER=<account>` installs it for that account
  (it runs as them; a worker installed for root earlier is then stopped and
  disabled once the new one is in), `ROOK_ALLOW_ROOT=1` runs it as root
  (updating such a root worker in place). Root writes only to its own home,
  even under `sudo -E`. The Windows
  installer refuses to run as SYSTEM (the worker must run in the user's
  session). Worker names are limited to letters, digits, `.`, `_` and `-`.
- `rook login` defaults a bare dotted host to `https://` (https against a
  plain-http hub suggests the `http://` command), asks before sending a token
  or password over plain http to a public address (`ROOK_ALLOW_HTTP=1` without
  a terminal; a written-out `http://` name that resolves only to private
  addresses, like Tailscale's, isn't asked about), and never follows
  a redirect from the MCP endpoint (the token would go along). The control
  link drops connections that don't send their token within 2 seconds, and
  `control.json` and the saved token get an owner-only ACL on Windows.
- A password-only hub's sign-in page asks for the password alone. The setup
  wizard and install-hub.sh's last screen use plain words; the wizard fills
  in its own address.

### User manual
Manual: help, welcome, getting-started, overview-and-start-page, adding-machines, bands-and-keys, accounts-and-access, workers, chat, work, sessions, knowledge, jobs, secrets, agents-and-mcp, home-agent, home-agent-memory, settings, plugins, phones-and-tablets, hardware, integrations, voice, updates, security, terminal-and-commands, running-a-hub, desktop-app
- Added a user manual: 27 plain-language pages in `docs/manual/` covering
  every current feature (and a preview page for the desktop app), shipped in
  the wheel (`rook/_manual`) and the image. **Help** in the dashboard sidebar
  reads it (search, page links): the operator from the knowledge base copy
  when there is one, everyone else from the bundled files. New
  hub plugin `manual` with caps `manual.status`, `manual.list`, `manual.get`
  (read) and `manual.update` (admin), and settings `manual.auto_seed` and
  `manual.prompt_updates`.
- A new hub copies the manual into its knowledge base the first time the
  knowledge plugin is on and empty: contents page `rook-manual` (never
  someone else's page with that slug: then `rook-manual-2`), pages
  `manual-<id>`, tagged `rook-manual` and `rook-<version>`, verified against
  the bundled file (`attrs.verified_from`: the knowledge store marks such a
  page unverified when anyone else changes its text). A knowledge base that
  already has pages is asked first.
- After an update whose manual differs from the copy, the operator sees "The
  Rook manual changed in this update: update your knowledge base copy?" with
  the pages added, changed and removed: Update, Not now (24 hours) or Never
  for this version. Pages edited in the knowledge base (text or tags, the
  contents page too) are only replaced when ticked, and their own tags are
  kept; removed pages are archived. The `rook_knowledge` tool's description
  tells agents where the manual is (an empty plugin guidance default no
  longer hides another plugin's text for the same slot).
- `tools/manual_check.py` reports a FEATURES.md section, settings group,
  plugin or command with no manual page, a page mapping a feature that no
  longer exists without being deprecated, and an Unreleased bullet under an
  Added/Removed/Deprecated heading that names no manual page (`--base REF`:
  or names pages that did not change). Blocking only as the release gate:
  `tools/release_prep.py --tag` runs it with `--release` (and writes
  `docs/manual/manifest.json` for the build; `--skip-manual-check` for
  emergencies). CI runs it as an advisory job; pytest checks only that pages
  parse and their links resolve. CONTRIBUTING has the rule. The split-layout
  hub installer sets `ROOK_MANUAL_ADMIN_URL`.
- The Knowledge page renders numbered lists and wrapped list items.

### Home agent memory
- New hub plugin `memory` (on by default, part of the "Home & voice agent"
  preset): the home agent remembers people, facts, conversations and skills
  between conversations, Hermes-agent style. It recalls a budgeted memory
  section before each chat reply, `home.ask` and job step, and keeps what it
  learned afterwards ("my name is", "I prefer", "remember that", "forget
  that"); job steps it finishes with tools become skills. Setting
  `home.memory` turns it off for the home agent; with `home.tools` on the
  model also gets `memory_save` / `memory_search`.
- Write rules: no secrets (vault values become `{{secret:name}}`
  references; key, password, access-code and card shapes are refused or
  redacted; `memory.deny_patterns` adds your own), no prompt-injection text,
  dedupe, and a changed fact supersedes the old one instead of editing it.
- Recall: keywords (FTS5) always, plus embeddings from `memory.embedder`, the
  home agent's endpoint (`memory.embed_model`) or any worker's `embed.text`,
  falling back to keywords when none answers.
- A consolidation job (every 30 minutes; `memory.consolidate`) summarises
  finished conversations, rolls old ones up by week, merges duplicates,
  fades unused memories and enforces budgets.
- Privacy: agents, `home.ask` and job steps see shared memory only; another
  person's memory, forgotten rows, procedures, pinning and approval are for
  the operator. Learned skills keep no tool output and need the operator's
  approval before any prompt uses them. Purge erases every earlier version
  and leaves no copy in the keyword index or the WAL file;
  replaced and forgotten rows expire after `memory.retention_days` (90).
  `rook_home_ask` is now authorized as `home.ask`.
- Manage > Memory: browse, search, correct, keep, approve, forget and erase memories,
  with provenance and history. Caps `memory.recall/show/list/status`
  (read, sensitive) and `memory.remember/edit/consolidate/forget` (write);
  MCP tool `rook_memory` only with `memory.agent_tool` on.

### Overview, start page and themes
- The dashboard opens on a new **Overview** after sign-in: tiles for workers,
  sessions, open tasks and unread chat; Waiting on you (sessions idle for
  input, unread rooms, blocked tasks, hygiene decisions); Active now; open
  tasks by project; recent knowledge changes. Rows link to the task, room
  and session they relate to. Chat and Sessions accept `#chat?room=<id>` and
  `#sessions?key=<key>` deep links. docs/web/overview.md.
- A per-account **Start page** of widgets in 1 to 4 columns: bookmark groups,
  RSS/Atom feeds, notes and band widgets (workers, tasks, rooms, knowledge,
  sessions), arranged by dragging or arrow buttons. Feeds are fetched by the
  hub: public addresses only unless `ui.feeds_allow_private` is on, a size
  cap (`ui.feed_max_kb`), a time cap (`ui.feed_timeout`) and a shared cache
  (`ui.feed_cache_minutes`). Link-local and metadata addresses and
  non-canonical numeric hosts are always refused; compressed bodies are
  inflated against the size cap; DOCTYPEs are refused. Hub plugins can add
  widget kinds (`WIDGETS`). docs/web/start-page.md.
- **Themes**: every dashboard colour is now a CSS token. Rook (the default,
  unchanged), Paper (light), Graphite and High contrast, picked per account
  under Settings > My preferences (`ui.theme`) and applied before the first
  paint. Themes are `rook-theme/1` JSON files (rook/web/themes/, served at
  `/account/ui/themes/<id>.json`) meant for the desktop app too.
  docs/web/themes.md.
- New hub plugin `ui` (Settings > Dashboard) behind `/ui/account-api`,
  proxied as `/account/ui/api` (`ROOK_UI_ADMIN_URL` for a split hub).
### Desktop app
- A first desktop app (preview), in `desktop/`: a Tauri 2 tray app for
  Windows, macOS and Linux. It shows the hub's dashboard in a window, opens a
  quick-ask box for the home agent on a global hotkey (default
  `Ctrl+Alt+Space`), shows notifications for new chat messages, and offers
  push-to-talk (`Ctrl+Alt+V`) through the voice server's `/api/transcribe`.
  Sign-in uses the hub's existing login (in the dashboard window, or user name
  and password); nothing changes on the hub. Quick ask goes to a private chat
  room with the home agent, or, with an agent token, to `home.ask` over MCP.
  docs/desktop-app.md; why Tauri over Electron: desktop/README.md.
- Wake word behind the `wake-word` build feature and an off-by-default
  setting: openWakeWord's pipeline on tract (pure Rust), with the user's own
  wake model (openWakeWord's ready-made ones are non-commercial). Design:
  docs/design/desktop-voice.md.
- Start and stop this computer's worker from the tray, and an "Install a hub
  on this computer" wizard that runs `scripts/install-hub.sh` on Linux (and
  explains Docker or WSL on macOS and Windows). It finds a hub the installer
  already put here (its `rook-hub` service) and offers to connect or update.
- The dashboard window stays on the hub (other links open in the browser),
  and an ACL manifest limits each app page to the commands it uses. A wake
  model that can't load is refused when saved, or switches the wake word off
  with a notification, instead of crashing the app.
- Notifications also read `GET /api/attention` (huddle questions, tasks that
  need you) on hubs that serve it; the contract is in docs/desktop-app.md.
- CI: `.github/workflows/desktop.yml` tests on Linux and builds unsigned
  bundles on Linux, macOS and Windows (actions pinned by commit, cargo
  `--locked`).
### Agent skill
- The Rook skill (`skills/rook/`) now teaches agents to work as a team, not just to
  run commands: a start-of-session ritual (whoami, a narrowed deck, handoffs,
  knowledge search, unread chat), the task lifecycle (claim, one owner, outcome
  plus evidence, or a handoff), room etiquette, huddle status and questions,
  when to wake an agent, what belongs in knowledge versus tasks and handoffs,
  handoff contents, timeouts and safe retries (`rook_journal`, same
  `request_id`), and common failure modes.
  New references: `coordination.md` (states, required fields, templates, hygiene
  findings), `examples.md` (worked calls) and `troubleshooting.md` (errors and
  fixes).
### Claude Code rules
Manual: claude-code-rules
- Manage > Claude Code: hub-managed triggers and actions for Claude Code on
  the workers (hub plugin and worker plugin `ccmod`). Triggers: Claude Code
  hook events with tool and text filters, Idle for N minutes, worker
  offline/online; scopes: everything, band, worker, project, agent; actions:
  post in a room or huddle, notify, run an allow-listed capability, inject
  instructions, block, status line. `ccmod.push` merges Rook's managed hooks
  into each Claude Code `settings.json` next to the person's own (backup kept,
  diff preview, clean removal); older workers are skipped. Capability actions
  are admin-only, allow-listed (`ccmod.cap_allowlist`) and need
  `ccmod.allow_exec` for exec-tier caps. Docs: docs/web/claude-code-rules.md,
  docs/design/ccmod.md.
- Claude Code rules hardening: a missing hook script (worker folder deleted)
  no longer blocks Claude Code (the hook command ends in `|| exit 0`, and
  the script is kept while any settings file still runs it); a hub restart
  no longer replays queued events (the poll position is stored per worker);
  writing, deleting and previewing rules and the worker's `ccmod.events` are
  admin-only; the hub accepts the hook's word for `contains` on prompts and
  notifications and for project paths; removal restores an untouched
  settings file byte for byte; settings files keep their owner and are
  re-read just before they are replaced (mode and owner are set on the temp
  file's descriptor, never by path after the rename; `atomic_write_text`
  does the same for every caller, and its in-place fallback does not follow
  symlinks); a worker running as root refuses a settings file reached
  through a symlink to another user's file; the outbox is capped (500) and stale
  events (10 min) dropped; hook input up to 8 MiB; pushes record who pushed
  and capability actions run as `system:ccmod:<rule>`; the Claude Code page
  uses the theme colours.

### Autoupdate
Manual: updates
- Manage > Updates (new hub plugin `updates`): the hub checks Rook's GitHub
  releases (ETag, backoff; prereleases only on the prerelease channel),
  verifies downloads against `SHA256SUMS` and, where required, build
  provenance of the files and `release.json` (`gh attestation verify` scoped
  to the release workflow, the release tag and GitHub-hosted runners; fails
  closed), re-reads a tag that was published again, and shows the hub,
  workers, apps and firmware against what is available, with release notes,
  progress and history.
- Hub self-update through `rook hub deploy` (the release sdist repacked and
  signed by the hub; dead-man rollback applies; the hub's key is passed
  explicitly unless the operator pinned one, and a pin to another key
  refuses the update) or by re-running the release's `install-hub.sh` with the wheel
  the hub verified (`--package`) on a `--user` install; other installs get the
  command to run. Refused, not run inside the hub's own service, when
  `systemd-run` cannot start it. Rollback from the History tab.
- Staged worker rollouts: test worker first, then batches, stopping at the
  first failed health check (new build announced, answers the hub, stays on
  it). Behaviour change: with the updates plugin on, a new worker bundle is
  no longer pushed to every worker at once; `/band-worker.json` and the push
  loop serve the *released* build (`/updates/worker/<build>/band-worker.pyz`)
  until a rollout releases the next one, and new builds are announced in
  chat. The first run releases the bundle the hub already has. Switching the
  plugin off, or the update service not running for 15 minutes, restores the
  old behaviour. The push loop leaves a running rollout's workers alone, and
  the feed keeps only bundles signed by the hub's own key.
- Breaking releases: each release publishes `release.json` (compat floors,
  breaking flag and reasons, relay pin, app and firmware versions) and
  `install-hub.sh`; a breaking or unknown update (cumulative over every
  release since the hub's own; a missing or mistyped floor counts as unknown)
  needs the operator's acknowledgement, and the devices it cuts off updated
  or acknowledged; the Workers page tags devices too old for this hub or for
  a pending breaking update.
- Settings `updates.mode` (off / notify, the default / auto), `channel`,
  `window`, `canary`, `batch`, `gate_secs`, `auto_min_age_hours`. Auto mode
  updates the hub only to a stable patch release of its own minor version,
  stable for 24 hours, crossing no breaking or unknown release, whose
  provenance verifies (needs the `gh` CLI). New releases are announced once
  through `notify.send`.
- Android: publish a release's APK through `/apk` and ask phones to update
  (`device.update`). Desktop: `GET /updates/desktop.json`. Firmware versions
  are shown (ESP32 announce, RP2040 `dongle.status`); flashing stays manual.
- Workers install one update at a time: a second `worker.apply`,
  `worker.check`, `worker.update` or in-band push while one is downloading,
  or after one was swapped in and the restart is pending, is refused ("an
  update is already in progress") instead of interleaving, so the rollback
  copy (`band-worker.pyz.prev`) is never overwritten by an unverified build.
  Each download goes to its own temporary file, removed on failure.

### Hub merge
- One hub process: `rook hub serve` (console script `rook-hub`) runs the
  dashboard and the MCP server together on the dashboard port (7005): the MCP
  paths (`/mcp`, `/band`, `/tokens/account-api`, `/authorize`, `/token`,
  `/.well-known/*`, `/skill/*`, `/healthz`) are served there through an
  in-process ASGI bridge, and the old MCP port (8765) keeps serving the MCP
  server alone (`--mcp-bind` / `ROOK_MCP_BIND`, `off` to drop it), so proxy
  routes, agent configs and workers need no change. Both halves share one band
  client, authorizer and set of stores, and the account pages reach the MCP
  side in memory instead of over loopback HTTP: on a local hub with two
  workers, 96 MB RSS instead of 65 + 92 MB. `rook-dashboard` and `rook-mcp`
  still work as separate processes (compat; to be removed in a later
  release). Migration runbook: docs/operations/hub-deploy.md.
- `scripts/local-hub.sh` starts the relay and one hub process
  (`ROOK_HUB_SPLIT=1` for the old two); installers and the printed MCP URL use
  the dashboard port. `compose.yaml` has one `hub` service (upgrade with
  `docker compose up -d --remove-orphans`).
- Upgrade note (Docker): the image's default command changed from
  `rook-dashboard` to `rook-hub`, which runs the MCP server too. A setup that
  starts the image without a command as its dashboard and also runs a
  separate `rook-mcp` container on the same data volume must drop that
  container (or give the dashboard container `command: ["rook-dashboard"]`):
  two MCP servers on one volume would share the stores and both join the band.
- The MCP paths refuse request bodies over 16 MiB with 413
  (`--mcp-max-body` / `ROOK_MCP_MAX_BODY`, 0 = no limit), and answer 503
  while the hub shuts down.
- `rook hub` deploy configs can name a single `hub` service
  (`rook.hub.serve`); the watchdog's unit list is configurable
  (`ROOK_WATCHDOG_SERVICES`).
### Release pipeline and hub install
- One-command hub install: `curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash`
  sets up Python, Rook from the release wheel, the prebuilt relay, generated
  secrets (mode 600) and two systemd units, `rook-relay` and `rook-hub` (the
  single-process hub; `--system` or `--user`), waits for
  health and prints the dashboard URL and password. Re-runs upgrade in place;
  `--uninstall [--purge]`; `--no-systemd` for containers and CI. See
  docs/operations/install.md.
- Releases: a `vX.Y.Z` tag publishes the sdist and wheel (version from the
  tag), a GitHub Release, and `ghcr.io/bake-ware/rook` for amd64 and arm64.
  Build names (`<build>.<adjective>.<noun>`) are unchanged; a hub installed
  from a wheel now stamps its worker bundles with the release's build number
  (they were build 0, so their workers never updated).
- The Docker image uses the prebuilt, sha256-checked relay from a telesthete
  release (`RELAY_SOURCE=source` compiles it as before), and builds again: the
  build context lacked a file `pyproject.toml` force-includes. `compose.yaml`
  runs the published image; `compose.build.yaml` builds from a clone.
- The release wheel requires telesthete by hash-pinned wheel URL instead of
  git, so installing it needs no git.
- CI: pytest on Python 3.11 and 3.12, generated-file checks, `node --check`,
  shellcheck, and an installer end-to-end run. Voice-service tests skip when
  that service's packages are missing instead of failing.
- The relay is pinned (`RELAY`, and each archive's sha256 in `RELAY_SHA256`,
  in `rook/_release.py`); the installer, `scripts/fetch-relay.py` and the image
  verify downloads against that pin, and the installer checks the Rook wheel
  against the release's `SHA256SUMS`. Releases publish as prereleases and are
  marked latest only after the image and an install test pass; artifacts and
  images carry build provenance attestations. Base images are pinned by digest.
- Installer: `--uninstall --purge` asks first (or needs `--yes`) and refuses a
  directory that isn't a hub data directory; `--user` never uses sudo; it never
  signals a pid that isn't its own service; it only falls back to building from
  git when there is no release at all; it warns when the hub would listen on a
  public address.

### Protocol features
- Addressed routing (telesthete SPEC §10.1, needs telesthete 0.3.0): workers
  and band clients declare a route id; targeted calls and replies (request
  `rr`) go only to their addressee on a routing relay. Old relays and peers
  see the same traffic as before. `ROOK_BAND_ROUTE=0` turns it off.
- Large replies (> 32 KB, `ROOK_BAND_DROP_THRESHOLD`) ride telesthete Drop
  with receiver-driven retransmit when the caller accepts it; `BandClient.call`
  returns the normal reply. A 1 MB reply now completes under 1% fragment loss.
  Worker replies are capped (`ROOK_BAND_MAX_REPLY_BYTES`, 32 MiB) with a clear
  error.
- Negotiated zlib compression of whole requests/replies (feature `z`), which
  also carries base64 attachments (screenshots, camera, files) compactly.
- In-band OTA uses one native DROP frame per Drop packet toward workers that
  announce `drop-native` (half the frames); `ROOK_BAND_DROP_NATIVE=0` opts out.
- Announces carry `features` and `caps_hash`; clients keep known caps across
  hash-only announces (opt-in on workers with `ROOK_ANNOUNCE_CAPS_HASH=1`
  after every client is upgraded).
- Workers can dial the relay's native WebSocket listener (`--ws-url`,
  `ROOK_HUB_WS_URL`; installers pass it when `ROOK_HUB_PUBLIC_WS_URL` is set).
  The `/band` bridge stays the default and gets 1 MiB socket buffers.
- telesthete pin (and the hub image's relay) moved to 0.3.0 (ad2865b); bundle
  builders refuse a vendored telesthete without `protocol/route.py`.

### Transport
- Band transport split into a shared frame codec (`rook.worker.wire.codec.
  BandCodec`) and thin UDP/WS links behind the unchanged
  `TelestheteHubTransport` facade. Frames shorter than header + AEAD tag
  (43 B) are now dropped before decryption (was `< 27`).
- One writer task per transport replaces the send lock that held the link for
  every fragment of a message: single-fragment messages (keepalives,
  announces, most replies) go first, multi-fragment messages are interleaved
  fragment by fragment, so a multi-MB reply no longer delays everything else
  for seconds. A call that times out before its message starts sends nothing;
  one that started is finished. Wire format unchanged.
- UDP bulk frames are paced (`ROOK_BAND_UDP_PACE_RATE`, default 2000
  frames/s, `ROOK_BAND_UDP_PACE_BURST` 64) and UDP sockets ask for 4 MB
  buffers, so a large reply no longer overflows the relay's shared queue.
- Keepalives are skipped while other frames flow.
- Inbound dispatch is bounded (`ROOK_BAND_DISPATCH_CONCURRENCY`, default 64,
  plus `ROOK_BAND_DISPATCH_BACKLOG` 1024); overflow is dropped with a
  rate-limited warning.
- `rook.core.backoff.Backoff` (jittered exponential) is used by the WS band
  reconnect, the terminal follower, the Android worker runtime and the KVM
  bridge serial stream, which now reconnects after the dongle reboots.
- Removed the unused `rook.worker.wire.channel` compatibility shim.
### Transport follow-up
- Addressed traffic (targeted requests, replies to `rr`, pass-through Drop
  to a declared route) is sent as telesthete ADDRESSED (0x06) frames, sealed
  with empty AAD so deployed receivers still open them when an old relay
  broadcasts them; the routing relay now routes only ADDRESSED frames, and
  CHANNEL frames are always broadcast. Only peers announcing `route` are
  addressed. Receivers open only CHANNEL and ADDRESSED frames.
- Inbound bursts no longer lose replies: receive loops yield every 32
  frames, only new requests can be shed (a worker answers a shed targeted
  request `busy`), and replies, announces and Drop packets get backpressure
  instead. 3,000 queued replies: 1,912 dropped before, 0 now.
- Large-reply pulls: `BandClient.call`'s timeout now bounds the Drop pull
  (a pull that can't finish returns `ok: false`, `timeout: large reply ...`);
  pass-through Drop packets go out in order (no window stalls on the
  receiver's replay check); the receiver is linear-time (telesthete fix, 32
  MiB pull ~28 s CPU -> 0.3 s); per-drop-id pull locks are released.
- Workers release unpulled reply drops at their TTL on a timer and hold at
  most 32 MiB of them (was 128 MiB, pruned only on the next large reply).
- In-band OTA refuses `drop_id` 0x8000 and up (large-reply ids).
- KVM bridge: malformed stream messages are skipped instead of ending the
  stream, and the old socket is closed before reconnecting.
- `ROOK_ANNOUNCE_CAPS_HASH` must stay off until every hub (> build 426) and
  APK (> 0.4.1) reads `caps_hash`; documented in core spec 14.5.
- telesthete pin and the hub image's relay moved to f3419d2 (audit/hub; move
  to the merged main commit after the telesthete PR lands). Bundle builders
  refuse a telesthete git checkout at any other commit
  (`ROOK_TELESTHETE_ANY_REV=1` to override).

### OTA health gate
- After an OTA update, a worker keeps the new build only once it has completed
  a band round trip, meaning a reply to its own request (it probes
  `caps.describe` on the hub node or another peer it heard announce). Before,
  staying up for 60 s was enough, so a build with a broken transport stranded
  the worker off-band.
- With no round trip within `ROOK_UPDATE_HEALTH_SECS` (setting
  `core.worker.update_health_secs`, default 180 s, 0 = off), the worker
  restores `band-worker.pyz.prev` and restarts into it at once. The reason goes
  to `update_state.json`, a new `update_result.json` and the log.
- The restored build reports `hb.worker.update = "rolled back: no band
  contact: …"` in its announce (shown by `rook_workers`), and `worker.status`
  returns `update.state` / `update.last`.
- A rolled-back build isn't re-applied by the hub push, `worker.apply`,
  `worker.ota_begin` or the poll loop for 24 h (`action: skipped`;
  `worker.check(force=true)` overrides). Pushes during verification are
  skipped as well, so `.prev` is never overwritten by an unverified build.
- The gate applies only if the old build heard the band in the 10 minutes
  before the swap, so a hub that was already unreachable doesn't make every
  worker roll back a good build. Updates from releases older than the gate
  get the old 60 s alive check. Crash-loop rollback (3 boots) and the wire
  format are unchanged.
- Review follow-ups:
  - The gate is also skipped when the old build had nobody to probe (a hub
    without its hub node) or the swap is over 10 minutes old.
  - A band that is heard but has no announcing peer keeps the build on the
    alive check instead of rolling it back.
  - A suspend gap of more than 30 s extends the window.
  - Late probe replies count.
  - After a rollback, the restored build rechecks the band. If it can't reach
    it either, the result is `inconclusive` and the build is held back 1 h,
    not 24 h.
  - Plugin reloads, and restarts after the run stayed up 60 s, no longer
    count as crash boots (all boots are capped at 8).
  - A failed rollback copy is recorded as `rollback_failed`.

### Plugin host
- Runtime plugin enable now uses the boot path (`PluginHost.enable`/`disable`/
  `reload`): placement, stored settings, collision safety, status and the
  worker's `bind_worker`, so re-enabling `work`, `sessions`, `config`, `shell`
  and others no longer crashes. New caps `worker.plugin.reload`,
  `hub.plugin.enable`/`disable`/`reload` (admin).
- Settings marked `apply="reload"` now take effect: a hub plugin reloads when
  one changes, and a worker push that only touches live/reload settings is
  applied in place instead of restarting the worker.
- Capability calls are bounded: `ROOK_CAP_TIMEOUT` (default 300 s, `0` = off),
  per-cap `@capability(timeout=…)`, a cap's own `timeout` argument plus 15 s.
  A timed-out call answers `timeout: …` instead of hanging.
- Every built-in cap (Android's too) declares its risk tier and has a
  docstring; the hand-written tier table shrank to core caps and the floor is
  read from the plugins. Android caps that silently resolved to `exec` now
  carry their real tier (e.g. `ui.text`, `sms.list` read; `device.update` admin).
- The Android app's native plugins load through the plugin host with
  placement (`app == 'android'`) instead of being registered by hand; a cap
  collision no longer crashes the worker. The skill reference lists them.
- `notify.send` reaches every running chat integration, including third-party
  ones. Optional `aiohttp`/PyNaCl imports are lazy, so plugins load and
  `tools/gen_*_reference.py --check` refuse to run with modules missing.
### Plugin kit
- `ROOK_WORKER_HOME` now moves all worker state (identity, enrollment, pushed
  config, audit log, plugin data, chat/inbox, deauth flag, work DB, trust
  anchors), not only the session mirror; the installed bundle stays in
  `~/.rook-band-worker`. Unset, nothing moves.
- New `rook.core.pluginkit` (ships in the worker bundle): `run()` (kills on
  timeout, decodes with `errors="replace"`), `which()`, `is_termux()`/
  `is_windows()`/`is_android()`/`has_display()`, `state_path()`, `fail()` and
  `require_str`/`require_int`/`opt_int`/`require_choice`. Worker plugins use it
  instead of their own copies. New node fact `termux`.
- `cec`, `pikvm`, `agent` (wake), `memory` and `dongle` read their settings
  through the settings view, so values set in the Settings UI are used (env
  vars still win). `cec` and `hermes` always export their plugin and gate in
  `available()`, so their settings show up while they are off.
- Worker caps report failures as the reply's own `ok: false` + `error` instead
  of `ok: true` wrapping `{ok: false}` (file, memory, msg, chat, camera,
  screenshot, battery, wake, deluge, dongle, cec, hermes memory.read,
  sessions.shim.install, worker.config_apply refusals, shell.exec argument
  errors). Every in-tree consumer already handled both shapes.
- `deluge.*` refuses torrent ids that are not hex hashes (or `*`), so an id
  can no longer smuggle a second console command; `deluge.add/pause/resume/
  remove` fail instead of returning `ok: false` with the console output.
- Worker chat client: its watcher thread now stops on every exit path.
  `hermes` logs through `logging` instead of printing.
### Dashboard
- Removed the legacy exec worker (`rook/remote/worker.py`, served as
  `/worker.py`) and the dashboard's `/ws` endpoint it connected to. Every
  installer has deployed `band-worker.pyz` (which joins over `/band`) for a
  long time; `--token` is still accepted and ignored so old units start.
- Removed the MCP port's password-gated, server-rendered `/tokens` page and
  its form routes; the dashboard's Tokens view (and Account -> Band access
  for pairing and PSK rotation) replaced it. `/tokens` on the MCP port now
  redirects to `https://<installer domain>/#tokens`. `ROOK_MCP_AUTH_PASSWORD`
  / `--admin-password` are ignored (a notice is printed when set).
- The dashboard login, first-run setup, Permissions and standalone account
  pages are static files under `rook/web` (`login.html`, `setup.html`,
  `permissions.html`, `account-page.html`) on the shared `theme.css`, filled
  by `rook.remote.web_pages.render`; forms, CSRF fields and redirects are
  unchanged.
- The account JSON APIs (guidance, vault, tokens, knowledge, settings, jobs)
  share one auth gate, `rook.remote.account_api.account_api` (session 401,
  operator 403, JSON object 400, constant-time CSRF 403), and every session
  CSRF compare in the dashboard uses `accounts.csrf_ok`. A POST body that is
  not a JSON object now answers 400 everywhere (some routes said 403).
- `TokenWeb` is a `KnowledgeWeb` proxy like Guidance, Vault, Settings and
  Jobs. A non-operator calling an operator-only `/account/*/api` proxy now
  gets a JSON 403 instead of an HTML one.
- Dashboard scripts share `esc`, `el` and `ago` from `rook/web/manage.js`
  instead of private copies. The classic-script `esc` in `index.html` now
  escapes `'` too. Relative times read the same everywhere ("5 min ago";
  Knowledge and Settings used to say "5m ago").

### Compatibility cleanup
- Cap errors raised through the plugin kit (`fail()`, `CapError`) reach the
  caller as the bare message (`file not found: /x`), not
  `CapError: file not found: /x`, and no longer log a traceback on the
  worker or hub. `bad args:` replies and unexpected exceptions are unchanged.
- `rook/core/compat.py` sets the supported-peer floor: workers from build 142
  (the worker in the 0.4.1 APK, the oldest still deployed), hubs from build
  230. Removed with it: the `rook band` CLI's fallbacks for hubs without
  `/api/band/whoami` and `/api/band/overview`. The hub-side
  `caps.describe(prefix=)` filtering, the v1 deauth wrapper and the classic
  Work view's `read_page` fallback stay, because that APK still needs them.
  Firmware, Android-lagging caps and stored-state migrations are kept.
- `claude-history.send`, `claude-history.resume` and `codex-history.resume`
  say they are deprecated in their own descriptions (use `sessions.send` and
  `work.stream.open(resume=)`). Task hygiene nudges go through
  `sessions.send`.
- The quickstart, test-hub and hub-deploy e2e scripts and `compose.yaml` no
  longer generate or pass `ROOK_MCP_AUTH_PASSWORD`.
- State files are written atomically everywhere: `setup.json` (band PSK),
  the Telegram offset and the home-agent cursor were written in place, and
  `setup.json` was briefly world-readable before its chmod. Modes are kept
  (secrets and worker config 0600).
- Remove next release: the hidden, ignored `rook-remote --token` and
  `rook-mcp --admin-password` / `ROOK_MCP_AUTH_PASSWORD` (and its
  `core.mcp.admin_password` settings entry).

### Review fixes
- Android native caps are back at the tier master gave them (undeclared, so
  `exec`): `sms.list`, `contacts.search`, `calllog.list`, `location.get`,
  `notify.list`/`post`/`dismiss`, `ui.text` and `device.info`,
  `update_status`, `find`, `find_stop`, `wake`, `vibrate`, `torch`,
  `clipboard_get`/`clipboard_set`. Their new `sensitive`/`physical` tags
  stay. The hub, which does not ship the app's plugins, now carries them in
  `authz.CORE` so an Android peer cannot announce them lower. Any of them can
  be lowered later as a deliberate change (plugin, `authz.CORE` and
  `tests/fixtures/master_tier_floor.json` together).
- `tests/test_tier_snapshot.py` pins every cap's effective tier and tags to
  at least master's table (frozen fixture), Android caps included.
- Reloading or re-enabling the hub's knowledge plugin keeps the MCP bridge's
  wiring (attributed caller, handoff store, band list): it is now a plugin
  host bind hook (`PluginHost.add_bind_hook`) that runs on every activation,
  and repoints `rook_knowledge`, the hygiene loop and the Knowledge page API
  at the new instance. Before, a reload left bands collapsed to `default`
  and inline handoffs failing.
- Workers keep running on Python 3.10, as master's did: the per-cap timeout
  (and Codex direct input) use `rook.core.aiocompat.timeout`, a backport of
  `asyncio.timeout`, instead of the 3.11-only API, and a test keeps 3.11-only
  APIs out of the worker bundle. Workers are not refused on 3.10, since
  master's worker never needed 3.11.
- `deluge.pause`/`resume`/`remove`/`files` take a torrent name again (deluge
  matches names as well as hashes), passed to `deluge-console` as one quoted
  argument; only `;`, quotes, backslashes, control characters and a leading
  `-` are refused. `deluge.add` refuses `;` (it split the console command
  even inside the old double quotes) and single-quotes its argument.
- `screenshot.capture_region` accepts negative `x`/`y` again (monitors left
  of or above the primary one); the ImageMagick backend gets a signed crop
  offset.
- Plugin reload: `worker.plugin.reload`/`disable` and `hub.plugin.reload`/
  `disable` are tagged `destructive`. A reload keeps the plugin's caps
  registered until the new instance takes them over, so an announce sent
  mid-reload no longer drops them. A reload that fails leaves dependents with
  `dependency()` = `None` (status `dependency_unloaded`) instead of the
  stopped instance. `selfupdate` no longer adds another OTA binary handler
  on every reload.
- With `ROOK_WORKER_HOME` set, a worker copies state missing there from
  `~/.rook-band-worker` once at start (worker_id, enrollment, deauth flag,
  hold, update state, pushed config, plugin/custom-cap state, anchors, audit,
  chat, work DB, plugin data), keeping modes; older releases kept it there.
  The deauth flag in `~/.rook-band-worker` is honoured even when nothing is
  copied. `ROOK_WORKER_HOME_FRESH=1` starts a separate worker instead.
- Dashboard modules import `manage.js` with their own `?v=` version (and the
  Account/Tokens views and the standalone `/account/bands` page load
  versioned URLs), so a stale edge cache cannot pair a new module with an old
  `manage.js`.
- A database opened with a mode (the vault, enrollment, and every other 0600
  store) gets the same mode on its `-wal`/`-shm`/`-journal` files: existing
  ones are chmodded on open and new ones are created under a matching umask.
- `rook/core/compat.py` documents that the Sessions dashboard needs worker
  build 364+ (`sessions.*`): deployed pyz workers are at 401, and the 0.4.1
  APK's embedded worker (142) gets the read-only `work.sessions` fallback,
  which stays until the peer floor passes 364. No deployed worker sits
  between 143 and 363.
- Atomic state writes fall back to an fsync'd in-place write when the file
  cannot be replaced (`EBUSY`/`EXDEV`), so `setup.json` saves again when
  `ROOK_SETUP_PATH` is a single bind-mounted file. Modes are kept.
- `PIKVM_URL` without a scheme (`kvm.lan`) means `https://kvm.lan`; one that
  is not an http(s) URL or host keeps `pikvm.*` unloaded with a warning.
  Before, it loaded and silently talked to `https://localhost`.

### Plugin management
- Manage > **Plugins**: one grid of every plugin (rows) on the hub and each
  online worker (columns, filterable by band and OS; a per-plugin summary on
  narrow screens). Each cell says on / off / not available and why
  (placement, `available()`, a missing dependency, the platform) or the
  error; click it to switch the plugin live there. A detail panel lists caps
  with their risk and docstring, links the plugin's settings, switches it on
  every eligible worker at once and reloads it. Core plugins (`selfupdate`,
  `config`, `enrollment`, `info`; hub `settings`, `policy`, `info`) stay on.
- Hub plugin switches persist: `hub.plugin.enable/disable` and the page
  store them in `core.plugins.hub` and the hub applies them at start.
  Turning on a plugin that has an `enabled` setting turns that on too; an
  environment variable for it still wins (docs/design/plugins.md §9.2).
- Defaults for new workers per band (`core.plugins.worker_defaults`):
  enrollment hands them to the worker as a plugin profile (a pairing code
  carries one only when the operator attaches it), and the worker applies its
  disables on its first start only (persisted like `worker.plugin.disable`;
  never widens `--enable`; older workers ignore it). Installers pass nothing new.
- Presets, "What do you want Rook to do?" (remote control, agent sessions,
  knowledge & tasks, home & voice agent, phone & chat alerts, hardware), each
  with its risk tiers and high-risk parts called out: on the Plugins page,
  as step 1 before "Get a pairing code" on the first-worker card and the
  Install page, and in the /setup wizard. Boxes show the stored choices;
  saving writes only the groups that changed.
- Marketplace preview: catalog format `rook-plugin-catalog/1`
  (docs/design/plugin-marketplace.md), `core.plugins.catalogs` /
  `ROOK_PLUGIN_CATALOGS`, a Marketplace tab listing entries (a bundled
  example catalog of fictional entries when none is set), and
  `plugin.marketplace.list` / `plugin.marketplace.install` on worker `rook`;
  installing is not implemented yet and says so.

### First-run onboarding
- One public base URL (`rook/remote/public_url.py`) for installers, one-liners,
  the account origin and the update manifest. `--domain`/`ROOK_DOMAIN` may now
  carry a scheme (`http://192.0.2.10:7005`); without one it stays `https`
  except for a local address (non-public IP, `localhost`, dotless or
  `.lan`/`.local`/`.internal` name) on this dashboard's own port, or port 80,
  so a LAN quickstart hub serves `http://` installers and a public hub's
  scripts are unchanged. Workers accept `--enroll http://` only
  for a non-public IP literal (LAN, loopback, tailnet).
- Dashboard and account cookies are set through one helper: `Secure` whenever
  the public base is `https` (as before, even if a proxy omits
  `X-Forwarded-Proto`) or the request came over HTTPS (`X-Forwarded-Proto`,
  `Forwarded`, `CF-Visitor`); only an `http://` hub reached over plain HTTP
  sends them without it. `rook_session` was never `Secure`; it now is on HTTPS
  requests. Fixes the endless redirect (ERR_TOO_MANY_REDIRECTS) and "Failed to
  fetch / Sign in" on Tokens and Account & access over plain HTTP.
- The dashboard-to-account sign-in bridge is loop-proof (a one-minute
  `rook_bridge` marker: a second miss answers 400 with what to fix) and also
  covers `/account/bands/api` and the Settings, Knowledge, Guidance, Vault and
  Jobs APIs. It only bridges GET/HEAD.
- A hub without a worker bundle builds one itself at start, in the background
  (`build_band_worker.py --output-dir`, signed with the hub's update key, the
  manifest `url` on the public base), into `$ROOK_DATA_DIR/worker-bundle`
  (else `~/.local/share/rook/worker-bundle`), and rebuilds it when the worker
  code is newer or the public URL changed. `/band-worker.pyz` and `.json`
  answer 503 "building, retry in a minute" meanwhile. An operator-built bundle
  beside the script still wins and is never rebuilt automatically.
  `/band-worker-enrollment.pyz` serves the same bundle unless an operator drops
  a separate one.
- `scripts/local-hub.sh` finds a `cargo install`ed relay in `~/.cargo/bin`
  (`$CARGO_HOME/bin`) when it is not on PATH yet, passes an explicit
  `http://` dashboard domain, and prints the Install-a-worker URL.
- Running from a git checkout reports `0.dev+<short commit>` (read from `.git`
  once at import, no subprocess) instead of a bare `0.dev`; build stays 0, so
  dev workers still never auto-update. Stamped bundles are unchanged.
- Install a worker: a "Get a pairing code" button mints a code for the
  selected (else primary) band and shows the complete Linux/macOS, Windows
  PowerShell and Android (Termux) commands with the code filled in and a
  self-renewing countdown. The Workers view shows an "Add your first worker"
  card with the same button while only the hub is on the band. Same owner-only,
  CSRF-checked pair action as Account & access (`manage.js` `mintPairing`,
  `pairingPanel`, `pairCommands`; the pairing dialog now uses them too and
  gains the Android line and copy buttons).
- README Quickstart lists the Debian/Ubuntu (and Fedora/Arch) prerequisites and
  the rustup one-liner; "Workers on your LAN" describes the Install-page flow
  over plain HTTP.
- Windows worker one-liner (`iex (irm ".../worker?band=CODE&os=windows")`):
  the script runs inside one script block and never `exit`s under `iex`, so
  the user's window stays open (no more "[process exited with code 1]") and
  its preferences are untouched. From a non-admin prompt it waits for the
  elevated run and reports the outcome there: "Done: <name> is installed and
  joining the band...", or the error, the exit code (`$LASTEXITCODE`) and the
  log path; a declined UAC prompt says so. The elevated run logs to
  `%USERPROFILE%\.rook-band-worker\install.log` and stays open on failure
  ("Press Enter to close"); it closes itself on success.
- Re-running an installer (upgrade, re-pairing to another hub) no longer fails
  on Windows with "band-worker.pyz ... being used by another process": the
  bundle downloads to `band-worker.pyz.download`, then the running worker and
  the `RookBandWorker` task are stopped and the file is moved into place; a
  failure after that restarts the previous worker. The Linux/macOS installer
  downloads beside the live bundle and renames it over (atomic), so a running
  worker never reads a half-written file.
- Both installers announce how long they take and print numbered progress
  (Python, venv, dependencies, bundle, enrollment, service); the bash one ends
  with the same "Done" line or a "FAILED (exit code N)" line.
- When enrollment fails on a pairing-code install (typically a code that
  expired during a slow first install), both installers ask for the fresh
  code the dashboard shows and retry instead of failing.
- The Install page's pairing commands carry a short note: UAC and 2-5 minutes
  on Windows; a per-user service, possible sudo prompt and 1-3 minutes on
  Linux/macOS.

### Work views and huddles
- Work area: **Board** (kanban by status; drag a card or use its menu),
  **List** (sortable, filterable table), **Timeline** (tasks with dates by
  project, today line, dependency arrows) and a **project page** (progress,
  open/blocked/overdue counts, recent activity, linked rooms and knowledge).
  Moving a card is the same `update` an agent sends, so the saving rules,
  claims and history apply; the dialogs collect the reason, handoff or
  outcome they need. docs/web/work-views.md.
- Tasks and projects take optional `attrs.start` / `attrs.due`
  (`YYYY-MM-DD`); new read action `board` (knowledge/task caps) returns
  projects and tasks flat, plus a project page's extras with `id=`.
- **Huddles** (hub plugin `huddle`, on by default, in the *Agent sessions*
  preset): a group chat room per project or band where agents post status
  and ask the person questions with the new `rook_huddle` tool (caps
  `huddle.read` / `huddle.write`). Questions stay pinned on Work > Huddle
  until answered; the answer is posted to the asker. Optionally (setting
  `wake_on_answer`, off by default) it also wakes the asker, only on the
  machine it names (its own X-Rook-Host) and authorized as the asking agent. Huddles are
  plain chat rooms for older clients; the structure lives in new tables in
  `chat.db`. Status lines are "quiet" chat messages (new nullable column):
  they do not raise `_unread_chat` for other agents.
- `ChatStore.send` also returns the message `seq`; `ChatStore.join` adds
  participants without posting. The Chat page opens `#chat?room=<id>`.

### Added
Manual: sessions, workers, work, chat, agents-and-mcp, settings, integrations, accounts-and-access, running-a-hub, getting-started, knowledge, terminal-and-commands
- Sessions catalog (docs/design/sessions.md, workstream A): worker caps
  `sessions.list`, `sessions.follow`, `sessions.send` and `sessions.stop`
  give one record per agent session (state live/idle/closed, origin, view,
  input, inbox policy) on every OS; live-session detection now works on
  macOS and Windows too; `*-history.resume` opens a Rook terminal where the
  worker has one; the hub serves the merged list at
  `GET /account/work/sessions`; workers report `hb.sessions` counts. Claude
  Code mod 0.3.1 stops a terminal-resumed session by closing its terminal.
- Dashboard workers page: a **3D view** beside list and grid (the hub as a
  rook, each worker a pawn on a polar board; a quiet worker's pawn tips over,
  a heartbeat sends a spark to the hub, a ring marks a worker that hosts
  sites; click a piece for its details, hosted links and capabilities).
  Group headings fold and unfold on a click. An expanded worker lists the
  sites and services it hosts as links. The version spread lists every
  version.
- Worker menu (⋯), the same in every view: **Rename…** (`worker.reconfigure`
  with a new name; the worker saves it and restarts) and deauth / remove ban,
  which moved there from the expanded row. Capabilities show as chips by
  namespace in the list, the grid and the 3D card.
- The hub worker `rook` reports the release it runs (`ROOK_RELEASE`, set by
  `rook hub deploy`) as its version and build, in the worker scheme.
- `serves`: hand-written hosting metadata per worker, kept on the hub. Hub
  plugin `serves` with caps `serves.list` (read), `serves.set` and
  `serves.clear` (write) on worker `rook`; each worker's `{sites, services}`
  (lists of `{name, url, note?}`) appears as the `serves` field of its
  `rook_workers` row. Keyed by worker name; needs no worker update.
- Task and handoff tools sized for grooming a deck. `rook_task` deck rows carry
  `revision`, resolved `dependencies` with an `unblocked` flag and, when a
  claim has gone quiet, a `hygiene` reason; deck takes `data {states,
  done_days, fields, handoffs, outcome}` (`handoffs: true` lists every open
  handoff thread with the open tasks linked to it). New actions `note` (a
  dated remark with optional evidence, no revision needed) and `batch` (up to
  50 writes, one result each). `release data {actor}` frees another actor's
  claim once it has been idle for two hours. A project update with
  `data {cascade: true}` gives `paused` or `archived` to its open tasks (work
  in progress is skipped and listed). `rook_handoff_save` takes `task` (link
  to that task, not the claimed one) and `status="closed"` (close a thread).
- Task state `closed`: a task closed because a person said so, kept apart
  from `done`, which needs evidence of the work. It takes
  `data {closed_by: {who, quote, session}}`: the person, their words, and the
  session they said it in. Rook records them on the task and as a `closed_by`
  link. A closed task does not unblock tasks that depend on it.
- Core specification `docs/spec/core-v1.md` (v1.0, RFC 2119): band transport
  profile, messages, caps and tiers, placement, the hub worker `rook` with
  grants and tickets, chat rooms, plugin contract, compatibility rules.
  `conformance/` has test vectors generated from the reference
  (`generate.py`, checked by the unit suite) and a live harness that runs a
  candidate worker against a throwaway hub. Example ports in TypeScript
  (Node, no runtime dependencies) and Rust under `examples/ports/` pass all
  vectors and the live run.
- Hub chat rooms on the band: caps `chat.read`, `chat.write`, `chat.delete`,
  `chat.presence` on worker `rook` (same store as `rook_chat_*`). Band callers
  are recorded as `band:<identity>`; posting needs `ROOK_HUB_BAND_MAX_RISK=write`.
  `scripts/test-hub.sh start --band-max-risk` sets it for a test hub.
- **Persona** plugin (docs/design/persona.md): persona profiles (name, voice,
  rules, do/don't, formatting, per-harness addenda) stored on the hub with
  scoped assignments (user > agent family > band > default), versions and
  attributed history; edited on Settings > Persona or with `persona.get/list/
  render/history` (read) and `persona.set/assign/delete` (admin) on worker
  `rook`. Delivered in each MCP session's `initialize` instructions (after the
  server guidance, at most 1,200 characters; unchanged when nothing is
  assigned), by the worker cap `persona.apply`, which writes a delimited,
  idempotent marker block into CLAUDE.md / AGENTS.md / SOUL.md without
  touching anything else (dry run, diff, removal), in work launches
  (`--append-system-prompt` / Codex `developer_instructions`), in the skill's
  site notes, and to the voice service as `voice.assistant_name` /
  `voice.owner` through `settings.fetch("voice")`.
- One **Settings** area in the dashboard, rendered from the settings schema:
  hub, each band, each worker (with plugin enable/disable and per-worker
  values), each plugin or service (Voice, Decision engine, Knowledge, Tasks,
  Watchdog, worker plugins) and personal preferences. Every row shows the
  effective value, its source (default / setup.json / hub / band / worker /
  user / env or flag, which locks it), what it hides, how a change applies and
  a restart-required flag; conflicts are listed on the overview; search
  matches keys, labels and variable names. Backed by `settings.db` (beside
  `enrollment.db`, shared by both hub processes) with attributed history, and
  served as caps on worker `rook`: `settings.describe/get/set/reset/history/
  apply_worker`, `settings.fetch` for services with a scoped token
  (`core.settings.service_readers`) and `settings.worker_secret` for workers.
  Secrets are stored in the vault and never returned. Generated reference:
  `docs/operations/settings-reference.md`.
- `setting()` gains `apply`, `bootstrap`, `overridable`, env alias lists (and
  the canonical `ROOK_<NS>_<NAME>`), `flag`, `group`, `order`, `min`/`max`/
  `pattern`, `advanced`, `deprecated`, and `url`/`hostport`/`path` types.
- Worker settings are pushed from the Settings page with the commit-confirmed
  config apply; worker secrets go as `{{secret:…}}` references that the worker
  fetches from the hub at use (memory only, never on its disk). New worker cap
  `worker.settings_report`. Tasks has its own `enabled` setting (`ROOK_TASKS`).
- Telegram and Discord integrations as hub plugins (`telegram`, `discord`,
  both off by default), with no platform library needed. Each one:
  - bridges Rook chat rooms both ways with a `<platform>:<user>` sender,
    mention mapping, rate limits and loop prevention;
  - offers `telegram.send` / `discord.send`, plus `notify.send` for every
    running integration (the watchdog can use it with
    `ROOK_WATCHDOG_VIA_HUB=1`);
  - answers a fixed command set that runs as `integration:<platform>` and
    fails closed on the permission policy, so there is no exec or admin
    unless a rule grants it.

  The bot token lives in the vault and is masked everywhere. See
  `docs/integrations.md`.
- Work worklog view (default; `ROOK_WORK_V2=0` or the **Classic view** button
  restores the old one): rooms per project and host, live sessions as real
  xterm.js terminals, finished ones collapsed, one-click resume of any
  Claude/Codex history. New worker caps `work.stream.*` (PTY
  open/read/write/resize/signal/close/list with long-poll reads and compressed
  framing), `work.sessions` and `work.export` (`rook.transcript/1`). The hub
  fans each terminal out to many viewers with one input holder, a bounded
  replay ring and reconnect replay. Agent launch templates (claude, codex,
  hermes, shell) can get a session-scoped Rook MCP token. See
  `docs/web/worklog.md`.
- One plugin API for the hub and workers (`rook.core`, contract in
  `docs/design/plugins.md`): manifest with `core_api` range and
  `<build>.<adjective>.<noun>` versions, placement over node facts
  (`place("is_hub")`, `has('gpu', vram_gb >= 8)`), cap `risk`/`limit`/`fields`
  enforced by core, settings schema, `cap://` resources, migrations. Existing
  worker plugins load unchanged. Worker announces add optional `facts` and
  `tiers` keys.
- The hub appears on every band as the reserved worker `rook`, serving
  hub-placed plugins: `rook_call(cap="hub.info", worker="rook")`. Band calls
  to it may reach only read caps (`ROOK_HUB_BAND_MAX_RISK`);
  `ROOK_HUB_PLUGINS=0` turns it off. Caps declared `tool=True` get a generated
  MCP tool.
- Knowledge and tasks are hub plugins (`rook/hub/plugins/knowledge`,
  `rook/hub/plugins/tasks.py`): caps `knowledge.read`/`knowledge.write` and
  `task.read`/`task.write` on worker `rook` (band callers reach the read caps),
  a settings schema (`enabled` = `ROOK_KNOWLEDGE`, `db_path`, `semantic`,
  `embedder` = `ROOK_EMBED_URL` or `cap://any/embed.text`, `embed_model`) and
  plugin migrations. `rook_knowledge`, `rook_task`, `rook_project` and
  `rook_concept` keep their names, arguments and replies; an existing
  `knowledge.db` is used in place and upgraded without loss. Plugin guidance
  slots now appear in the guidance store. Core API 1.1 adds `DEPENDS` and
  wires settings before `available()`.
- Agent skill at `skills/rook/` (SKILL.md + install/admin/tools/usage
  references). `tools/gen_skill_reference.py` generates the tool and cap tables
  from the code; a test fails when they are stale. The hub serves it as MCP
  resources (`rook://skill/rook`) and `GET /skill/rook.skill`, with optional
  operator site notes (`ROOK_SKILL_SITE_PAGE` / `ROOK_SKILL_SITE_FILE`).
  `rook skill install [--harness claude|codex]` installs it.
- `scripts/local-hub.sh` runs the relay, dashboard and MCP server locally with
  generated credentials.
- `Dockerfile` and `compose.yaml` for running the hub in containers.
- Console scripts `rook-dashboard`, `rook-mcp` and `rook-worker`, and
  `rook dashboard` / `rook mcp` subcommands.
- `ROOK_DATA_DIR` keeps all hub state in one directory.
- `ROOK_*` environment variables for every hub and worker setting, documented in
  `.env.example`.
- `ROOK_UPDATE_PUBKEY` lets workers trust an operator's own OTA signing key.
- Dashboard `--bind`; it refuses to serve on a non-loopback address without a
  password unless `--insecure-no-auth` is given.
- README quickstart and architecture overview; feature tour moved to
  `docs/FEATURES.md`; `CONTRIBUTING.md`.
- Licensed under the Apache License 2.0, with a `NOTICE` for bundled
  third-party components.

- MCP roster filters: `rook_workers(name=, cap_prefix=, online=, fields=)` and
  `rook_caps(prefix=, worker=)`. `caps.describe` takes `prefix=` on the worker;
  through `rook_call` the hub filters it itself, so it works on older workers.
- `rook_call(text=true)` returns a successful result as plain text
  (`shell.exec`: stdout, then stderr and exit code only when set).
- `rook_knowledge`/`rook_task`/`rook_project`/`rook_concept` search and list
  accept `data.fields` (a list, comma string, or `"all"`).
- `tools/token_budget.py` measures what the MCP costs an agent (tools/list,
  instructions, common replies) against an in-process hub.
- `ROOK_MCP_ENVELOPE=legacy` restores the previous `rook_call` reply shape.
- Permissions (`docs/design/permissions.md`), shipped in **audit mode**: a
  policy engine (per-principal tier defaults, most-specific-wins rules with
  worker/group/fact target selectors, hard invariants) evaluated inside the
  hub's band client for every hub path, plus hub tools as caps on `rook`.
  Nothing is denied until `policy.json` sets `"mode": "enforce"`; would-be
  denials are journaled (`calls` gains `principal`, `decision`, `rule`,
  `policy_rev`, `tier`). Principals come from verified credentials only;
  tokens get a role at mint (agent, operator, readonly, integration, custom).
  `policy.explain/get/set/status` caps on `rook` and a `/permissions`
  dashboard page. Built-in cap tier table; a worker may declare a higher tier,
  never a lower one; unknown caps are exec.
- Signed role grants: the OTA root key signs an `is_hub` grant for a new hub
  operational key (`hub-op-key`, rotated every 30 days). The hub announces
  it with an op-key signature (proof of possession); only its holder resolves
  as `rook`, and anyone else announcing that name is quarantined as
  `rook~<id8>` and journaled as `audit.impostor`.
- Call tickets: targeted hub calls carry a short-lived, op-key-signed ticket
  bound to the worker, message id, cap and exact args. Workers verify them in
  `audit` mode by default (`ROOK_AUTHZ_MODE=off|audit|enforce-admin|
  enforce-exec|enforce-all`), record the result in `audit.jsonl` and announce
  their readiness under `authz`. Build-167 workers ignore the extra keys.

### Changed
- Over MCP, `rook_task`/`rook_knowledge` `get` returns only hand-made links
  plus `auto_links` (a count per kind) and the last 10 events; pass
  `data {links: "all", events: N}` for more. Deck outcomes are cut to 240
  characters unless `data {outcome: "full"}`. A revision conflict now names
  the current revision (`current_revision` in the reply).
- A claim idle for more than two hours no longer collects automatic links.
  Finishing, cancelling or archiving a task closes its handoff threads unless
  another open task uses them, and an inline `data.handoff` continues the
  task's existing thread instead of starting a new one.
- The dashboard's domain, relay address and band label now follow environment
  or flag > Settings page > `setup.json` > default; `setup.json` no longer
  silently overrides the environment, and a conflict is logged and shown.
- `ROOK_BAND_PSK` only seeds an empty enrollment database, for the dashboard
  and the MCP server; the MCP server no longer requires `--psk` and serves the
  bands in the shared enrollment database. The MCP server honours
  `ROOK_CHAT_DB`, and the dashboard follows the MCP's chat database.
- `worker.config_get`, `rook_config_get`, `rook_config_apply` and
  `shell.env.list`/`env.get` mask the band key and pushed env values.
- A plugin enabled at runtime that `--enable` leaves out now survives a
  worker restart; `worker.plugin.list` says why a plugin is not loaded.
- A worker started as `python -m rook.worker` restarts correctly (re-exec with
  `-m`).
- MCP replies are compact. Tool results are JSON without indentation and are
  sent once (no `structuredContent` copy), tool listings drop output schemas
  and pydantic schema noise, and the default tool descriptions and server
  instructions are shorter: `tools/list` went from about 32,400 to under
  12,000 characters for the same 29 tools. Details stay in cap tips and
  error messages.
- `rook_call` reply shape for MCP clients: `{ok, id, from, result|error}` where
  `id` is the journal id (`_journal_id` is gone), `from` is the worker's name
  instead of its hex id, and a `shell.exec` result drops empty
  `stdout`/`stderr` and the `ok` implied by `code` (`{"code":0}` for a silent
  success). `_task` and `_unread_chat` appear only when new or changed for the
  MCP session; the per-reply `_hint` line is gone (`_tips` still shows once
  per session, `hint=true` re-shows it). `ok`, `result` and `error` keep their
  meaning, so clients reading those are unaffected; the band wire protocol and
  the dashboard `/api/band/*` API are unchanged.
- `rook_workers` returns name, description, build, hb and last_seen_age_secs
  per worker by default (empty values omitted; worker_id added when a name is
  shared); `fields="all"` gives the previous rows. `rook_caps` returns
  `{workers: N, caps: {cap: "*" | {all_but: [...]} | [names]}}`.
- `caps.describe` through `rook_call` returns `{cap: "(args) — doc"}`.
- Knowledge search over MCP defaults to 5 results with 240-character excerpts
  and a small field set; list defaults to 20. The operator's Knowledge page is
  unchanged.
- Default operator tool tips (guidance `tool:*`) are empty; their content is in
  the tool descriptions. The slots remain editable.
- Removed the pre-band personal agent (`rook agent`, `rook hub`, `rook discord`,
  `rook sync`, `rook extract`, the `rook/core`, `tools`, `memory`, `modules`,
  `net`, `interfaces`, `voice`, `tasks` packages, `config.yaml` and the
  `[legacy]` extra). Its dashboard routes (`/api/workers`, `/api/facts`, `/ui/`
  and others) had been failing on every hub. It remains in git history.
- The repository no longer ships the maintainer's update signing key. Each hub
  creates its own on first start and the worker bundles it builds trust only
  that key. Workers run from source trust no update unless `ROOK_UPDATE_PUBKEY`
  is set.
- `telesthete` is now a declared dependency (installed from GitHub).
- The pre-band personal agent's dependencies (OpenAI, Anthropic, Discord, Kuzu, …)
  are no longer installed; screen capture moved to the `[desktop]` extra.
- Defaults point at localhost instead of the maintainer's hosts: worker `--hub`,
  terminal UI `--url` and user, voice service MCP URL.
- Maintainer deployment records, handoffs, incident notes and the roadmap are
  no longer kept in this repository.

### Fixed
- The deck showed a retracted handoff link as a task's latest handoff.
- The dashboard listed the hub's own worker `rook` under whichever band it
  had last announced on, offered to move it to another band and then asked
  for a worker update. The hub is now shown as on all bands, grouped as
  **HUB** when grouping by operating system, has no move action, and is no
  longer part of a band's roster for worker moves or key migration. Workers
  that announce platform facts are grouped by them without a device probe.
- `worker.deauth` accepted any validly signed OTA manifest as an order and
  skipped the target/age checks when fields were missing. It now requires a
  deauth v2 order with its own signature domain and mandatory `worker_id` and
  `issued_at`; the hub sends one nested in a legacy body so older workers
  still park.
- `worker.update(url=...)` installed a bundle with no signature check. It now
  needs a signed OTA `manifest` (sha256 and `--selftest` checked, no
  downgrades).
- `worker.reconfigure`, `worker.update` and `worker.config_apply` could
  repoint a worker at another hub or PSK without a signed order. Hub/PSK
  changes now need a verified hub ticket (`ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT=1`
  on the worker is the local escape hatch).
- The first-run `/setup` page required no login: anyone who could reach an
  unconfigured hub could set its band key, even with a dashboard password.
  With a password, `/setup` now needs the login first. A password-less hub
  (loopback only) keeps an open wizard, protected by a form token.
- Dashboard login cookies were a fixed hash of the username and password, so
  they never expired and could not be revoked. Each login now gets a random
  30-day session that ends on logout or when the password changes. Existing
  logins must sign in once more.
- The MCP server failed to start without `--public-url`.
- The MCP server rejected loopback Host headers on any port other than 8765.
- A hub configured only with `ROOK_BAND_PSK` / `--psk` stayed locked behind the
  `/setup` wizard.
- The relay's default 15s peer TTL evicted Rook peers between their 20s
  keepalives; the quickstart and compose file set it to 60s.
- Dashboard `/api/band/call` now accepts a worker name as well as its id.
- A worker now warns when saved enrollment or pushed config overrides the
  `--hub`/`--psk` it was started with.
- The worker bundle builder can use a pip-installed `telesthete`.

### Data layer and session history
- `rook tmux` is removed: it ran a second Claude Code session manager (its
  own `~/.rook/sessions.db`) beside Rook terminals. It now prints where its
  jobs went: the Sessions page, `rook band`, and `sessions.shim.install`.
- `rook sessions` reads transcripts with the worker's `claude-history`
  parsers, so titles, tool calls and counts match the dashboard.
- The dashboard (Sessions page and classic Work view) uses `sessions.follow`,
  `sessions.send` and `sessions.list` only; a worker that has just the older
  `<agent>-history.*` caps is asked to update instead of being driven through
  them. Classic "Resume on host" opens a Rook terminal (`work.stream.open`).
  `claude-history.send`/`resume` and `codex-history.resume` stay on workers
  and are documented as deprecated for direct use.
- `codex-history.resume` finds the real `codex` binary (never the session
  shim) and the Windows npm `codex.cmd`, like Claude's lookup.
- Internal: `rook.core.db` (SQLite open/transaction helpers, additive column
  backfill, atomic file writes) used by the hub and worker stores; one shared
  work-session store and command path for hub and worker; one manifest
  canonical form and file digest (`rook.worker._update_verify`) for worker
  bundles and hub releases; `rook.worker.agents` and `rook.worker.ring`.
  Database schemas, paths and file modes are unchanged.

## Development history (June to September 2026)

Unversioned work on `master` before public preparation, in broad strokes:

- **Agent workspace (September):** shared knowledge wiki with human
  verify/dispute and review mode; concepts, projects and tasks with claims,
  evidence and handoffs; a secret vault with `{{secret:name}}` placeholders;
  operator-editable agent instructions; per-token attribution of every MCP call;
  MCP session limits and a Telegram watchdog.
- **Accounts and enrollment (September):** Google and local accounts, pairing
  codes, certificate-backed worker enrollment, five-word band keys, band
  management with verified worker moves and PSK migration.
- **Work sessions (September):** Claude Code and Codex session history, review and
  resume across workers from the dashboard.
- **Android (August and September):** native worker app with screen capture,
  accessibility-based input, SMS, notifications, location and device controls;
  voice assistant with wake word; verified APK self-update.
- **Band services (July and August):** signed OTA self-update with in-band push,
  signed deauth and ban, console rooms, chat rooms with presence and wake,
  `rook band` terminal UI, Windows workers, custom command capabilities.
- **Hardware:** ESP32-S3 USB HID/serial dongle firmware, PiKVM and HDMI-CEC plugins.
- **Security:** fixed AEAD nonce reuse across peers (CSPRNG-seeded sequence
  numbers); the first public import was sanitized of credentials.
