# Deploying the hub (`rook hub`)

Workers update themselves from signed bundles. `rook hub` gives the hub the
same treatment: a signed release, a fresh release directory, preflight checks,
database backups, **one** release selector per service, a dead-man timer that
rolls back on its own, health verification, and a history you can roll back
from.

The hub is one service, or two in the older layout:

| Service | Module | Restart impact |
|---|---|---|
| `hub` | `rook.hub.serve` (`rook hub serve`: dashboard + MCP server in one process) | the web UI reloads and **every connected agent is disconnected** |
| `dashboard` | `rook.remote.bootstrap` (two-process layout) | the web UI reloads |
| `mcp` | `rook.band_mcp` (two-process layout) | **every connected agent is disconnected** |

Configure either `hub` alone or `dashboard` + `mcp` (the default when no
`services` are configured). With two, both are deployed one at a time,
dashboard first and MCP last; if the dashboard fails verification the MCP is
never touched. Moving a two-unit hub to one is described in
[Moving to one hub process](#moving-to-one-hub-process).

Manage > Updates runs this for a GitHub release: it repacks the release's
source archive as a hub release signed by this hub's update key and runs
`rook hub deploy` detached (docs/operations/updates.md).

## Why this exists

Hand deploys fail in quiet ways. A typical failure history:

- Each deploy added a new systemd drop-in that selected its release. Drop-ins
  apply in lexical order, so every new, alphabetically later file silently
  overrode the ones before it. Several stacked overrides later, nobody could
  tell which tree a unit ran without `systemctl cat`.
- A rollback script restored an old drop-in, which re-pinned an old release
  the next time the unit restarted.
- One deploy copied only part of the tree over an existing release: removed
  modules stayed behind and a stale `RELEASE` file named the wrong version.

`rook hub` removes each of these by construction: every release is a clean
`git archive` in its own directory, exactly one file selects the release, and
deploy refuses while any other file tries to.

## Layout

```
<root>/
  releases/<build>.<adjective>.<noun>/   one clean tree per release
    RELEASE.json                         the signed manifest it came from
  state/
    downloads/                           fetched tarballs
    history.jsonl                        deploy / rollback events
    deploys/<id>/                        per deploy:
      plan.json  selectors.json          what changed, and the prior selector state
      db/                                SQLite backups (sqlite3 backup API)
      strays/                            adopted stray drop-ins (see below)
      rollback.sh                        plain-sh rollback for this deploy
      deadman.armed                      present while the dead-man is armed
  current-<service>                      symlink mode only
```

The hub venv (`python` in the config) supplies third-party dependencies. The
release directory supplies Rook itself: the selector puts it on `PYTHONPATH`,
ahead of whatever copy of `rook` is installed in the venv. Preflight checks
that every module really is imported from the release, so a shadowing install
is caught before the switch. When a release needs new dependencies, install
them into the venv first (they are additive); preflight fails on a missing one.

## Release selection: exactly one mechanism

**systemd (recommended).** Each unit gets one drop-in,
`<unit>.d/90-release.conf`, written only by `rook hub`:

```ini
# Managed by `rook hub deploy`. ...
[Service]
Environment=ROOK_RELEASE=412.salty.otter
Environment=ROOK_RELEASE_DIR=/opt/rook-hub/releases/412.salty.otter
Environment=PYTHONPATH=/opt/rook-hub/releases/412.salty.otter
WorkingDirectory=/opt/rook-hub/releases/412.salty.otter
```

Any other drop-in for the unit that sets `PYTHONPATH`, `ROOK_RELEASE`,
`WorkingDirectory` or `ExecStart` is a **stray**. Deploy refuses while strays
exist. `--adopt-strays` moves them into the deploy's `strays/` directory, and
that deploy's rollback puts them back, so the previous state is restored
exactly. Drop-ins that set other things (limits, extra environment) are left
alone. `rook hub status` shows both the selected release and the one systemd
actually applies (`systemctl show -p Environment`); a difference means a stray
is winning. Verification after each restart checks the effective release too.

`rook hub units` prints generic unit files for your config. The unit itself
never names a release:

```ini
[Service]
EnvironmentFile=-/etc/rook/hub.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/rook-hub/venv/bin/python -m rook.hub.serve
Restart=on-failure
```

Settings such as `ROOK_DATA_DIR`, `ROOK_BAND_PSK`, `ROOK_PORT`, `ROOK_HUB_HOST`
and `ROOK_MCP_BIND` go in the environment file (see docs/design/settings.md).
The `hub` service needs one virtualenv with both halves' dependencies (the
package's own: aiohttp, mcp, ...); the two-process layout could split them.

**symlink.** For installs that are not run by systemd (or units you would
rather point at a path): `<root>/current-<service>` points at
`releases/<version>` and is switched atomically with a rename. The service's
launcher sets `PYTHONPATH=<root>/current-<service>`. Each service needs a
`restart` command (any shell command), or a `unit` to restart with systemctl.

**Docker / compose** installs are out of scope: there the image is the
release. Build a new image and roll back by tag. The image's default command
is `rook-hub` (dashboard + MCP); a setup that used the default command as its
dashboard next to a separate `rook-mcp` container must drop that container
or pin `command: ["rook-dashboard"]`.

## Configuration

One JSON file: `--config`, else `$ROOK_HUB_DEPLOY_CONFIG`, else
`/etc/rook/hub-deploy.json`.

```json
{
  "root": "/opt/rook-hub",
  "mode": "systemd",
  "systemd": {"scope": "system"},
  "python": "/opt/rook-hub/venv/bin/python",
  "databases": ["/var/lib/rook/*.db"],
  "keep": 5,
  "deadman_minutes": 10,
  "health_timeout": 60,
  "settle_seconds": 3,
  "services": {
    "hub": {"unit": "rook-hub.service",
            "health": ["http://127.0.0.1:7005/health", "tcp://127.0.0.1:8765",
                       "http://127.0.0.1:8765/healthz"]}
  }
}
```

The two-process layout configures two services instead:

```json
  "services": {
    "dashboard": {"unit": "rook-hub-dashboard.service",
                  "health": ["http://127.0.0.1:7005/"]},
    "mcp": {"unit": "rook-hub-mcp.service",
            "health": ["tcp://127.0.0.1:8765", "http://127.0.0.1:8765/healthz"]}
  }
```

| Key | Meaning |
|---|---|
| `root` | holds `releases/` and `state/` |
| `mode` | `systemd` or `symlink` |
| `systemd.scope`, `systemd.unit_dir` | `system` (`/etc/systemd/system`) or `user` (`~/.config/systemd/user`); override the directory if needed |
| `python` | the services' interpreter (per-service `python` overrides) |
| `databases` | globs of SQLite files to back up before every switch |
| `keep` | releases kept by `prune` (at least 2), besides anything in use or needed for rollback |
| `deadman_minutes` | auto-rollback delay; `0` disables |
| `health_timeout`, `settle_seconds` | how long a restarted service may take to become healthy, and how long it must then stay healthy |
| `pubkey` | trusted update public key (default: `$ROOK_UPDATE_PUBKEY`, else this hub's own key) |

Per service: `unit`, `order` (lower restarts first; defaults dashboard 10,
hub 50, mcp 90), `modules` and `packages` (preflight imports; defaults cover
`rook.remote`, `rook.band_mcp` and `rook.knowledge`, and `rook.hub` for
`hub`), `health` (URLs:
`http(s)://` passes on any status below 500, so an auth-gated endpoint
answering 401 still counts; `tcp://host:port` passes when the port accepts),
`restart` (shell command instead of `systemctl restart`), `link` (symlink
path), `disruptive` (ask before restarting; default true for `mcp` and `hub`).

## Building a signed release

On the machine that holds the update signing key (the hub itself, by
default; see docs/design/permissions.md, where this key is the root of trust):

```sh
rook hub release build --ref v-or-commit --out dist/hub \
    [--url-base https://hub.example.com/releases]
```

This runs `git archive` on the commit (never the working tree, so local edits
and untracked files cannot leak in) and writes
`rook-hub-<version>.tar.gz` plus `rook-hub-<version>.json`:

```json
{"schema": 1, "typ": "rook-hub-release", "build": 412,
 "version": "412.salty.otter", "commit": "<full sha>", "built_at": "...",
 "filename": "rook-hub-412.salty.otter.tar.gz", "sha256": "...", "size": 1234567,
 "url": "https://hub.example.com/releases/rook-hub-412.salty.otter.tar.gz",
 "sig": "<ed25519>"}
```

The version uses the worker scheme `<build>.<adjective>.<noun>` (build = the
commit count, the name derived from the commit). The signature covers
`b"rook-hub-release-v1\n" + canonical(manifest without sig)`: the same key as
worker bundles, but a separate domain, so a hub release never verifies as a
worker manifest, grant or deauth order, and vice versa.
`rook hub release verify MANIFEST` checks a manifest (and a local artifact).

## Deploying

```sh
rook hub deploy dist/hub/rook-hub-412.salty.otter.json          # path or URL
rook hub deploy URL --services dashboard                        # only the dashboard
rook hub deploy URL --test tests/test_update_keys.py            # extra preflight tests
```

Steps, in order; any failure before step 7 changes nothing:

1. Take `state/deploy.lock` (one deploy at a time).
2. Verify the manifest signature, fields and version; refuse a downgrade
   unless `--allow-downgrade`.
3. Fetch the artifact (the signed `url`, else next to the manifest) and check
   size and sha256.
4. Unpack into a new `releases/<version>` (via a temporary directory and a
   rename; members outside `<version>/` are rejected). An existing directory
   is reused only if it was unpacked from the same artifact.
5. Preflight: with each service's interpreter and the release on
   `PYTHONPATH`, import every configured module and every submodule of the
   configured packages, and check each came from the release. Optionally run
   `--test` paths with pytest inside the release (pytest must be in the venv).
6. Check for stray drop-ins.
7. Snapshot the current selectors, back up the databases, write
   `rollback.sh`, record the deploy in history.
8. Arm the dead-man: a `systemd-run --on-active=<N>min` timer (systemd mode),
   or a detached `sleep` (symlink mode), that runs `rollback.sh --deadman`.
9. For each service in order: switch its selector, `daemon-reload`, restart,
   wait until healthy (unit active, effective release correct, health URLs
   pass), then confirm it is still healthy after `settle_seconds`. If
   systemd's `NRestarts` goes up meanwhile (the release crashes at start and
   `Restart=on-failure` brings it back), verification fails at once instead
   of waiting out `health_timeout`.
10. Disarm the dead-man and record success.

If step 9 fails, `rollback.sh` runs at once: it restores the selectors from the
snapshot and restarts only the services that had been switched. With
`--no-auto-rollback` the deploy stops as it is and the dead-man still fires.
If the deploy process itself dies (lost SSH session, killed terminal), nothing
disarms the dead-man, and the hub rolls back by itself after
`deadman_minutes`.

Restarting the MCP disconnects every agent. `deploy` asks first when a
disruptive service is included; non-interactive runs need `--yes`, or leave
the MCP out with `--services dashboard` and deploy it in a quiet moment.
Other options: `--no-restart` (switch only), `--parallel` (restart all, then
verify), `--deadman-minutes N`, `--no-db-backup`, `--skip-preflight`.

Databases are backed up but **not** restored by a rollback: a rollback that
threw away rows written since the deploy would be worse than the bug. Restore
a backup by hand (stop the service, copy the file from `state/deploys/<id>/db/`,
start) only when a release damaged data.

## Status, history, rollback, prune

```sh
rook hub status            # per service: selected, effective, active, previous; releases; armed dead-men
rook hub history -n 20
rook hub rollback          # every service back to its previous release
rook hub rollback --to 410.jolly.walnut --services dashboard
rook hub disarm [ID]       # cancel a pending dead-man by hand
rook hub prune --keep 5 [--dry-run]
```

`rollback` goes through the same path as deploy (backup, dead-man, ordered
restart, verification). The previous release of a service is the one it ran
before its last *successful* switch. Every deploy also leaves its own
`state/deploys/<id>/rollback.sh`, which works without Rook's code at all.

`prune` keeps the newest `keep` releases plus every release that is selected,
effective or a rollback target, deletes the rest with their tarballs, and
keeps the latest 20 deploy records (never an armed one).

## Moving a hand-deployed hub over

1. Write the config; set `root` to a new directory.
2. `rook hub units` and compare with the existing units. Keep the settings
   (environment file, `ExecStart` flags); drop anything that names a release.
3. `rook hub status` lists every existing drop-in that selects a release as a
   stray.
4. Build a release of the commit you run today and deploy it with
   `--adopt-strays`. The old drop-ins move into that deploy's backup; its
   `rollback.sh` restores them if needed.
5. Delete old release directories by hand once you are confident; `prune`
   only manages `<root>/releases`.

## Moving to one hub process

`rook hub serve` runs the dashboard and the MCP server in one process: one
interpreter, one band client, one set of stores, and the dashboard's account
pages call the MCP side in memory instead of over loopback HTTP. It listens on
the dashboard port (7005) for everything, and also on the old MCP port (8765,
`--mcp-bind`) serving exactly what `rook-mcp` served there, so reverse-proxy
routes (for example Cloudflare Tunnel sending `/mcp`, `/band`,
`/tokens/account-api`, `/authorize`, `/token`, `/.well-known/`, `/skill/` to
8765 and the rest to 7005), agent configs and workers keep working unchanged.
`rook-dashboard` and `rook-mcp` still work, so the old units stay a rollback
target until they are removed.

On a local hub with two workers joined (scripts/local-hub.sh, debug relay),
the dashboard and MCP processes took 65 MB + 92 MB RSS; the merged process
96 MB.

The runbook below moves a hub whose units are `rook-remote` (dashboard,
:7005) and `rook-band-mcp` (MCP, :8765), each with its own virtualenv, to one
unit `rook-hub`. Substitute your unit names and paths. Nothing changes for
clients until step 6, and steps 1 to 5 can be done ahead of time. Do it in a
quiet moment: step 6 disconnects every agent, as an MCP restart always did.

1. **Deploy a release that has `rook hub serve` to the old layout first.**
   `rook hub release build` + `rook hub deploy` as usual. Both old units then
   run code that also contains the merged entry point, and the compat entry
   points are proven on that release before the layout changes.

2. **Record what each process uses today.** Settings and files must be the
   same after the move:

   ```sh
   systemctl cat rook-remote rook-band-mcp            # ExecStart flags, EnvironmentFile, User
   systemctl show -p Environment rook-remote rook-band-mcp
   for u in rook-remote rook-band-mcp; do              # the stores each one has open
     ls -l /proc/$(systemctl show -p MainPID --value $u)/fd | grep -E '\.(db|json|token)$'
   done
   ```

   Both must run as the same user and agree on `ROOK_DATA_DIR`. If a store
   is not under it (for example the MCP's tokens under
   `/var/lib/rook-band-mcp`), pin each path explicitly for the new unit:
   `ROOK_MCP_PERSIST`, `ROOK_MCP_JOURNAL`, `ROOK_CHAT_DB`,
   `ROOK_ENROLLMENT_DB`, `ROOK_SETUP_PATH`.

3. **One virtualenv with both halves' dependencies.** Keep the MCP's venv (it
   has the larger set: mcp, starlette, uvicorn, pydantic) and add what the
   dashboard needs. This is additive; the running MCP is not affected:

   ```sh
   MCPVENV=/opt/rook-band-mcp/venv          # the venv rook-band-mcp's ExecStart uses
   REL=$(systemctl show -p Environment rook-band-mcp | grep -o 'ROOK_RELEASE_DIR=[^ ]*' | cut -d= -f2)
   $MCPVENV/bin/pip install "aiohttp>=3.9" "websockets>=12.0" "Pillow>=12.3,<13" \
       "pynacl>=1.5" "cryptography>=42" "PyJWT[crypto]>=2.13,<3" "mcp>=1.27.1,<2"
   PYTHONPATH=$REL $MCPVENV/bin/python -c \
       "import rook.hub.serve, rook.remote.bootstrap, rook.band_mcp.server; print('ok')"
   ```

   (The list is `[project.dependencies]` in that release's pyproject.toml,
   minus telesthete, which the MCP venv already has at the pinned commit.)

4. **Write the unit** `/etc/systemd/system/rook-hub.service`, with the same
   `User`/`Group` as the old units:

   ```ini
   [Unit]
   Description=Rook hub (dashboard + MCP server)
   After=network-online.target telesthete-hub.service
   Wants=network-online.target

   [Service]
   Type=simple
   User=rook
   # Both old environment files: the dashboard's and the MCP's settings.
   EnvironmentFile=-/etc/rook/remote.env
   EnvironmentFile=-/etc/rook/band-mcp.env
   Environment=PYTHONUNBUFFERED=1
   ExecStart=/opt/rook-band-mcp/venv/bin/python -m rook.hub.serve
   Restart=on-failure
   RestartSec=3
   # SIGTERM stops it cleanly (MCP sessions, /band peers, band client).
   TimeoutStopSec=30

   [Install]
   WantedBy=multi-user.target
   ```

   Carry over every `ExecStart` flag of both old units; the only renamed
   one is `rook-mcp --bind HOST:PORT`, which is `--mcp-bind HOST:PORT` here
   (or `ROOK_MCP_BIND`, which keeps its meaning). The relay comes from
   `ROOK_HUB_HOST`/`ROOK_HUB_PORT`, else `ROOK_HUB`. `ROOK_MCP_BIND` must stay
   `127.0.0.1:8765` (the default) so the Cloudflare routes to 8765 still
   land. Then the release selector, the same release the MCP runs:

   ```sh
   mkdir -p /etc/systemd/system/rook-hub.service.d
   sed 's/rook-band-mcp/rook-hub/g' /etc/systemd/system/rook-band-mcp.service.d/90-release.conf \
       > /etc/systemd/system/rook-hub.service.d/90-release.conf
   systemctl daemon-reload            # do not enable or start it yet
   ```

5. **Point the tooling at the new unit (not yet active).** In
   `/etc/rook/hub-deploy.json` replace the two services with
   `"hub": {"unit": "rook-hub.service", "health": ["http://127.0.0.1:7005/health",
   "tcp://127.0.0.1:8765", "http://127.0.0.1:8765/healthz"]}` and keep the old
   file as `hub-deploy.two-units.json` for a rollback. For the watchdog, set
   `ROOK_WATCHDOG_SERVICES=rook-hub,telesthete-hub,cloudflared` in its
   environment file (its probe URL, 8765, is unchanged). Both take effect at
   step 6.

6. **Switch** (about ten seconds without a dashboard or MCP; the relay and
   workers keep running, and workers on `/band` reconnect by themselves):

   ```sh
   systemctl stop rook-remote rook-band-mcp
   systemctl start rook-hub
   journalctl -u rook-hub -n 50 --no-pager     # "dashboard and MCP on ...", "MCP also on 127.0.0.1:8765"
   ```

   Stopping both first matters on a small VM: three hub processes at once
   would not fit, and the ports must be free.

7. **Verify**, on the hub and through the public URL:

   ```sh
   curl -s http://127.0.0.1:7005/health                       # band_workers > 0 after ~30 s
   curl -s -H "Authorization: Bearer $ROOK_MCP_STATIC_TOKEN" http://127.0.0.1:8765/healthz
   python3 -m rook.band_mcp.watchdog --mode hub --state /tmp/wd.json   # no alerts
   ps -o rss,args -C python3 | grep rook.hub.serve             # one process
   ```

   Then in a browser: sign in, open Workers (all listed), Tokens, Settings,
   Knowledge, Jobs, Vault (each lists without "service is unavailable"),
   send a dashboard chat message. From an agent: `rook_whoami`,
   `rook_workers`, a `rook_call`. From claude.ai's web connector (OAuth
   front door), reconnect once. Watch `journalctl -u rook-hub -f` and
   `free -m` for a while.

8. **Retire the old units** once satisfied (keep them installed for one
   release as the rollback path):

   ```sh
   systemctl disable rook-remote rook-band-mcp
   systemctl enable rook-hub
   ```

   Later, remove their unit files and drop-ins, and the dashboard's venv.
   The next `rook hub deploy` manages `rook-hub`'s drop-in.

**Rollback** (any time before the old units are removed):

```sh
systemctl stop rook-hub
systemctl start rook-remote rook-band-mcp
systemctl disable rook-hub; systemctl enable rook-remote rook-band-mcp
```

and restore `hub-deploy.two-units.json` and the watchdog's
`ROOK_WATCHDOG_SERVICES`. Data needs no rollback: both layouts use the same
stores in the same format.

**Later:** with every client on the public domain, the proxy can send the
whole domain to 7005 and `ROOK_MCP_BIND=off` drops the second port.

## Testing it

`tests/test_hub_deploy.py` covers manifests, unpacking, preflight, symlink and
(simulated) systemd deploys, stray detection, automatic and dead-man
rollbacks, status and prune, without a hub.

`scripts/hub-deploy-e2e.sh --dir DIR [--port-base P] [--deadman]` runs the
real thing against a throwaway test hub (docs/testing.md) whose dashboard and
MCP it moves into `systemd --user` units: first deploy, dashboard-only
deploy, a release that crashes at start, a stray drop-in, the dead-man timer,
rollback and prune, with the integration suite run against the deployed
release. It uses its own signing key and a private clone, and removes
everything afterwards unless `--keep`. Never point `rook hub` at a live
install to try it out.
