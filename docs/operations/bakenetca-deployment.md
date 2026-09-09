# BakeNetCA deployment layout

Verified read-only through Rook on `bakenetcanada`, 2026-09-09. Historical
instructions were retrieved from Sojourn’s Hermes reference
`/root/.hermes/skills/devops/rook/references/bakenetca-deployment.md` and its
Claude memory `bakenetca-rook-pyz-deploy.md`. Do not copy credentials from those
notes into repository files or command logs.

## Current services

Both `rook-remote.service` and `rook-band-mcp.service` run as `ubuntu`, with
`WorkingDirectory` and `PYTHONPATH` pointing to:

```
/opt/rook-releases/enrollment-20260908-migration-118
```

The service overrides are:

```
/etc/systemd/system/rook-remote.service.d/90-enrollment-upgrade.conf
/etc/systemd/system/rook-band-mcp.service.d/90-enrollment-upgrade.conf
```

The clean `master` checkouts at `/home/ubuntu/rook` and
`/opt/rook-remote/rook-src` still exist, but updating them alone does **not** update
the currently running release. The older worker artifact under the second
checkout was dated September 1 during verification.

## Preparing the band-management release

1. Verify the active units and overrides again. Preserve their configuration,
   credential file paths, and existing signed artifact configuration. Back up
   shared enrollment SQLite state using SQLite’s backup API before schema changes.
2. Prepare a new release directory from the reviewed source, including
   `rook/remote/band_web.py` and `rook/remote/bands.html`. Keep the previous release
   available. Both controllers share enrollment state and need the updated
   migration schema and credential-change guards.
3. Build the worker artifact from that exact source with
   `rook/remote/build_band_worker.py`. Its Telesthete input normally resolves to
   `../telesthete/telesthete`; the historical symlink at
   `/opt/rook-remote/telesthete` points to
   `/opt/rook-band-mcp/telesthete-src`. Validate the build input for the new release,
   and use the established signing and artifact-publishing process. The new move
   capability also requires rebuilding native Android APKs before those devices
   can move; do not distribute a Python zipapp as an Android application update.
4. Switch the service overrides to the prepared release through the established
   release process, reload systemd, and restart the affected services. Check logs,
   `/health`, the account-scoped Bands page, and the contents/hash/signature of
   the **served** worker artifact. Do not infer the served path from an old build
   directory: recent releases use staged enrollment artifacts.
5. Verify new workers advertise `worker.enrollment_move_prepare`. Exercise a
   disposable worker move before migrating an actual fleet. Existing workers
   need the compatible worker build; publishing the page alone does not add
   that capability to running workers.

A worker move or PSK migration persists progress in shared enrollment state.
Cancel prepared jobs before rolling back code. Activated jobs require forward
recovery; reverting to code that does not understand cross-band migrations can
strand workers. Preserve the database and its retired-key history.

## Historical layout and gotchas

The older MCP layout copied Python files from `/home/ubuntu/rook` into
`/opt/rook-band-mcp`. The older web/worker layout ran and built directly from
`/opt/rook-remote/rook-src`. Those instructions predate the current release
switch and must not be used as the sole deployment steps.

Restart managed workers through their existing systemd service. Launching a
second `nohup` worker alongside the managed one causes duplicate workers and
racing replies. For public WebSocket checks, use an actual WebSocket client or
HTTP/1.1 Upgrade; plain HTTP requests and HTTP/2 curl checks can give misleading
404 responses on a valid WebSocket route.
