# Changelog

All notable changes to Rook are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Rook has no tagged
releases yet: workers identify themselves by build number (for example
`111.stinky.goat`), and the Python package version is `0.1.0`.

## Unreleased

Preparation for public use.

### Added
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

### Changed
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
