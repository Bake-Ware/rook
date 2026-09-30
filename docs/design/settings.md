# Settings: inventory and proposal

Status: design (rook-beta wave 1). This document changes no behaviour. Part 1 lists
every setting as of `beta` (cut from master `733fc16`). Part 2 lists the problems.
Part 3 proposes one Settings area built from per-plugin `setting()` schemas, and
a migration path to it.

Related: `docs/DESIGN-band-services.md` §1 (settings wizard and commit-confirmed
config OTA). Part 3 builds on that design and does not replace it.

## How the inventory was built

Each row was checked against the source with these searches (run from the repo
root). Tests, `tests/`, and generated files were excluded.

```sh
grep -rnE 'ROOK_[A-Z0-9_]+' --include='*.py' --include='*.sh' --include='*.yaml' \
     --include='*.service' --include='*.example' --include='*.kt' --include='Dockerfile' .
grep -rnE '(environ|getenv)' --include='*.py' rook services server tools
grep -rnE 'add_argument\(' --include='*.py' rook services server tools
grep -rnE 'getSharedPreferences|putString|putBoolean|getString\("|getBoolean\("' android/app/src/main
grep -rnE 'buildConfigField|rookSetting' android/app/build.gradle
grep -rnoE '\.rook-band-worker[^"]*' --include='*.py' rook
```

Column key:

- **Where**: `flag` (CLI argument), `env` (environment variable), `file`
  (JSON/INI file on disk), `DB` (sqlite store edited through a UI or API),
  `UI:<page>` (dashboard page), `setup` (first-run `/setup` page), `build`
  (build-time property), `pref` (Android SharedPreferences).
- **Scope**: `hub` (one hub installation), `band` (one band on a hub), `worker`
  (one worker process or device), `user` (one account or one app user),
  `build` (fixed when an artifact is built).
- **Secret**: `yes` means the value grants access or decrypts traffic.
- **Restart**: `proc` means the process must restart to pick up a change.
  `live` means the change takes effect immediately. `worker` means a worker
  restart, which `rook_config_apply` triggers itself.

---

# Part 1 — Inventory

## 1.1 Hub dashboard (`rook-dashboard`, `python -m rook.remote.bootstrap`)

Entry point: `rook/remote/bootstrap.py:_cli_main`. The dashboard is also reached
through `rook dashboard` (`rook/__main__.py`) and `python -m rook.remote`.

| Name | Where | Default | Scope | Read by | Secret | Restart |
|---|---|---|---|---|---|---|
| `--bind` / `ROOK_BIND` | flag, env | `0.0.0.0` | hub | `bootstrap._cli_main` | no | proc |
| `--port` / `ROOK_PORT` | flag, env | `7005` | hub | `bootstrap._cli_main` | no | proc |
| `--domain` / `ROOK_DOMAIN` | flag, env; **overridden by** setup `pyz_domain` | `hub.example.com` | hub | `CombinedServer` (installer URLs, update URL) | no | proc (setup: live) |
| `--psk` / `ROOK_BAND_PSK` | flag, env; **overridden by** setup `band_psk` and the enrollment DB primary band | empty (then `/setup`) | band | `CombinedServer`, `setup_store`, `EnrollmentStore.import_config` | **yes** | proc (setup: live) |
| `--hub-public` / `ROOK_HUB_PUBLIC` | flag, env; **overridden by** setup `hub_public` | `hub.example.com:443` | hub (copied per band into the accounts DB) | `CombinedServer`, installers, `band_web.create_band` | no | proc (setup: live) |
| `--band-name` / `ROOK_BAND_NAME` | flag, env; **overridden by** setup `band_name` | `rook-band` | band | `CombinedServer` | no | proc (setup: live) |
| `--token` | flag only | empty | hub | `CombinedServer(auth_token=)` legacy exec-worker auth | **yes** | proc |
| `--web-user` / `ROOK_WEB_USER` | flag, env | empty (password only) | hub | `CombinedServer` login | no | proc |
| `--web-pass` / `ROOK_WEB_PASS` | flag, env | empty (refuses off-loopback) | hub | `CombinedServer` login, first local admin | **yes** | proc |
| `--insecure-no-auth` | flag only | off | hub | `_cli_main` guard | no | proc |
| `--hub-host` / `ROOK_HUB_HOST` | flag, env | `127.0.0.1` | hub | band client to relay | no | proc |
| `--hub-port` / `ROOK_HUB_PORT` | flag, env | `7474` | hub | band client to relay | no | proc |
| `ROOK_PUSH_UPDATES` | env | `1` | hub | `CombinedServer.start` (pushes signed manifests to outdated workers) | no | proc |
| `ROOK_PUBLIC_APK_SHA256` | env | empty (APK routes off) | hub | `/apk`, `/apk.json` allowlist | no | proc |
| `ROOK_CHAT_DB` | env | `$ROOK_DATA_DIR/chat.db`, else `/var/lib/rook-band-mcp/chat.db` | hub | `CombinedServer` chat panel | no | proc |
| `ROOK_ENROLLMENT_DB` | env | `enrollment.db` beside `setup.json` | hub | `remote/enrollment.py` (also used by the MCP server) | holds secrets | proc |
| `ROOK_SETUP_PATH` | env | `$ROOK_DATA_DIR/setup.json`, else `<repo>/data/setup.json` | hub | `setup_store.setup_path` | holds secrets | proc |
| `ROOK_GOOGLE_CLIENT_FILE` | env | empty (Google login off) | hub | `google_auth` | **yes** (client secret file) | proc |
| `ROOK_GOOGLE_ANDROID_CLIENT_FILE` | env | empty | hub | `google_auth` | no | proc |
| `ROOK_GOOGLE_WEB_LOGIN` | env | `1` | hub | `account_web.AccountWeb` | no | proc |
| `ROOK_TOKEN_ADMIN_URL` | env | `http://127.0.0.1:8765/tokens/account-api` | hub | `token_web` (proxy to MCP) | no | proc |
| `ROOK_KNOWLEDGE_ADMIN_URL` | env | `http://127.0.0.1:8765/knowledge/account-api` | hub | `knowledge_web` (proxy to MCP) | no | proc |
| `ROOK_GUIDANCE_ADMIN_URL` | env | `http://127.0.0.1:8765/guidance/account-api` | hub | `knowledge_web.GuidanceWeb` | no | proc |
| `ROOK_VAULT_ADMIN_URL` | env | `http://127.0.0.1:8765/vault/account-api` | hub | `knowledge_web.VaultWeb` | no | proc |
| `ROOK_WORK_IMPORT_WORKERS` | env | empty (all workers) | hub | `work_web` session import allowlist | no | proc |
| `ROOK_UPDATE_KEY` | env | `~/.config/rook/update-signing-key`, else `$ROOK_DATA_DIR/update-signing-key` | hub | `update_keys.key_path` | **yes** (path to signing key) | proc |
| `ROOK_DATA_DIR` | env | unset (each store keeps its legacy path); `/data` in the Docker image | hub | `rook/paths.py`, `setup_store`, `update_keys`, MCP, watchdog | no | proc |

## 1.2 Hub MCP server (`rook-mcp`, `python -m rook.band_mcp`)

Entry point: `rook/band_mcp/server.py:main`.

| Name | Where | Default | Scope | Read by | Secret | Restart |
|---|---|---|---|---|---|---|
| `--hub` / `ROOK_HUB` | flag, env | `127.0.0.1:7474` | hub | band client, WS bridge | no | proc |
| `--psk` / `ROOK_BAND_PSK` | flag, env (comma list = several bands) | **required** | band | `EnrollmentStore.import_config`, `WSBandBridge` (first PSK only) | **yes** | proc |
| `--bind` / `ROOK_MCP_BIND` | flag, env | `127.0.0.1:8765` | hub | uvicorn | no | proc |
| `--allowed-hosts` / `ROOK_ALLOWED_HOSTS` | flag, env | empty (loopback only) | hub | transport security | no | proc |
| `--public-url` / `ROOK_MCP_PUBLIC_URL` | flag, env | empty (OAuth shim off) | hub | resource metadata, `oauth_shim` | no | proc |
| `--admin-password` / `ROOK_MCP_AUTH_PASSWORD` | flag, env | empty | hub | `TokenStore` (`/tokens` page on the MCP port) | **yes** | proc |
| `--persist-path` / `ROOK_MCP_PERSIST` | flag, env | `$ROOK_DATA_DIR/oauth.json`, else `/var/lib/rook-band-mcp/oauth.json` | hub | `TokenStore`; its directory also holds the other stores unless `--journal-path` moves them | holds secrets | proc |
| `--static-token` / `ROOK_MCP_STATIC_TOKEN` | flag, env | empty | hub | `TokenStore`, `api_tokens_ui`, watchdog | **yes** | proc |
| `--journal-path` / `ROOK_MCP_JOURNAL` | flag, env | `journal.db` next to `--persist-path` | hub | `Journal`; **its directory decides** where `sessions.db`, `chat.db`, `console.db`, `guidance.db`, `vault.db`, `knowledge.db` go | no | proc |
| `-v` | flag | warning level | hub | logging | no | proc |
| `ROOK_KNOWLEDGE` | env | `0` (off) | hub | `server._build` (knowledge wiki, tasks, hygiene) | no | proc |
| `ROOK_KNOWLEDGE_DB` | env | `knowledge.db` beside the journal | hub | `KnowledgeService` | no | proc |
| `ROOK_EMBED_URL` | env | empty (keyword search only) | hub | `knowledge/search.py` | no | proc |
| `ROOK_EMBED_MODEL` | env | `sentence-transformers/all-MiniLM-L6-v2` | hub | `knowledge/search.py` | no | proc |

Stores in the MCP data directory, managed through UI pages (see 1.5): `oauth.json`
(tokens), `vault.db`, `guidance.db`, `knowledge.db`, `chat.db`, `console.db`,
`sessions.db`, `journal.db`.

## 1.3 Relay (`telesthete-hub`) and its installer

The relay is a separate Rust binary. Rook only sets its environment in
`compose.yaml` and in the `/hub` installer script (`bootstrap._HUB_INSTALL_SCRIPT`).

| Name | Where | Default | Scope | Read by | Secret | Restart |
|---|---|---|---|---|---|---|
| `HUB_BIND` | env (compose, installer wizard) | `0.0.0.0:7474` in compose | hub | relay | no | proc |
| `HUB_PEER_TTL_SECS` | env | `60` in compose (relay default 15) | hub | relay | no | proc |
| `HUB_PRUNE_SECS` | env | `10` in compose | hub | relay | no | proc |
| `RUST_LOG` | env | `info` | hub | relay | no | proc |
| `HUB_USER`, `HUB_PREFIX`, `HUB_YES`, `HUB_SRC_REPO` | env (installer only) | prompted / `/usr/local/bin` / off / public repo | hub | `/hub` install script | no | n/a |

## 1.4 Watchdog (`rook/band_mcp/watchdog.py`)

Run from cron or a timer, once on the hub and once off-hub.

| Name | Where | Default | Scope | Read by | Secret | Restart |
|---|---|---|---|---|---|---|
| `--mode` | flag | `hub` | hub | watchdog | no | next run |
| `--state` / `ROOK_WATCHDOG_STATE` | flag, env | `/var/lib/rook-watchdog/state.json` | hub | watchdog | no | next run |
| `--test-alert` | flag | off | hub | watchdog | no | n/a |
| `ROOK_WATCHDOG_MCP_URL` | env | `http://127.0.0.1:8765` (hub); required for `remote` | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_HOST` | env | required in hub mode | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_NAME` | env | `hub` / `external` | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_EVICT_PER_MIN` | env | `10` | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_MIN_MEM_MB` | env | `80` | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_REPEAT_MIN` | env | `60` | hub | watchdog | no | next run |
| `ROOK_WATCHDOG_TELEGRAM_TOKEN` | env | empty | hub | watchdog | **yes** | next run |
| `ROOK_WATCHDOG_TELEGRAM_CHAT` | env | empty | hub | watchdog | no | next run |
| `ROOK_JOURNAL_DB` | env | `$ROOK_DATA_DIR/journal.db` | hub | watchdog | no | next run |
| `ROOK_MCP_STATIC_TOKEN` | env (shared with MCP) | empty | hub | watchdog probe | **yes** | next run |

## 1.5 Dashboard pages and the setup page (UI-managed state)

The dashboard (`rook/web/index.html`) has these views: Workers, Account &
access, API tokens, Bands, Chat, Knowledge, Secrets, Agent instructions, Work,
Sessions, Install. The account pages are served at `/account*`
(`rook/remote/account_web.py`, `band_web.py`, `token_web.py`, `knowledge_web.py`,
`work_web.py`). Several of them proxy to account-API routes on the MCP port
(`band_mcp/account_tokens.py`, `guidance_web.py`, `vault_web.py`).

| Setting | Where | Default | Scope | Stored in / read by | Secret | Restart |
|---|---|---|---|---|---|---|
| Band name, public hub address, installer domain, band PSK | setup (`/setup`, only while unconfigured) | name `rook-band`, PSK suggested by `psk.generate_psk` | band / hub | `setup.json` via `setup_store`; wins over flags/env at start | PSK **yes** | live |
| Extra known bands (`bands`) | setup.json (`save_bands`) | empty | hub | `setup_store.load_bands` | **yes** (PSKs) | live |
| Deauthed workers (`bans`) | UI:Workers (deauth), `/api/band/ban` | empty | band | `setup.json` via `save_bans` | no | live |
| Bands: create, rename, delete, migrate workers | UI:Bands | none | band | accounts/enrollment DB (`enrollment.db`) | no | live |
| Band PSK rotate / revoke, pairing codes, invites, member roles | UI:Account, UI:API tokens (`/tokens/bands/rotate`, `/tokens/pairing`) | pairing code TTL 300 s (hardcoded) | band | enrollment DB, `EnrollmentStore` | **yes** | live |
| Profile name, avatar source, local password, linked Google login, devices | UI:Account | from Google or username | user | accounts DB (`users`, `devices`) | password **yes** | live |
| Operator account, admin flag | `ROOK_WEB_USER`/`ROOK_WEB_PASS` bootstrap an operator account (admin, owner of every band); changing them updates its credentials and ends its sessions | none | user | accounts DB `users.admin`, `account_settings.bootstrap_user` (`AccountStore.bootstrap`) | password **yes** | proc |
| API tokens (mint, revoke, label, avatar) | UI:API tokens, and `/tokens` on the MCP port (admin password) | none | user / hub | `oauth.json` (`TokenStore`) | **yes** | live |
| Vault secrets | UI:Secrets | none | hub | `vault.db` | **yes** | live |
| Agent instructions (`server`, `hygiene`, `tool:<name>`, `cap:<prefix>`) | UI:Agent instructions | `guidance.DEFAULTS` | hub | `guidance.db` (overrides + attributed history) | no | live |
| Knowledge pages, tasks, projects | UI:Knowledge, UI:Work, MCP tools | none | band | `knowledge.db` | no | live (content, not configuration) |
| Worker description (280 chars) | UI:Workers (`worker.description_set`), MCP | empty | worker | worker `~/.rook-band-worker/metadata.json` | no | live |
| Worker restart / update / deauth | UI:Workers, TUI | none | worker | caps on the worker | no | actions, not settings |

## 1.6 Worker (`rook-worker`, `rook worker`, `python -m rook.worker`, the `.pyz` bundle)

Entry point: `rook/worker/cli.py:main`.

### Flags and environment

| Name | Where | Default | Scope | Read by | Secret | Restart |
|---|---|---|---|---|---|---|
| `--hub` / `ROOK_HUB` | flag, env; **overridden by** enrollment and pushed `config.json` `hub` | `127.0.0.1:7474` | worker | `cli.main` | no | proc |
| `--psk` / `ROOK_BAND_PSK` | flag, env; **overridden by** enrollment and pushed `psk` | **required** | worker | `cli.main` | **yes** | proc |
| `--enroll URL`, `--pair-code`, `--enrolled` | flag | off | worker | `worker/enroll.py` | pair code **yes** | proc |
| `--name` | flag; **overridden by** pushed `name` | hostname | worker | `Worker` | no | proc |
| `--announce-interval` | flag; **overridden by** pushed `announce_interval` | `30` s | worker | `Worker` | no | proc |
| `--bind-port` | flag | `0` (ephemeral) | worker | transport | no | proc |
| `--keepalive` | flag | `20` s | worker | transport | no | proc |
| `--ws` | flag | off (UDP) | worker | transport | no | proc |
| `--enable` | flag | all built-in plugins | worker | `load_plugins` | no | proc |
| `--update-url` / `ROOK_UPDATE_URL` | flag, env | empty (no OTA) | worker | `plugins/selfupdate` | no | proc |
| `-v` | flag; raised by pushed `log_level` | warning | worker | logging | no | proc |
| `--version`, `--selftest`, `--install-cli`, `--cli` | flag | n/a | worker | `cli.main` | no | n/a |
| `ROOK_UPDATE_PUBKEY` | env; stamped into hub-built bundles (`_update_pubkey.py`) | bundled key, else none trusted | worker | `_update_verify` | no | proc |
| `ROOK_UPDATE_POLL` | env | `300` s | worker | `selfupdate` | no | proc |
| `ROOK_ENROLLMENT_FILE` | env | `~/.rook-band-worker/enrollment.json` | worker | `enroll.py` | holds secrets | proc |
| `ROOK_WORK_DB` | env | `~/.rook-band-worker/work.sqlite3` | worker | `plugins/work`, `session_messages` | no | proc |
| `ROOK_CODEX_CONTROL_SOCKET` | env | `$CODEX_HOME/app-server-control/app-server-control.sock` | worker | `codex_input` | no | per call |
| `CODEX_HOME` | env (third-party) | `~/.codex` | worker | `codex_history`, `codex_input` | no | per call |

### Plugin environment gates (turn a plugin on when set)

| Name | Default | Read by | Secret | Notes |
|---|---|---|---|---|
| `ROOK_WAKE_CMD` | unset (plugin off) | `plugins/wake` | no | Command template; `{prompt_file}` placeholder |
| `ROOK_WAKE_AGENT` | unset | `plugins/wake` | no | Identity this host wakes |
| `ROOK_MEMORY_VAULT` | unset (plugin off) | `plugins/memory` | no | Directory of markdown notes |
| `ROOK_DONGLE_PORT` | unset (plugin off) | `plugins/dongle` | no | Serial device |
| `CEC_HOST`, `CEC_PORT`, `CEC_TIMEOUT` | unset (plugin off), `9526`, `5` | `plugins/cec` | no | |
| `PIKVM_URL`, `PIKVM_USER`, `PIKVM_PASS`, `PIKVM_INSECURE` | `https://localhost`, `admin`, `admin`, `1` | `plugins/pikvm` | `PIKVM_PASS` **yes** | Default password and TLS-verify off |

Not settings (listed so nobody adds them to a schema): `ROOK_WAKE_PROMPT`,
`ROOK_WAKE_ROOM`, `ROOK_WAKE_PROMPT_FILE` are **exported to** the woken agent;
`DISPLAY`, `WAYLAND_DISPLAY`, `PREFIX`, `TERMUX_VERSION`, `ANDROID_ARGUMENT`,
`APPDATA`, `USERPROFILE`, `ProgramData` are platform detection.

### Worker files under `~/.rook-band-worker/`

| File | Written by | Holds | Scope | Secret | Restart |
|---|---|---|---|---|---|
| `config.json` (+ `.prev`, `.pending`) | `worker.config_apply` (MCP `rook_config_apply`) | allowlist `name`, `announce_interval`, `log_level`, `hub`, `psk`, `env` (any variable), `epoch` | worker | `psk`, and `env` values can be | worker (automatic, commit-confirmed) |
| `plugins.json` | `worker.plugin.enable/disable` (TUI, MCP) | `{"disabled": [module, ...]}` | worker | no | live |
| `custom_caps.json` | `customcap.add/remove` (TUI, MCP) | `{name: {command, args, ...}}`, registered as `cmd.<name>` | worker | commands may embed secrets | live |
| `metadata.json` | `worker.description_set` | description | worker | no | live |
| `enrollment.json` | `--enroll` | enrolled bands (hub + PSK), active band, `auto_start` | worker | **yes** | proc |
| `hold` | `worker.hold` / release | presence pins the build (no auto-update) | worker | no | live |
| `banned` | signed `worker.deauth` | presence keeps the worker off the band | worker | no | proc |
| `worker_id` | first start | stable identity | worker | no | n/a |
| `update_state.json`, `band-worker.pyz(.prev)` | selfupdate | OTA state | worker | no | n/a |

## 1.7 Installers (served by the dashboard)

The dashboard renders these scripts with `{hub_public}`, `{band_psk}`,
`{domain}` substituted (`bootstrap.py`).

| Name | Where | Default | Scope | Secret | Notes |
|---|---|---|---|---|---|
| `ROOK_BASE` | env of the install shell | the serving hub URL | worker | no | download base |
| `ROOK_BIN` | env | `~/.local/bin` | worker | no | CLI destination |
| `ROOK_INSTALL` | env / first arg | prompt (`worker`, `cli`, `both`) | worker | no | |
| `ROOK_JOIN_CODE` | env | templated | worker | **yes** | |
| `ROOK_WEB_URL`, `ROOK_WEB_USER`, `ROOK_WEB_PASS` | env (Windows CLI installer) | `https://<domain>`, a hardcoded username, empty | user | pass **yes** | written to `%USERPROFILE%\.config\rook\band.conf` |
| worker command line | templated into the systemd unit / scheduled task | `--hub {hub_public} --ws --psk {band_psk} --name ... --update-url https://{domain}/band-worker.json` | worker | **yes** (PSK) | enrolled installs use `--enrolled --ws` instead |

## 1.8 Terminal dashboard (`rook`, `rook band`, `rook/cli/band_tui.py`)

| Name | Where | Default | Scope | Secret | Restart |
|---|---|---|---|---|---|
| `--url` / `ROOK_WEB_URL` / `band.conf url` | flag > env > file | `http://127.0.0.1:7005` | user | no | proc |
| `--user` / `ROOK_WEB_USER` / `band.conf user` | flag > env > file | `admin` | user | no | proc |
| `--pass` / `ROOK_WEB_PASS` / `band.conf pass` | flag > env > file > prompt | prompt | user | **yes** | proc |
| `--reset` | flag | off | user | no | n/a |
| `~/.config/rook/band.conf` | file (saved after first login) | none | user | **yes** (plaintext password) | proc |

Other `rook` subcommands (`sessions`, `history`, `tmux`) take only per-command
arguments (`--json`, `--project`, `-d`, `-n`) and read `CODEX_HOME`/`APPDATA` for
platform paths. They carry no persistent settings.

## 1.9 Voice service (`services/voice`)

Run as `python -m services.voice.server`. Configuration is env only.

| Name | Default | Scope | Read by | Secret |
|---|---|---|---|---|
| `VOICE_BIND`, `VOICE_PORT` | `127.0.0.1`, `8900` | hub (service) | `server.py` | no |
| `VOICE_TLS_KEY`, `VOICE_TLS_CERT` | unset | service | `server.py` | key path |
| `VOICE_TOKEN` | empty | service | `server.py`, `smoke.py` | **yes** |
| `VOICE_ALLOW_ANONYMOUS` | unset (refuse when no token) | service | `server.py` | no |
| `VOICE_MODEL_DIR` | `.` (server) / module dir (providers) | service | `server.py`, `providers.py` | no |
| `VOICE_STATE_DB` | `$VOICE_MODEL_DIR/voice-state.sqlite3` | service | `server.py` | no |
| `VOICE` | `af_heart` | service (default voice) | `providers.py` | no |
| `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE` | `small.en`, `cpu`, `int8` | service | `providers.py` | no |
| `MIN_SPEECH_MS`, `MIN_RMS`, `MAX_NO_SPEECH`, `MIN_LOGPROB` | `450`, `0.008`, `0.6`, `-1.0` | service | `providers.py` | no |
| `DIRECT_TOOL_BUDGET` | `1` | service | `providers.py` | no |
| `VLLM_URL`, `VLLM_MODEL` | `http://127.0.0.1:1234/v1/chat/completions`, a specific model id | service | `providers.py` | no |
| `ACP_HOST`, `ACP_PORT`, `ACP_AUTO_APPROVE` | `127.0.0.1`, `9200`, `1` | service | `providers.py`, `acp.py` | no |
| `ROOK_MCP_URL`, `ROOK_MCP_TOKEN` | `http://127.0.0.1:8765/mcp`, empty | service | `rookmcp.py` | token **yes** |
| `VOICE_SMOKE_URL` | `ws://127.0.0.1:8901/ws` | test tool | `smoke.py` | no |

All need a process restart. The service has no UI, no DB-backed settings and no
hub integration beyond its MCP token.

## 1.10 Decision engine (not on `beta`)

The shadow decision engine lives on `feature/voice-decision-shadow` (not merged).
It reads `DECISION_URL` (empty = off), `DECISION_TIMEOUT_MS` (`150`),
`DECISION_ASSISTANT_NAMES` (`rook,assistant`), `DECISION_RECENT_SPEECH_SECONDS`
(`15`), `DECISION_SILENCE_SECONDS` (`15`), `DECISION_RAW_RETENTION_DAYS` (`30`).
On `beta` the only trace is the `cap:cmd.decide-` guidance slot, which assumes
the engine is exposed as a custom cap. The Android client opts in per user with
`show_thinking` (see 1.12). Part 3 uses these knobs for the decision-plugin
wireframe.

## 1.11 Knowledge embeddings service (`services/knowledge-embeddings`)

No settings. The model id (`sentence-transformers/all-MiniLM-L6-v2`), port
`8768`, bind `0.0.0.0`, cache `/models` and `threads=2` are hardcoded in
`server.py`. The hub's `ROOK_EMBED_MODEL` must match the model by hand.

## 1.12 Android app (`android/`)

### Build-time (`android/app/build.gradle`, from `-P`, env, or untracked `android/rook.properties`)

| Name | Default | Scope | Secret | Notes |
|---|---|---|---|---|
| `ROOK_SERVER` | `https://rook.example.com` | build | no | Account site; the only accepted update origin (`ApkUpdater`) |
| `ROOK_DEFAULT_HUB` | `hub.example.com:443` | build | no | Seeds the `hub` pref |
| `ROOK_DEFAULT_PSK` | empty; build **fails** if set | build | yes | Guarded |
| `ROOK_VOICE_URL` | `wss://voice.example.com/ws` | build | no | Seeds `voice_url` |
| `ROOK_VOICE_TOKEN` | empty; build **fails** if set | build | yes | Guarded |
| `ROOK_GOOGLE_WEB_CLIENT_ID` | empty (Google sign-in hidden) | build | no | |
| `ROOK_WAKE_MODEL`, `ROOK_WAKE_PHRASE` | empty (tap-to-talk only) | build | no | Wake *word*, unrelated to worker `ROOK_WAKE_CMD` |
| `ROOK_ASSISTANT_NAME` | `Rook` | build | no | |
| `ROOK_APK_VERSION_CODE`, `ROOK_APK_VERSION_NAME` | `10`, `0.4.6` | build | no | `-P` only |

### Runtime (`SharedPreferences("rook")`, edited in `SettingsActivity`)

| Key | Default | Scope | Read by | Secret |
|---|---|---|---|---|
| `account_server` | `ROOK_SERVER` | user | `SettingsActivity` | no |
| `band_configurations` | `[]` (from Google enrollment) | user | `SettingsActivity` | **yes** (contains PSKs) |
| `hub`, `psk`, `name` | build defaults / device model | worker (device) | `WorkerService`, `BootReceiver`, `MainActivity` | `psk` **yes** |
| `band_id`, `band_epoch` | from chosen band | worker | `SettingsActivity` | no |
| `autostart` | `false` | worker | `BootReceiver`, `MainActivity` | no |
| `apk_auto_update` | `true` | worker | `ApkUpdater` | no |
| `voice_url`, `voice_token`, `voice_insecure` | build default, empty, `false` | user | `VoiceService`, `VoiceClient`, `MainActivity` | token **yes**; insecure disables TLS checks |
| `voice_choice` | server catalog default | user | `VoiceClient` | no |
| `show_thinking` | `false` | user | `ChatAdapter`, `VoiceClient` (opts into decision events) | no |
| `wake_enabled` | `true` | user | `VoiceService`, `MainActivity` | no |
| `voice_conversation_<hash>` | generated UUID | user | `VoiceClient` | no (state, not a setting) |

`SharedPreferences("rook_apk_updates")` holds updater state only.

## 1.13 Build and release tooling

| Name | Where | Read by | Secret |
|---|---|---|---|
| `ROOK_PUBLIC_BASE` | env | `remote/build_band_worker.py` (manifest `url`), `android/build_apk_manifest.py` | no |
| `ROOK_SERVER` | env / `--server` | `remote/migrate.py` (required) | no |
| `rook.remote.migrate` flags `--band --workers --completion-report --owner --hub-host --hub-port --ws --window --execute --resume` | flag | one-shot migration tool | no |
| `rook/remote/update_keys.py` subcommands | argv | key management | no |

Out of scope (dev tools or separate products): `tools/diagnostics/*`
(`ROOK_PROBE_WORKER`, `--seconds`, `--output`, …), `server/rook_kvm/*` and
`server/validate_dongle.py` (`ROOK_BRIDGE_HOST`, `ROOK_BRIDGE_PORT`,
`ROOK_ADMIN_USER`, `ROOK_ADMIN_PASS`, `ROOK_FIRMWARE_BIN`; `ROOK_EOF`,
`ROOK_END__`, `ROOK_FILE` there are protocol markers, not settings),
`rook/remote/worker.py` (legacy exec worker: `--name --server --token
--no-service --no-banner`).

## 1.14 Deployment files

- `.env.example`: documents 20 variables for dashboard, MCP, worker and TUI.
- `compose.yaml`: passes 9 dashboard and 7 MCP variables, pins
  `--hub-host relay --hub-port 7474` and `--hub relay:7474 --bind 0.0.0.0:8765`
  as command-line flags, and requires `ROOK_WEB_PASS` and `ROOK_BAND_PSK`.
- `Dockerfile`: sets `ROOK_DATA_DIR=/data`.
- `scripts/local-hub.sh`: reads `ROOK_DATA_DIR`, `BIND`, `RELAY_PORT`,
  `DASHBOARD_PORT`, `MCP_PORT`, `PYTHON`; generates `ROOK_BAND_PSK`,
  `ROOK_WEB_PASS`, `ROOK_MCP_STATIC_TOKEN`, `ROOK_MCP_AUTH_PASSWORD` into
  `$ROOK_DATA_DIR/quickstart.env` (mode 600).
- `pikvm/rook-worker.env.example` + `pikvm/rook-worker.service`:
  `ROOK_HUB`, `ROOK_BAND_PSK`, `ROOK_WORKER_NAME`, `PIKVM_*`, expanded into
  `ExecStart` flags.
- `README.md`: a table of 13 variables.

## 1.15 Hardcoded values that operators have reason to change

These have no knob today. They are candidates for schema entries, not
proposals to expose all of them.

| Constant | Value | Module |
|---|---|---|
| `WORKER_STALE_SECS` | 90 | `band_mcp/client.py` |
| `IDLE_SECONDS` (hygiene nudge) | 1800 | `band_mcp/hygiene.py` |
| `_MAX_ROWS` (journal) | 20000 | `band_mcp/journal.py` |
| `MAX_ROOMS`, `MAX_TOTAL_BYTES` (console archive) | 1000, 512 MiB | `band_mcp/console_rooms.py` |
| `SESSION_IDLE_SECONDS`, `MAX_SESSIONS` (MCP HTTP) | 300, 128 | `band_mcp/http_sessions.py` |
| `_ACCESS_TTL` (OAuth shim token) | 1 year | `band_mcp/oauth_shim.py` |
| `_ADMIN_SESSION_TTL` (`/tokens`) | 1800 | `band_mcp/tokens.py` |
| `PAIRING_TTL` | 300 | `remote/enrollment.py` |
| `LIMITS` (guidance length) | server 6000, hygiene 2000 | `band_mcp/guidance.py` |
| `_HEALTH_SECS`, `_MAX_BOOT_ATTEMPTS` (OTA) | 60, 3 | `worker/plugins/selfupdate.py` |
| `MAX_SESSIONS`, `DONE_TTL_SECS` (proc) | 16, 900 | `worker/plugins/proc.py` |
| `_REWAKE_SECS` | 45 | `worker/plugins/wake.py` |

---

# Part 2 — Problems

Numbered so the migration plan and follow-up tasks can refer to them.

### Precedence and conflicts

- **P1. The setup file silently beats the environment.** `CombinedServer.__init__`
  loads `setup.json` after the flags and overwrites `band_psk`, `hub_public`,
  `domain` and `band_name`. The primary band in `enrollment.db` then overrides the
  PSK again. `_cli_main` writes flags/env into `setup.json` only when it is
  unconfigured. So after the first start, editing `ROOK_BAND_PSK`,
  `ROOK_HUB_PUBLIC`, `ROOK_DOMAIN` or `ROOK_BAND_NAME` in `.env` or compose has no
  effect, and nothing says so. This is the reverse of every other setting, where
  the environment wins.
- **P2. The worker has three sources for hub and PSK.** Flag/env, then
  `enrollment.json` (if `--enrolled` or `auto_start`), then pushed `config.json`
  (except when enrolled). `cli.py` logs a warning when the result differs from the
  command line. The operator cannot see or change this from the dashboard.
- **P3. The MCP server and the dashboard disagree on the PSK source.** The
  dashboard can start with no PSK and take it from `/setup`. The MCP server refuses
  to start without `--psk`/`ROOK_BAND_PSK`, even though it then syncs bands from the
  shared `enrollment.db`. `.env.example` says an empty PSK is shared "through
  ROOK_DATA_DIR", which is only true for the dashboard. `WSBandBridge` uses only
  the first PSK.
- **P4. The chat database path can differ.** The MCP server puts `chat.db` beside
  the journal (`--journal-path` directory). The dashboard uses `ROOK_CHAT_DB`, else
  `$ROOK_DATA_DIR/chat.db`, else `/var/lib/rook-band-mcp/chat.db`. With
  `--journal-path` or `--persist-path` set outside `ROOK_DATA_DIR`, the dashboard
  chat panel and MCP agents use different rooms. The same applies to
  `ROOK_JOURNAL_DB` in the watchdog.

### Duplicates

- **P5. One concept, several shapes.** The relay address is `ROOK_HUB`
  (`host:port`, MCP and worker), `ROOK_HUB_HOST` + `ROOK_HUB_PORT` (dashboard) and,
  for the address workers dial, `ROOK_HUB_PUBLIC`. The listen address is
  `ROOK_BIND` + `ROOK_PORT` (dashboard) but `ROOK_MCP_BIND` as `host:port` (MCP).
  The installer domain is `--domain`/`ROOK_DOMAIN` on the command line and
  `pyz_domain` in `setup.json`.
- **P6. Three admin credentials.** `ROOK_WEB_PASS` (dashboard), the accounts
  database (local and Google logins, `users.admin`), and `ROOK_MCP_AUTH_PASSWORD`
  (the `/tokens` page on the MCP port). The dashboard also serves `/tokens` and
  `/account/tokens` by proxying to the MCP account API, so the same page exists
  twice with different authentication.
- **P7. `ROOK_WEB_USER`/`ROOK_WEB_PASS` mean two things.** On the hub they
  configure the server login; for the TUI and the Windows installer they are the
  client's credentials for a remote dashboard. The TUI defaults the user to
  `admin`; the Windows installer defaults it to a hardcoded username (it should be
  generic, see P18).
- **P8. `ROOK_MCP_STATIC_TOKEN` is shared by the MCP server and the watchdog**,
  so the watchdog needs the hub's most powerful token instead of a scoped one.
- **P9. The embeddings model is named twice** (`ROOK_EMBED_MODEL` on the hub, a
  constant in `services/knowledge-embeddings/server.py`) and must be kept equal by
  hand.
- **P10. "Wake" means two things.** `ROOK_WAKE_CMD`/`ROOK_WAKE_AGENT` (worker:
  start an agent for a chat room) and Android `ROOK_WAKE_MODEL`/`wake_enabled`
  (hotword). A schema needs distinct names.

### Undocumented knobs

- **P11.** Not in `.env.example` or `README.md`: `ROOK_BAND_NAME`,
  `ROOK_PUSH_UPDATES`, `ROOK_PUBLIC_APK_SHA256`, `ROOK_CHAT_DB`,
  `ROOK_ENROLLMENT_DB`, `ROOK_SETUP_PATH`, `ROOK_MCP_PERSIST`, `ROOK_MCP_JOURNAL`,
  `ROOK_KNOWLEDGE_DB`, `ROOK_EMBED_URL`, `ROOK_EMBED_MODEL`, the four
  `ROOK_*_ADMIN_URL` proxies, `ROOK_WORK_IMPORT_WORKERS`, `ROOK_GOOGLE_*`,
  `ROOK_UPDATE_KEY`, `ROOK_UPDATE_POLL`, `ROOK_ENROLLMENT_FILE`, `ROOK_WORK_DB`,
  `ROOK_CODEX_CONTROL_SOCKET`, every plugin gate (`ROOK_WAKE_*`,
  `ROOK_MEMORY_VAULT`, `ROOK_DONGLE_PORT`, `CEC_*`), and all `ROOK_WATCHDOG_*`
  (documented only in the module docstring). Voice settings are documented only in
  `services/voice/README.md`, and not all of them (`MIN_*`, `MAX_NO_SPEECH`,
  `DIRECT_TOOL_BUDGET`, `VOICE` are missing).
- **P12. Inconsistent defaults.** The voice smoke test defaults to port 8901; the
  server defaults to 8900. `.env.example` sets `ROOK_DOMAIN=localhost:7005` and
  `ROOK_HUB_PUBLIC=localhost:8765`; the code defaults are `hub.example.com` and
  `hub.example.com:443`. The PiKVM plugin defaults to `admin`/`admin` with TLS
  verification off.

### Only changeable by editing files, unit arguments or the command line

- **P13. Worker plugin settings need a restart through an untyped `env` map.**
  `rook_config_apply` can set any environment variable on a worker, which is the
  only remote way to turn on wake, memory, CEC or the dongle. It restarts the
  worker every time, has no schema, no validation, and no UI: the dashboard has no
  caller for `worker.config_*`.
- **P14. Plugin enable/disable and custom caps have no dashboard UI.** They are
  reachable from the TUI and MCP only. `--enable` in unit files (for example the
  PiKVM unit's `--enable info,pikvm`) and `plugins.json` can disagree: a plugin
  outside `--enable` can be enabled at runtime, but `plugins.json` records only
  disables, so it disappears again at the next restart, and nothing shows why.
- **P15. Hub feature switches need a restart and a shell.** `ROOK_KNOWLEDGE`,
  `ROOK_PUSH_UPDATES`, Google login, the embeddings URL, the watchdog and all voice
  settings can only be changed by editing the environment of a service. After
  first run, the band name, installer domain and public hub address have no page
  at all: `/setup` is served only while unconfigured, and `band_web` has only
  create/rename/delete/migrate.
- **P16. Relay settings live only in compose or the relay unit.** The relay TTL
  must be ≥ 60 s (README) because peers keep alive every 20 s; nothing checks this.

### Secrets

- **P17. Secrets on command lines.** The worker installers template
  `--psk {band_psk}` into the systemd `ExecStart` and the Windows scheduled task
  arguments; `pikvm/rook-worker.service` expands `--psk ${ROOK_BAND_PSK}`;
  `scripts/local-hub.sh` prints a worker command with `--psk`; the legacy
  `remote/worker.py` unit uses `--token`. The PSK is then visible in `ps` and in
  unit files. The README tells operators the opposite ("keep them out of process
  arguments"). Enrolled installs (`--enrolled`) avoid this.
- **P18. Secrets and personal defaults in plaintext files.** `setup.json` (PSKs,
  mode 600), `enrollment.json`, `~/.config/rook/band.conf` (dashboard password),
  worker `config.json` (`psk`, and any secret passed in `env`), Android
  `SharedPreferences` (`psk`, `voice_token`, `band_configurations`). The Windows
  installer's default username should be a generic value.
- **P19. Secrets can leak through read paths.** `worker.config_get` returns the
  unfiltered `config.json`, including `psk` and every `env` value;
  `rook_config_apply` echoes that config in its final reply. `shell.env.list`
  returns the worker's whole environment, including plugin secrets (such as
  `PIKVM_PASS`) that were pushed through `env`.
- **P20. No attribution or history outside guidance.** Guidance overrides keep an
  attributed history. Setup changes, band renames, plugin toggles, custom caps and
  pushed config are not recorded as settings changes. Band actions leave only the
  resulting state; config pushes appear in the call journal at most.

### Scope confusion

- **P21. Per-user preferences exist only on the phone.** Voice choice, "show
  thinking" and wake are device preferences on Android. The server-side default
  voice (`VOICE`) is a process environment variable. A user's preferences do not
  follow them between devices, and the hub cannot set a default.
- **P22. Band vs hub is implicit.** `hub_public` is a hub value copied into each
  band row at creation; `band_name` in `setup.json` names only the primary band,
  while other band names live in the accounts DB.

---

# Part 3 — Proposal

## 3.1 Principles

1. **One registry.** Every setting is declared once, in code, by the plugin that
   owns it (core counts as a plugin). The UI, the environment mapping,
   validation, documentation and the MCP surface are generated from that
   declaration. Nothing reads `os.environ` for configuration outside the
   settings layer.
2. **Visible precedence.** For every setting the UI shows the effective value,
   where it came from, and what it would be without that source.
3. **The environment wins, and says so.** Values set by env/flag are shown
   read-only with the variable name. This keeps compose and systemd deployments
   declarative and fixes P1 without surprising anyone who uses the UI.
4. **Secrets go to the vault.** The setting stores a reference; values are
   write-only in the UI and never echoed by `get` calls or the journal.
5. **Every change is attributed.** Who (account user or API token `agent_id`),
   when, old and new value (secrets as fingerprints), and why (optional note).
6. **Risk drives the apply path.** Cosmetic settings apply live. Settings that
   can strand a worker go through the existing commit-confirmed epoch
   (`wconfig`), per `DESIGN-band-services.md` §1.

## 3.2 The `setting()` declaration

The plugin API is being designed in `rook-beta-plugin-api` concurrently. This
is the shape this proposal assumes; the fields, not the syntax, matter.

```python
class VoicePlugin(Plugin):
    name = "voice"

    whisper_model = setting(
        str, default="small.en", scope="hub",
        label="Speech recognition model",
        help="faster-whisper model name.",
        choices=["tiny.en", "base.en", "small.en", "medium.en"],
        group="Recognition",
        apply="reload",                 # live | reload | restart | risky
        env="WHISPER_MODEL",            # legacy alias; ROOK_VOICE_WHISPER_MODEL is automatic
    )
    token = setting(
        str, secret=True, scope="hub", label="Client bearer token",
        group="Access", apply="live", env="VOICE_TOKEN",
    )
    default_voice = setting(
        str, default="af_heart", scope="user", label="Voice",
        choices_from="voice.voices",    # cap that lists valid values
        group="Speech",
    )
```

Fields:

| Field | Meaning |
|---|---|
| type | `bool`, `int`, `float`, `str`, `enum`, `list[str]`, `duration`, `bytes`, `url`, `hostport`, `path`, `json` |
| `default` | built-in value; may be a callable (for example "hostname") |
| `scope` | `hub`, `band`, `worker`, `user`; see 3.3 |
| `overridable` | lower scopes that may override (e.g. a `band` setting with `overridable=("worker",)`) |
| `secret` | value lives in the vault; UI write-only |
| `env` | legacy variable names accepted as aliases, in order; the canonical name `ROOK_<PLUGIN>_<NAME>` is always accepted |
| `flag` | optional CLI flag for bootstrap settings |
| `label`, `help`, `group`, `order` | UI text and placement |
| `choices`, `choices_from`, `min`, `max`, `pattern`, `validate` | validation (3.6) |
| `apply` | `live` (callback on change), `reload` (plugin restart), `restart` (process), `risky` (commit-confirmed worker restart) |
| `bootstrap` | needed before the settings store is reachable (listen address, data dir, relay address, PSK on a worker); env/flag/file only, shown read-only in the UI |
| `advanced` | hidden behind "Show advanced" |
| `deprecated` | replacement key and removal version |

Keys are `<plugin>.<name>`: `core.dashboard.port`, `voice.whisper_model`,
`wake.command`. The canonical env name is derived:
`ROOK_` + key upper-cased with dots as underscores (`ROOK_VOICE_WHISPER_MODEL`).

## 3.3 Scopes and precedence

```
built-in default
  < hub value          (settings DB, scope hub)
  < band value         (settings DB, scope band)
  < worker override    (settings DB, scope worker, if overridable)
  < user preference    (settings DB, scope user, only for user-scoped keys)
  < file               (worker config.json / hub settings file, legacy only)
  < environment / flag (process env, CLI flag)
```

- A key has one home scope. Lower scopes can override only if the key allows it.
  For example `wake.command` is `worker` scoped, `core.worker.announce_interval`
  is `band` scoped and overridable per worker, `voice.default_voice` is `hub`
  scoped with a `user` preference.
- **Environment and flags win and lock the field.** The UI shows the variable
  name and the value the store holds underneath, so removing the variable has a
  predictable result.
- **Files** are a migration crutch (3.9): `setup.json` and worker `config.json`
  are read as a source until imported, then retired.
- **User preferences** are stored per account and delivered to clients (Android,
  TUI, dashboard) with the session. A device may keep a local override for
  device-bound values (microphone, wake word); the app shows it as "this device".

Resolution runs on the hub for hub/band/user scopes. For worker scope the hub
computes the worker's effective map and delivers it (3.7); the worker then layers
its own environment on top and reports back which keys its environment locked,
so the hub UI can show the lock.

## 3.4 Storage, history and API

Hub `settings.db` in `ROOK_DATA_DIR`:

```
settings(key, scope, target, value_json, secret_ref, rev, updated_by, updated_at)
  -- target: '' for hub, band_id, worker name, or user id
history(id, key, scope, target, old_json, new_json, actor_kind, actor_id,
        actor_label, note, at, source)   -- source: ui | mcp | import | api
```

- Secret values never enter `settings.db`: `secret_ref` names a vault entry, and
  history stores a fingerprint (first 8 hex of SHA-256) so a change is visible
  without revealing the value.
- The accounts DB already has an `account_settings (key, value)` table, used only
  for `bootstrap_user`. It has no scope, type or history columns, so a separate
  `settings.db` is proposed rather than extending it.
- The guidance store keeps its own table but gains the same history shape;
  guidance slots are listed in the Settings area under "Agent instructions".
- Caps on the hub worker `rook`: `settings.describe` (schema), `settings.get`
  (effective values with source; secrets masked), `settings.set` (key, scope,
  target, value, note), `settings.reset`, `settings.history`. The MCP bridge
  exposes these as generated tools, and the dashboard uses the same API. Writes
  need the `admin` tier in the permissions spec (`rook-beta-permissions-spec`).

## 3.5 Information architecture

A single **Settings** entry in the dashboard sidebar replaces the scattered
configuration pages. Content views (Chat, Knowledge, Work, Sessions) stay where
they are.

```
Settings
├── Hub                      scope hub
│   ├── General              name, public addresses, installer domain
│   ├── Network              dashboard/MCP bind, allowed hosts, public URL, relay
│   ├── Sign-in              accounts, Google login, admin users  (replaces ROOK_WEB_PASS, /tokens password)
│   ├── Updates              push updates, signing key status, APK allowlist
│   ├── Storage              data directory, retention limits (journal, consoles)
│   └── Monitoring           watchdog, alerts
├── Bands                    one page per band (scope band)
│   ├── General              name, relay address workers dial
│   ├── Keys & access        PSK rotate/revoke, pairing codes, members
│   └── Worker defaults      announce interval, log level, OTA channel, default plugins
├── Workers                  one page per worker (scope worker)
│   ├── Identity             name, description
│   ├── Connection           hub, band (risky → commit-confirmed)
│   ├── Plugins              enable/disable + each plugin's worker settings
│   └── Custom caps          cmd.<name> definitions
├── Plugins                  one page per plugin: all its settings across scopes
│   ├── Knowledge            enable, embeddings URL/model
│   ├── Voice
│   ├── Decision engine
│   └── …
├── Agent instructions       guidance slots (existing page, moved)
├── Secrets                  vault (existing page, moved)
├── API tokens               (existing page, moved; single copy)
└── My preferences           scope user: profile, voice, thinking, notifications
```

Groups inside a page come from `group`. A **search box** at the top of Settings
matches keys, labels, help text and env names (typing `ROOK_KNOWLEDGE` finds the
Knowledge toggle).

## 3.6 Source indicators and validation

Each row shows a badge:

| Badge | Meaning |
|---|---|
| `default` | built-in value |
| `hub` / `band` | inherited from that scope (link to it) |
| `override` | set at this scope; "Reset to inherited" available |
| `env ROOK_X` | locked by the environment (read-only; shows the stored value underneath) |
| `flag --x` | locked by a command-line flag |
| `file` | read from a legacy file not yet imported |
| `vault:name` | secret reference |
| `restart` / `reload` | change saved, not yet active |
| `pending` | worker change awaiting commit-confirm |

Validation runs in three places:

1. **Client**: type, range, pattern, choices (generated from the schema).
2. **Hub `settings.set`**: the same checks plus cross-field validators declared
   by the plugin (for example `relay.peer_ttl ≥ 3 × core.worker.keepalive`, fixing
   P16; `core.mcp.public_url` requires `core.mcp.allowed_hosts` to contain its
   host).
3. **Worker, before staging**: the worker validates the delivered map with its
   own schema version and rejects keys it does not know (older builds), so a
   newer hub cannot strand an older worker.

`settings.set` accepts `dry_run=true` and returns the effective diff and the
apply class, which drives the confirmation dialog.

## 3.7 Delivery to workers and restart handling

- Worker-scoped effective settings ride the existing config epoch
  (`worker.config_apply`/`config_confirm`) as a typed `settings` map, not an
  untyped `env` map. Workers that predate the schema keep receiving the legacy
  `env` form, which the hub generates from each key's first `env` alias (fleet
  compatibility, project rule 3).
- `apply=live` keys are delivered with a `settings.changed` event and applied by
  the plugin's callback, with no restart. Plugin enable/disable is `live` today
  and stays so.
- `apply=reload` restarts only that plugin (`worker.plugin.disable/enable`).
- `apply=restart` restarts the process at a time the operator chooses; the UI
  shows a "Restart required" banner per worker with a button.
- `apply=risky` (hub address, PSK, band move) uses commit-confirmed with auto
  revert, as today.
- Secrets for worker plugins are resolved on the hub and sent only to the target
  worker inside the band-encrypted config message. The worker stores them in a
  mode-600 file separate from `config.json`, and `config_get`/`env.list` mask
  them (fixes P19).

## 3.8 Wireframes

### Hub settings

```
┌ Rook ─────────────┬──────────────────────────────────────────────────────────────┐
│ Workers           │ Settings › Hub                          [ search settings… ] │
│ Chat              │                                                              │
│ Knowledge         │ General  Network  Sign-in  Updates  Storage  Monitoring      │
│ Work              │ ─────────────────────────────────────────────────────────── │
│ Sessions          │ Network                                                      │
│ Install           │                                                              │
│ ▸ Settings        │ Dashboard listen address   0.0.0.0:7005                      │
│   Hub             │                            [env ROOK_BIND, ROOK_PORT] 🔒     │
│   Bands           │                            stored: —   ⟳ restart to change  │
│   Workers         │                                                              │
│   Plugins         │ MCP listen address         127.0.0.1:8765  [flag --bind] 🔒  │
│   Agent instr.    │                                                              │
│   Secrets         │ Public MCP URL             [https://mcp.example.com     ]    │
│   API tokens      │                            override · set by admin 2d ago    │
│   My preferences  │                                                              │
│                   │ Allowed Host headers       [mcp.example.com            ] +   │
│                   │                            ⚠ must include the public URL host│
│                   │                                                              │
│                   │ Relay address (internal)   relay:7474    [flag --hub] 🔒     │
│                   │ Relay peer TTL             [ 60 ] s      default             │
│                   │                            ✓ ≥ 3 × worker keepalive (20 s)   │
│                   │                                                              │
│                   │ ▸ Show advanced (4)                                          │
│                   │                                                              │
│                   │ 1 change pending: Public MCP URL  (restart MCP)              │
│                   │                          [ Discard ]  [ Save and restart ]   │
│                   │ History ▸                                                    │
└───────────────────┴──────────────────────────────────────────────────────────────┘
```

### Band settings

```
Settings › Bands › home-lab                                   band id 3f9a1c2e
General   Keys & access   Worker defaults
──────────────────────────────────────────────────────────────────────────────
General
  Band name                 [ home-lab              ]   override
  Relay address workers dial [ hub.example.com:443  ]   inherited from Hub  ↺
                            ⚠ risky: workers move under commit-confirm (60 s window)

Keys & access
  Band key                  ●●●●●●●●  vault:band/home-lab   rotated 14 d ago
                            [ Rotate… ]  [ Revoke old key ]
  Pairing codes             none active                      [ New code (5 min) ]
  Members                   user-a (owner) · agent-ci (member)   [ Invite… ]

Worker defaults                              applies to 7 workers · 1 overrides
  Announce interval         [ 30 ] s        default
  Log level                 ( ) error (•) warning ( ) info ( ) debug
  Automatic updates         [x] on          2 workers held   (view)
  Plugins on new workers    [x] info [x] shell [x] file [ ] wake [ ] memory …

History
  2026-09-28 14:02  user-a     Band key rotated (fp 8c1e…→41d0…)   [ view ]
  2026-09-21 09:15  agent-ci  Log level warning → info            [ revert ]
```

### One worker's settings

```
Settings › Workers › worker-a                 build 167.quiet.otter  ● online
Identity   Connection   Plugins   Custom caps
──────────────────────────────────────────────────────────────────────────────
Identity
  Name                      [ worker-a          ]   override   ⟳ restart
  Description               [ Build box, GPU     ]  live

Connection                                        ⚠ risky: commit-confirmed
  Band                      home-lab ▾               from enrollment
  Relay address             hub.example.com:443      inherited from band
  Transport                 WebSocket                [flag --ws] 🔒
  Announce interval         [ 30 ] s                 inherited from band  ↺

Plugins                                              [ Show all 24 ]
  ┌──────────────┬─────────┬───────────────────────────────────────────────┐
  │ shell        │ [x] on  │ —                                             │
  │ proc         │ [x] on  │ Max sessions [16]  default                    │
  │ wake         │ [x] on  │ Command  [claude -p {prompt_file}      ]      │
  │              │         │ Agent    [agent:claude                  ]      │
  │ memory       │ [ ] off │ Notes directory [                       ]     │
  │ pikvm        │ n/a     │ not loaded: excluded by --enable in unit file │
  └──────────────┴─────────┴───────────────────────────────────────────────┘

Custom caps
  cmd.deploy     systemctl --user restart {svc}     args: svc     [edit] [×]
  [ + Add custom cap ]

Pending: wake.command (reload plugin)            [ Discard ] [ Apply ]
Config epoch 42 · confirmed 3 d ago · History ▸
```

### Plugin settings: Voice

```
Settings › Plugins › Voice                         service ● running on hub
Access   Recognition   Speech   Agent   Advanced
──────────────────────────────────────────────────────────────────────────────
Access                                                   scope: hub
  Listen address           127.0.0.1:8900        [env VOICE_BIND, VOICE_PORT] 🔒
  TLS certificate / key    (none)                 served behind proxy
  Client token             ●●●●●●  vault:voice/token     [ Replace… ]
  Allow anonymous (LAN)    [ ] off                default

Recognition                                              scope: hub
  Model                    [ small.en ▾ ]         default           ⟳ reload
  Device / compute         [ cpu ▾ ] [ int8 ▾ ]   default
  Minimum speech           [ 450 ] ms             env MIN_SPEECH_MS 🔒 (legacy name)
  Energy floor             [ 0.008 ]              default
  No-speech ceiling        [ 0.6 ]                default

Speech                                         scope: hub · users may override
  Default voice            [ af_heart ▾ ]         default
                           3 users have their own voice  (view)

Agent                                                    scope: hub
  Language model URL       [ http://127.0.0.1:1234/v1/chat/completions ]
  Model id                 [ ...                               ]
  Rook MCP token           ●●●●●●  vault:voice/mcp-token (agent_id voice)
  ACP endpoint             [ 127.0.0.1 ] : [ 9200 ]
  Auto-approve ACP prompts [x] on                 ⚠ unattended tool permission

My preferences (you)                                     scope: user
  Voice                    [ inherit (af_heart) ▾ ]
  Show thinking            [ ] off
```

### Plugin settings: Decision engine

```
Settings › Plugins › Decision engine                 mode: shadow (observe only)
General   Timing   Data
──────────────────────────────────────────────────────────────────────────────
General                                                  scope: hub
  Engine endpoint          [ cap://any/decide.score        ]  override
                           ✓ reachable on worker-b (12 ms)
  Mode                     (•) shadow  ( ) advise  ( ) act       ⚠ act is not built
  Assistant names          [ rook ] [ assistant ] [+]          default
  Probabilities are uncalibrated: do not gate actions on them.  (guidance cap:cmd.decide-)

Timing                                                   scope: hub
  Per-turn timeout         [ 150 ] ms          default   live
  Recent speech window     [ 15 ] s            default   live
  Silence window           [ 15 ] s            default   live

Data                                                     scope: hub
  Keep raw inputs          [ 30 ] days         default
  Shown to users           per-user "Show thinking" preference (3 of 5 users on)

History ▸   Export schema ▸
```

## 3.9 Migration path

Each phase keeps existing deployments and existing workers (build 167) working.

**Phase 0 — document (this PR).** Inventory and problem list.

**Phase 1 — schema and read-only view (wave 2, settings framework).**
- Add `setting()` to the plugin API, a core settings module, and `settings.db`.
- Declare every row of Part 1 in a schema, with today's variable names as `env`
  aliases and today's defaults. No behaviour changes: values still come from
  where they come from today; the settings layer only reports them.
- Add the read-only Settings area with source badges. This alone shows P1–P4 to
  operators.
- Generate the configuration section of `README.md` and `.env.example` from the
  schema (fixes P11), and fail CI if any `os.environ` read of a `ROOK_*` name
  sits outside the settings module.

**Phase 2 — writable hub and band settings.**
- `settings.set` with history and validation for hub and band scopes.
- Change precedence to env over DB. On the first beta start, import
  `setup.json` into `settings.db` (band name, hub public, installer domain) and
  PSKs into the vault. Where env and file disagree, keep the env value, record
  an `import` history entry that names both, and show a banner. `setup.json`
  becomes read-only and is kept for rollback for one release.
- One admin sign-in: the accounts DB. `ROOK_WEB_PASS` becomes the bootstrap
  admin password only (creates the first admin if none exists).
  `ROOK_MCP_AUTH_PASSWORD` and the MCP-port `/tokens` page are deprecated in
  favour of the dashboard page (fixes P6).
- Unify shapes with aliases: `core.relay.address` accepts `ROOK_HUB` and
  `ROOK_HUB_HOST`+`ROOK_HUB_PORT`; `core.dashboard.listen` accepts
  `ROOK_BIND`+`ROOK_PORT`; `core.mcp.listen` accepts `ROOK_MCP_BIND` (fixes P5).
  Old names log a deprecation warning once.
- Derive every hub store path from `ROOK_DATA_DIR` and one key per store, so
  the dashboard and MCP cannot diverge (fixes P4). The MCP server reads bands
  from the enrollment DB and no longer requires `--psk` (fixes P3).

**Phase 3 — worker settings.**
- Typed worker settings over the config epoch, with legacy `env` generation for
  old builds. Dashboard worker page with plugins and custom caps (fixes P13,
  P14).
- Workers report `--enable` exclusions and env-locked keys in their announce, so
  the UI can explain them.
- Secret masking in `config_get` and `env.list`; secrets in a separate mode-600
  file (fixes P19).
- Installers stop templating `--psk`: the PSK goes into a mode-600 environment
  file or enrollment (fixes P17). Existing units keep working; the next OTA
  rewrites the unit when the worker's service manager allows it.

**Phase 4 — plugins and user preferences.**
- Voice, decision engine, knowledge, watchdog declare schemas and read from the
  settings layer; legacy variables stay as aliases for two releases.
- User preferences (voice, show thinking, wake) move to the account and are
  delivered to Android and the TUI with the session. The Android app keeps
  local values as "this device" overrides (fixes P21).
- Watchdog gets its own scoped token instead of the static token (fixes P8).
- The embeddings service reports its model on `/health`; the hub takes the
  model from there instead of `ROOK_EMBED_MODEL` (fixes P9).

**Phase 5 — remove.** Drop deprecated aliases and legacy file sources after two
releases with warnings; the schema's `deprecated` field drives the warnings and
the changelog.

### Key mapping (today → proposed)

| Today | Proposed key | Scope | Apply |
|---|---|---|---|
| `ROOK_BIND` + `ROOK_PORT` | `core.dashboard.listen` | hub | restart, bootstrap |
| `ROOK_MCP_BIND` | `core.mcp.listen` | hub | restart, bootstrap |
| `ROOK_HUB`, `ROOK_HUB_HOST`/`PORT` | `core.relay.address` | hub | restart, bootstrap |
| `ROOK_HUB_PUBLIC`, setup `hub_public` | `core.band.public_relay` | band (default from hub) | risky |
| `ROOK_DOMAIN`, setup `pyz_domain` | `core.hub.public_url` | hub | live |
| `ROOK_BAND_NAME`, setup `band_name` | `core.band.name` | band | live |
| `ROOK_BAND_PSK`, setup `band_psk` | `core.band.key` (vault) | band | risky |
| `ROOK_WEB_PASS` | `core.auth.bootstrap_password` (vault) | hub | bootstrap |
| `ROOK_MCP_AUTH_PASSWORD` | removed (accounts) | — | — |
| `ROOK_MCP_STATIC_TOKEN` | `core.mcp.static_token` (vault) | hub | live |
| `ROOK_MCP_PUBLIC_URL`, `ROOK_ALLOWED_HOSTS` | `core.mcp.public_url`, `core.mcp.allowed_hosts` | hub | restart |
| `ROOK_KNOWLEDGE` | `knowledge.enabled` (the `task` plugin follows it; implemented, env alias) | hub | restart |
| `ROOK_EMBED_URL`, `ROOK_EMBED_MODEL`, `ROOK_KNOWLEDGE_DB` | `knowledge.embedder` (resource: `http(s)://` or `cap://any/embed.text`), `knowledge.embed_model`, `knowledge.db_path`; new `knowledge.semantic` (implemented, env aliases) | hub | restart |
| `ROOK_PUSH_UPDATES` | `core.updates.push` | hub | live |
| `ROOK_GOOGLE_*` | `core.auth.google.*` (client file → vault) | hub | restart |
| `--announce-interval`, pushed `announce_interval` | `core.worker.announce_interval` | band, worker override | restart |
| pushed `log_level`, `-v` | `core.worker.log_level` | band, worker override | live |
| `ROOK_UPDATE_POLL` | `core.worker.update_poll` | band | restart |
| `ROOK_WAKE_CMD`, `ROOK_WAKE_AGENT` | `wake.command`, `wake.agent` | worker | reload |
| `ROOK_MEMORY_VAULT` | `memory.notes_dir` | worker | reload |
| `PIKVM_*` | `pikvm.url`, `pikvm.user`, `pikvm.password` (vault), `pikvm.verify_tls` | worker | reload |
| `CEC_*`, `ROOK_DONGLE_PORT` | `cec.*`, `dongle.port` | worker | reload |
| `plugins.json` | `core.worker.plugins.<name>.enabled` | worker (default from band) | live |
| `custom_caps.json` | `customcap` records (not settings; own page, same history) | worker | live |
| `VOICE_*`, `WHISPER_*`, `VLLM_*`, `ACP_*`, `MIN_*` | `voice.*` | hub | reload |
| `DECISION_*` | `decision.*` | hub | live |
| `ROOK_WATCHDOG_*` | `watchdog.*` (Telegram token → vault) | hub | next run |
| Android `voice_choice`, `show_thinking`, `wake_enabled` | `voice.default_voice`, `voice.show_thinking`, `voice.hotword_enabled` | user (device override) | live |

## 3.10 Open questions

1. **Plugin API alignment.** This proposal assumes `setting()` carries `apply`,
   `bootstrap`, `overridable` and `env` aliases. The plugin-API task should
   confirm or rename these.
2. **Where do hub-hosted services live?** Voice and the decision engine run as
   separate processes today. Do they become hub plugins that read settings in
   process, or remote services that fetch their settings through the MCP with a
   scoped token? The wireframes work for both.
3. **Env-over-DB for the PSK.** Phase 2 makes the environment win. A deployment
   whose `.env` still holds an old PSK after a UI rotation would then revert to
   the old key on restart. Options: treat `core.band.key` as UI-owned once
   imported (env only seeds it), or refuse to start and show the conflict.
   Recommendation: seed-only for keys, env-wins for everything else.
4. **Per-worker secrets on disk.** Is a mode-600 file on the worker acceptable,
   or should plugins fetch secrets on use through `{{secret:name}}` so nothing is
   stored on the worker?
5. **Who may change band settings?** Band `owner` role only, or also API tokens
   with the admin tier? This belongs to the permissions spec.
6. **Relay settings.** The relay is a separate binary with its own environment.
   Should the hub manage it (write its unit environment and restart it), or only
   validate and display it?
