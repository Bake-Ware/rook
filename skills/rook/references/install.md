# Installing Rook

Pieces: **relay** (`telesthete-hub`, Rust, UDP 7474, blind forwarder), **hub** (`rook-hub` / `rook hub serve`, :7005 — one process: the dashboard (web UI, installers, pairing, OTA controller) and the MCP server (`/mcp` agent tools, journal, `/band` WebSocket bridge); also serves MCP alone on :8765 for older setups), and **workers** (`rook-worker` / `band-worker.pyz`) on every machine. `rook-dashboard` + `rook-mcp` still run the two halves as separate processes.

Hub state lives in `ROOK_DATA_DIR` (keys, enrollment DB, tokens, journal, chat, vault, knowledge). Worker state lives in `~/.rook-band-worker/` (`worker_id`, `enrollment.json`, `band-worker.pyz`, `audit.jsonl`, custom caps).

## Hub: one-command install (recommended)

On the Linux machine that will be the hub (Debian/Ubuntu/Fedora/Arch/openSUSE, x86_64 or aarch64):

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash
# options after `bash -s --`: --user (no root) --version vX.Y.Z --bind 127.0.0.1 --yes --uninstall [--purge]
```

Installs python3/venv/curl if missing, Rook from the release wheel into `/opt/rook/venv`, the prebuilt relay (sha256-checked), generates `ROOK_BAND_PSK`, `ROOK_WEB_PASS`, `ROOK_MCP_STATIC_TOKEN` into `/var/lib/rook/secrets.env` (mode 600; settings in `hub.env` beside it), starts systemd units `rook-relay` and `rook-hub` (dashboard + MCP in one process, :7005) as user `rook`, waits for health, and prints the dashboard URL + password. Next step for the user: open the URL, sign in (any username), **Install a worker** → **Get a pairing code**. Re-run = upgrade (secrets kept). `--user` puts everything in `~/.local/share/rook-hub` with `systemctl --user` units (`sudo loginctl enable-linger $USER` to survive logout); `--no-systemd` runs background processes (containers/CI). Logs: `sudo journalctl -u rook-hub -n 50 --no-pager`. A `--version` from before the single-process hub gets `rook-dashboard` + `rook-mcp` units instead. Details and troubleshooting: `docs/operations/install.md`.

## Hub: Docker

```sh
# compose.yaml + .env.example from the repo; set ROOK_WEB_PASS, ROOK_BAND_PSK, ROOK_MCP_STATIC_TOKEN,
# ROOK_DOMAIN=http://<ip>:7005, ROOK_HUB_PUBLIC=<ip>:7005 in .env
docker run --rm ghcr.io/bake-ware/rook python -m rook.remote.psk   # a band key
docker compose up -d                                               # ghcr.io/bake-ware/rook:${ROOK_VERSION:-latest}, amd64/arm64
docker compose -f compose.yaml -f compose.build.yaml up -d --build # build from a clone instead
```

## Hub: from source (development)

```sh
git clone https://github.com/Bake-Ware/rook && cd rook
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                                  # '.[desktop]' for screenshots on desktop workers
python3 scripts/fetch-relay.py --dest .venv/bin   # prebuilt relay; or: cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium
scripts/local-hub.sh                              # BIND=0.0.0.0 to serve the LAN
```

First run writes `rook-data/quickstart.env` (mode 600) with `ROOK_BAND_PSK`, `ROOK_WEB_PASS`, `ROOK_MCP_STATIC_TOKEN`, and starts the hub (dashboard :7005, MCP :7005/mcp) and the relay (udp :7474) in the foreground.

## Hub: public (workers anywhere)

Put it behind HTTPS (Caddy/nginx/Cloudflare Tunnel). Route the domain to the hub (7005); `rook-hub` serves the MCP paths there. (Two processes: also route `/mcp`, `/band`, `/tokens/account-api`, `/authorize`, `/token`, `/.well-known/`, `/skill/` to MCP on 8765; `rook-hub` serves those on 8765 too.) Set:

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
- Windows 10 1809+ workers run interactive terminals (`work.stream.*`, the web Sessions page) on ConPTY; `proc.start(pty=True)` and pty console rooms still need a POSIX worker. On older Windows, or for those, run a second worker inside WSL on the same machine (`<name>-wsl`).
- Source/pip workers trust no update key unless `ROOK_UPDATE_PUBKEY` is set (`python rook/remote/update_keys.py pubkey`).
- Linux service unit is `rook-band-worker.service` (user or system unit depending on installer); `journalctl [--user] -u rook-band-worker -n 50 --no-pager` for logs.

After install, give it a role description so other agents know what it's for:
`rook_call("worker.description_set", worker=NAME, args={"description": "GPU box: llama.cpp, voice STT"})`.

## Connecting an agent (MCP)

Give each agent its **own token** (minted on the dashboard's Tokens page, `https://<host>/#tokens`, signed in as the operator) so the journal attributes calls. The static token is a fallback.

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
