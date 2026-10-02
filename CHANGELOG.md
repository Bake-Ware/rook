# Changelog

All notable changes to Rook are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Rook has no tagged
releases yet: workers identify themselves by build number (for example
`111.stinky.goat`), and the Python package version is `0.1.0`.

## Unreleased

Preparation for public use.

### Added
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
