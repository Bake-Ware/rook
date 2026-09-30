# Deploying the hub (`rook hub`)

Workers update themselves from signed bundles. `rook hub` gives the hub the
same treatment: a signed release, a fresh release directory, preflight checks,
database backups, **one** release selector per service, a dead-man timer that
rolls back on its own, health verification, and a history you can roll back
from.

The hub runs two services:

| Service | Module | Restart impact |
|---|---|---|
| `dashboard` | `rook.remote.bootstrap` | the web UI reloads |
| `mcp` | `rook.band_mcp` | **every connected agent is disconnected** |

By default both are deployed, one at a time, dashboard first and MCP last. If
the dashboard fails verification the MCP is never touched.

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
ExecStart=/opt/rook-hub/venv/bin/python -m rook.remote.bootstrap
Restart=on-failure
```

Settings such as `ROOK_DATA_DIR`, `ROOK_BAND_PSK`, `ROOK_PORT`, `ROOK_HUB`
and `ROOK_MCP_BIND` go in the environment file (see docs/design/settings.md).

**symlink.** For installs that are not run by systemd (or units you would
rather point at a path): `<root>/current-<service>` points at
`releases/<version>` and is switched atomically with a rename. The service's
launcher sets `PYTHONPATH=<root>/current-<service>`. Each service needs a
`restart` command (any shell command), or a `unit` to restart with systemctl.

**Docker / compose** installs are out of scope: there the image is the
release. Build a new image and roll back by tag.

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
    "dashboard": {"unit": "rook-hub-dashboard.service",
                  "health": ["http://127.0.0.1:7005/"]},
    "mcp": {"unit": "rook-hub-mcp.service",
            "health": ["tcp://127.0.0.1:8765", "http://127.0.0.1:8765/healthz"]}
  }
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
mcp 90), `modules` and `packages` (preflight imports; defaults cover
`rook.remote`, `rook.band_mcp` and `rook.knowledge`), `health` (URLs:
`http(s)://` passes on any status below 500, so an auth-gated endpoint
answering 401 still counts; `tcp://host:port` passes when the port accepts),
`restart` (shell command instead of `systemctl restart`), `link` (symlink
path), `disruptive` (ask before restarting; default true for `mcp`).

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
   pass), then confirm it is still healthy after `settle_seconds`.
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

## Testing it

`tests/test_hub_deploy.py` covers manifests, unpacking, preflight, symlink and
(simulated) systemd deploys, stray detection, automatic and dead-man
rollbacks, status and prune, without a hub. For an end-to-end run use a
throwaway test hub (docs/testing.md) with its dashboard and MCP moved into
`systemd --user` units in a separate directory. Never point `rook hub` at a
live install to try it out.
