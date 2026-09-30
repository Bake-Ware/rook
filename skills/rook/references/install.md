# Installing Rook

Pieces: **relay** (`telesthete-hub`, Rust, UDP 7474, blind forwarder), **dashboard** (`rook-dashboard`, :7005 — web UI, installers, pairing, OTA controller), **MCP server** (`rook-mcp`, :8765 — agent tools, journal, `/band` WebSocket bridge), and **workers** (`rook-worker` / `band-worker.pyz`) on every machine.

Hub state lives in `ROOK_DATA_DIR` (keys, enrollment DB, tokens, journal, chat, vault, knowledge). Worker state lives in `~/.rook-band-worker/` (`worker_id`, `enrollment.json`, `band-worker.pyz`, `audit.jsonl`, custom caps).

## Hub: quickstart (one machine)

Needs Linux, Python 3.11+, git, Rust (`cargo`).

```sh
cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium   # → ~/.cargo/bin/telesthete-hub
git clone https://github.com/Bake-Ware/rook && cd rook
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # '.[desktop]' for screenshots on desktop workers
scripts/local-hub.sh        # BIND=0.0.0.0 to serve the LAN
```

First run writes `rook-data/quickstart.env` (mode 600) with `ROOK_BAND_PSK`, `ROOK_WEB_PASS`, `ROOK_MCP_STATIC_TOKEN`, and starts dashboard :7005, MCP :8765/mcp, relay udp :7474 in the foreground.

## Hub: Docker

```sh
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(16))"   # ROOK_WEB_PASS
python3 -m rook.remote.psk                                       # ROOK_BAND_PSK
# set ROOK_WEB_PASS, ROOK_BAND_PSK, ROOK_MCP_STATIC_TOKEN in .env
docker compose up -d --build
```

## Hub: public (workers anywhere)

Put it behind HTTPS (Caddy/nginx/Cloudflare Tunnel). Route the domain to the dashboard (7005) and these paths to MCP (8765): `/mcp`, `/band`, `/tokens`, `/authorize`, `/token`, `/.well-known/`, `/skill/`. Set:

- dashboard: `ROOK_DOMAIN=your.domain`, `ROOK_HUB_PUBLIC=your.domain:443`
- MCP: `ROOK_MCP_PUBLIC_URL=https://your.domain`, `ROOK_ALLOWED_HOSTS=your.domain` (also enables the OAuth front door the claude.ai web connector needs)
- never expose the dashboard without `ROOK_WEB_PASS` (it refuses off-loopback without one unless `--insecure-no-auth`)

Build the installable worker bundle once (telesthete checkout next to rook, or pip-installed):

```sh
ROOK_PUBLIC_BASE=https://your.domain python rook/remote/build_band_worker.py
```

The hub creates its ed25519 update key on first start (`ROOK_DATA_DIR`, or `ROOK_UPDATE_KEY`); the build stamps the public half into the bundle so workers only trust this hub.

Standalone relay on another box: `curl -fsSL https://<host>/hub | bash -s -- --yes` (tune `/etc/telesthete-hub.env`, then `systemctl restart telesthete-hub`). Keep `HUB_PEER_TTL_SECS` ≥ 60.

## Workers

**Pairing code first.** Codes come only from the dashboard **Tokens** page (6 chars, lowercase alphanumeric, 5-minute expiry, reusable for several devices within the window). Agents cannot mint them — ask the user to read one out, then run the install within 5 minutes.

| Platform | Command |
|---|---|
| Linux/macOS (service) | `curl -fsSL 'https://<host>/worker?band=CODE' \| bash` |
| Unattended combined installer | `curl -fsSL https://<host>/install \| bash -s -- worker` (with `ROOK_JOIN_CODE=CODE` in env) — targets `worker`, `cli`, `both` |
| Windows (PowerShell) | `iex (irm "https://<host>/worker?band=CODE&os=windows")` |
| Android | install the APK from `https://<host>/apk`, then enter the pairing code (or Google sign-in) in the app's settings; grant each sensitive permission (screen capture, accessibility, SMS, call log, contacts, location, notifications) in-app |
| From source, LAN | `rook-worker --hub <hub-ip>:7474 --psk "$ROOK_BAND_PSK" --name NAME` (put the PSK in env, not argv, outside demos) |
| From source, remote | `rook-worker --hub your.domain:443 --ws --name NAME` |
| Enroll via web login | `rook-worker --enroll https://your.domain [--pair-code CODE]`, then run with `--enrolled` |

Useful flags: `--name`, `--enable plugin1,plugin2` (default all builtins), `--update-url` (empty = no auto-update), `--announce-interval`, `--selftest`, `--version`, `--install-cli`.

Gotchas:
- A worker appears within ~30 s (announce interval). Verify with `rook_call("info.ping", worker=NAME)`, not `rook_workers`.
- Saved state in `~/.rook-band-worker/` overrides `--hub/--psk` (it logs a warning). For a throwaway second worker on the same box: `HOME=$(mktemp -d) rook-worker …`.
- The installer returns 403 with a missing, expired or revoked code — get a fresh one.
- Windows workers can't allocate a PTY. For interactive sessions (resuming `claude`/`codex`), run a second worker inside WSL on the same machine (`<name>-wsl`).
- Source/pip workers trust no update key unless `ROOK_UPDATE_PUBKEY` is set (`python rook/remote/update_keys.py pubkey`).
- Linux service unit is `rook-band-worker.service` (user or system unit depending on installer); `journalctl [--user] -u rook-band-worker -n 50 --no-pager` for logs.

After install, give it a role description so other agents know what it's for:
`rook_call("worker.description_set", worker=NAME, args={"description": "GPU box: llama.cpp, voice STT"})`.

## Connecting an agent (MCP)

Give each agent its **own token** (minted at `https://<host>/tokens`, protected by `ROOK_MCP_AUTH_PASSWORD`) so the journal attributes calls. The static token is a fallback.

```sh
# Claude Code
claude mcp add --transport http rook https://your.domain/mcp --header "Authorization: Bearer $TOKEN"
```

Any streamable-HTTP MCP client with a bearer header works the same way. The claude.ai web connector uses the OAuth front door (needs `ROOK_MCP_PUBLIC_URL` + `ROOK_ALLOWED_HOSTS`). Confirm with `rook_whoami`.

Terminal UI: `rook` (or `rook band`), `--url` for a non-local dashboard; `curl -fsSL https://<host>/install | bash -s -- cli` installs it.

## Installing this skill into an agent harness

The hub serves the skill it was built with, plus the operator's site notes (see admin.md) for authenticated callers:

- MCP resources: `rook://skill/rook` (SKILL.md), `rook://skill/rook/references/<name>.md`
- HTTP: `GET https://<host>/skill/rook.skill` (Claude skill zip, root folder `rook/`); send `Authorization: Bearer $TOKEN` to include site notes

From a machine with Rook installed:

```sh
rook skill install                                # Claude Code → ~/.claude/skills/rook
rook skill install --harness codex                # Codex → ~/.agents/skills/rook
rook skill install --hub https://your.domain      # fetch from the hub; ROOK_TOKEN adds site notes
rook skill install --dest ./.claude/skills/rook   # project-scoped
rook skill package -o rook.skill                  # zip for upload (e.g. claude.ai)
```

Without `--hub` it installs the copy bundled with the local Rook package (generic, no site notes).
