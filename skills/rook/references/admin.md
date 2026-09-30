# Administering Rook

## Worker config (commit-confirmed)

`rook_config_get(worker)` shows active overrides and pending/confirm state.
`rook_config_apply(worker, settings, confirm_within=120)` stages, restarts, waits for the worker to reappear, verifies, confirms. If the worker doesn't come back it **auto-reverts**.

Settings keys: `name`, `announce_interval`, `log_level`, `hub`, `psk`, `env` (dict). Examples:
- enable agent wake: `{"env": {"ROOK_WAKE_CMD": "claude -p {prompt_file}"}}`
- memory vault: `{"env": {"ROOK_MEMORY_VAULT": "/srv/notes-vault"}}`

Never hand-edit remote config over `shell.exec` when this path works. Moving hub/PSK via config is fleet surgery — ask first.

## Plugins, restarts, descriptions

| Action | Call |
|---|---|
| See what's loaded | `worker.plugin.list` |
| Toggle a plugin at runtime | `worker.plugin.enable` / `worker.plugin.disable` with `{"module": "deluge"}` |
| Restart the worker process | `worker.restart` |
| Change hub/psk without the confirm dance | `worker.reconfigure` (`hub`, `psk`) — prefer `rook_config_apply`. Hub/psk changes need the hub's signed call ticket, so send them through the hub (rook_call), never from a direct band peer |
| Role text shown in rosters | `worker.description_set` / `worker.description_get` |
| Worker health/build | `worker.status` |
| Recent cap calls on that worker | `log.audit` (`limit`, `cap_prefix`), `log.tail` (`limit`) |

## OTA self-update

Flow: build bundle → hub signs manifest `{build, sha256, url, sig}` → the controller pushes `worker.apply(manifest)` to any worker behind → worker verifies signature + sha256 + `--selftest`, swaps, keeps the previous bundle for rollback.

- **Default policy: let autoupdate converge.** Don't sweep the fleet with `worker.apply` or per-worker checks unless the user asks.
- Canary: `worker.check` with `{"force": true}` on one worker, verify, then let the rest converge.
- Pin a node: `worker.hold`.
- `worker.apply` / `worker.deauth` / `worker.update` only accept payloads signed by the controller key — being on the band isn't enough, and agents can't forge them. `worker.update(url=…)` needs a signed `manifest` too.

## Permissions

The hub evaluates a policy for every call (`docs/design/permissions.md`). It ships in **audit** mode: nothing is denied, would-be denials are journaled (`decision=would_deny`). `rook_call(worker="rook", cap="policy.explain", args={"principal": "role:agent", "cap": "…", "worker": "…"})` shows the rule that decides a call; `policy.status` shows the mode and whether calls carry tickets. A denial comes back as `{"ok": false, "error": "denied: …", "denied": {"rule", "rev", …}}`: don't retry it, ask the user. Changing the policy (`policy.set`, dashboard **/permissions**) is for band owners and operator tokens.
- Android updates come from a rebuilt APK (hub feed checked on start and every 6 h; SHA-256 + same signing cert required), not the zipapp feed. The ESP32 dongle has its own flash path.

## Custom caps (fleet-wide patterns)

`customcap.add {name, command, args[], description, timeout}` → `cmd.<name>`; `customcap.list`; `customcap.remove {name}`. Hyphenated names. Placeholders `{x}` are shell-escaped. Persisted per worker. Good candidates: health briefs, service restarts with verification, route management, anything an agent keeps rebuilding from scratch. Keep outputs to a few lines.

## Bands, keys, pairing

- PSK = five hyphenated words (≈64.6 bits) or a legacy key; `band_id = SHA256(PSK)[:16]`. Anyone with the PSK can command every worker in the band.
- Dashboard **Tokens**: start/revoke pairing codes. **Bands** (`/account/bands`): rename, delete, **Migrate all**, **Migrate PSK** (all active enrolled devices must be online), per-worker **Move to band…** (needs `worker.enrollment_move_prepare`). Keep the page open during migrations; **Resume** after interruption.
- Replacing/revoking a PSK does not erase it on remote devices; offline peers holding the old key still work on the old band until reconfigured.
- `ROOK_ENROLLMENT_DB` + `ROOK_SETUP_PATH` must be shared (read/write) by dashboard and MCP. Back up the enrollment DB; never delete it to "reset".
- All of these are user decisions. Prepare, explain, and let them click.

## Hub services

| Service | Role | Restart impact |
|---|---|---|
| `telesthete-hub` | UDP relay :7474 | LAN workers drop for seconds, then reconnect |
| dashboard (`rook-dashboard`) | UI, installers, pairing, OTA controller | UI/pairing/pushes pause |
| MCP (`rook-mcp`) | agent tools, journal, `/band` WS bridge | **every agent disconnects, remote WS workers drop** |

Unit names vary by install (a hand-built hub may call them e.g. `rook-band-mcp` / `rook-remote`); check `systemctl list-units 'rook*'`. If releases are selected by systemd drop-ins, keep **one** drop-in that sets `WorkingDirectory`/`PYTHONPATH` and edit it; never stack new higher-sorting overrides on top, and back the drop-in dir up before changing it.

Health: `GET /healthz` on the MCP (session counters; needs auth). Watchdog (`rook/band_mcp/watchdog.py`) runs every minute on-hub plus an off-hub copy, alerting via Telegram. MCP holds a bounded session pool; each token may hold ≤48 sessions (leaky clients only starve themselves; LRU idle sessions are evicted).

Agent-facing text (connection instructions, idle-task prompt, tool/cap tips) is editable on the dashboard's **Agent instructions** page — no deploy needed. That's also where to shorten tips that cost every agent tokens.

## Agent skill and site notes

The hub serves the generic agent skill (`skills/rook/` in the repo) as MCP resources and as `GET /skill/rook.skill`. To add band-specific notes (host roles, standing rules, which worker does what) without committing them, set one of these on the MCP service:

- `ROOK_SKILL_SITE_PAGE=<knowledge slug or id>`: a knowledge page (needs `ROOK_KNOWLEDGE=1`); edits show up on the next fetch
- `ROOK_SKILL_SITE_FILE=/path/site.md`: a Markdown file on the hub

It is served as `references/site.md` to MCP clients and to HTTP callers with a valid bearer token; anonymous downloads get the generic skill. Keep secrets out of it: it is copied into every agent's skills directory.

## Troubleshooting ladder (cheapest first)

1. `rook_call("info.ping", worker=X)` — alive on the band?
2. Refused call → read the error; it names who has the cap or that the worker is unknown (roster evicts after ~90 s of silence).
3. Timed out? It may still be running: `rook_journal(call_id=…)` before retrying anything with side effects.
4. `worker.status` → build, plugins, hold state.
5. On the box (another worker or console): `journalctl [--user] -u rook-band-worker -n 50 --no-pager`.
6. Worker ignoring `--hub/--psk`? Saved `~/.rook-band-worker/enrollment.json` wins.
7. Remote WS workers all gone at once → check the tunnel/proxy and `rook-mcp` (the `/band` bridge), not the workers.
8. LAN workers gone → relay (`telesthete-hub`) and UDP 7474 firewall.
9. Android worker flaky → foreground service killed or a permission revoked; check the app, battery optimization and `battery.status`.
