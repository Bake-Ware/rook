# BakeNetCA deployment layout

## Integrated account, tokens, and device inventory deployed, 2026-09-09

Both services now run from `/opt/rook-releases/settings-20260909-fdbbbea`.
Account and token management are embedded in the main dashboard with styling
adapted from the local BakeDash project. Token metadata, creation, revocation,
and identity pictures use the MCP process's live token store via an
operator-authenticated same-origin proxy. Shared account sessions come from the
existing enrollment database; no separate browser login to MCP is needed.

All 121 tests passed. Local browser checks covered profile edits, pairing and
invitation dialogs, token creation/revocation, picture upload/clear, worker
menus, device grouping/sorting, and responsive layouts. Public HTTPS checks
created a disposable token, initialized an MCP connection with it, revoked it,
and verified subsequent authentication returned 401. Its picture was uploaded
and cleared. Existing `/tokens` and OAuth metadata HTTP routes still responded.
The live inventory classified 24 Linux workers and two Android devices via
read-only capability calls. Keyboard menus and responsive views passed with no
JavaScript errors. Follow-up styling aligned worker columns and stopped labeling
native Android development builds as firmware.

Signed worker build 120 and the public APK remain unchanged. No workers were
migrated by this deployment. Backups of unit overrides, SQLite, and the token
store are under `/var/backups/rook/settings-20260909-fdbbbea`; the previous shell
release remains available. Temporary test credentials, picture, and SSH access
were removed after verification. See `docs/web/dashboard.md` for service routing
and component boundaries.

## Dashboard shell deployed, 2026-09-09

Both services previously ran from `/opt/rook-releases/shell-20260909-cf7bc93`.
The dashboard includes worker band labels, shared sidebar navigation, and
an embedded band-management component. Source release `cf7bc93` passed all
117 tests and local desktop/mobile browser checks. Public HTTPS browser checks
verified real worker labels, the embedded dialog, and layouts at 1440, 1100,
820, and 390 pixels with no JavaScript errors. A follow-up stylesheet adjustment
keeps worker action labels on one line while wrapping the action buttons.

This is a web-only update: signed worker build 120, its manifest, and the APK
are byte-for-byte unchanged. No workers or bands were migrated by this update.
Previous source and artifacts remain at `/opt/rook-releases/bands-20260909-ce8aa7d`;
unit and SQLite backups are at `/var/backups/rook/shell-20260909-cf7bc93`.
Temporary verification sessions and SSH access are removed after verification.

## Band management deployed and verified, 2026-09-09

Both services initially ran from `/opt/rook-releases/bands-20260909-ce8aa7d`,
source commit `ce8aa7d` on `feature/band-management`. The signed worker build
`120.steady.iguana` is published at the existing worker download endpoints.
Its SHA-256 is
`ba8f76f1e505c64cf525c3c73ae315c5ae50d0a6d8cf66e47e963fb55794585f`.

An isolated worker exercised the actual public HTTPS API: inline band creation
and move, rename, PSK migration, migrate-all back to its original band, and
deletion. All three migrations completed with signed destination-channel proofs
and the same worker identity. The temporary service, Linux user, account,
sessions, enrollment, and SSH key were removed; test bands remain only as
retired tombstones. No production workers were moved between bands.

After publication, all 24 compatible workers reported build 120 and the move
capability. Three native Android installs still reported `0.dev` and require an
APK update; the existing public APK was preserved. Both services were active,
and the three original active bands were unchanged.

The previous release below is retained. SQLite and configuration backups are at
`/var/backups/rook/bands-20260909-ce8aa7d`. The deployed release contains
`deployment-result.json`; the durable Rook test console is `6875c8bce42f4d96`.
The desktop froze before starting the browser migration; live migration checks
continued on the server and passed independently of the desktop session.

## Historical discovery

Verified read-only through Rook on `bakenetcanada`, 2026-09-09. Historical
instructions were retrieved from Sojourn’s Hermes reference
`/root/.hermes/skills/devops/rook/references/bakenetca-deployment.md` and its
Claude memory `bakenetca-rook-pyz-deploy.md`. Do not copy credentials from those
notes into repository files or command logs.

## Previous service layout

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
