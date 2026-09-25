# Changelog

All notable changes to Rook are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Rook has no tagged
releases yet: workers identify themselves by build number (for example
`111.stinky.goat`), and the Python package version is `0.1.0`.

## Unreleased

Preparation for public use.

### Added
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
  `docs/FEATURES.md`; `CONTRIBUTING.md`; license placeholder.

### Changed
- `telesthete` is now a declared dependency (installed from GitHub).
- The pre-band personal agent's dependencies (OpenAI, Anthropic, Discord, Kuzu, …)
  moved to the `[legacy]` extra; screen capture to `[desktop]`.
- Defaults point at localhost instead of the maintainer's hosts: worker `--hub`,
  terminal UI `--url` and user, voice service MCP URL.
- The legacy pre-band hub no longer has a built-in PSK.
- Maintainer deployment records, handoffs, incident notes and roadmap moved to
  `docs/internal/`.

### Fixed
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
