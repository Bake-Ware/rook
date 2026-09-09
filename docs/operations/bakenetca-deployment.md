# BakeNetCA deployment layout

## Verified APK updater deployed, 2026-09-09

Both services run from `/opt/rook-releases/apk-ota-20260909-24aee7e`, source
`24aee7e`. Override backups are in `/var/backups/rook/apk-ota-20260909-24aee7e`.
The public APK is **0.4.1 (5)**, 142,838,632 bytes, SHA-256
`5f296a17e64dedaefd1a045fdb900f8de4c1ebc290a94fa78e1f967d39733fd2`.
The signing certificate is unchanged. `/apk.json` and `/apk` were fetched
publicly and matched the release metadata/hash. The desktop worker feed remains
**140.curly.newt**; this release does not require replacing desktop workers.

The native APK adds automatic six-hour checks while its worker runs,
`device.update`, and `device.update_status`. Installation validates package,
increasing version code, size/hash, and matching signer. Android confirmation
or install-source permission is handled by notifications. The Settings download
button is at the bottom, alongside the automatic-update toggle and permission
link. Existing APKs need one manual upgrade to acquire this updater.
See [Android updates](../android-updates.md) for setup and publishing.

All 143 Python tests passed, including approved/missing/mismatched APK manifest
cases. Android 14 emulator verification accepted a same-signer code-6 test APK
and rejected version mismatch and a wrong signer. Denying install permission
produced a notification which opened Android's permission prompt. Granting it
allowed a silent code-5 → code-6 replacement; the worker rejoined the isolated
test band with the same stable ID and its foreground service running.
The production code-5 APK then answered `device.update` over that isolated band,
fetched the live HTTPS manifest, and reported `current` through
`device.update_status`. Test APKs and keys were never published; no physical
Android devices were modified by these checks.

## Android 0.4.0 and Codex capabilities deployed, 2026-09-09

Both services run from `/opt/rook-releases/mobile-codex-20260909-21441c3`, source
`21441c3`. Override backups are in
`/var/backups/rook/mobile-codex-20260909-21441c3`; the prior overview release is
retained. The APK hash allowlist was updated with the service release.

The public `/apk` download is APK **0.4.0 (4)**, SHA-256
`56f2f8d707964c5a9e80c901111ba6aba491e364bd56678e74165429ffa4feb5`.
Its signing certificate is unchanged. Version labeling, Settings update link,
web-style palette/panels, active background location, Google Maps result link,
and bounded find-device ringing are documented in
[the release guide](../worker-codex-android.md). Android 14 emulator tests passed
for GPS with Maps stopped and max-volume ring with timed/manual volume restore,
including the actual Chaquopy capability bridge. The update button opened
Chrome. Physical-device GPS, audible output, and Google sign-in were not
retested for this release; the prior APK had been user-verified.

Signed worker **140.curly.newt**, SHA-256
`3f2d12bcf6fdd2b9bcc132d891ff6879e383eb9e58d8dbfe91e0216c7385b2e3`, adds
seven `codex-history.*` capabilities matching Claude's history/resume operations.
The web Sessions page now selects either agent. APK release metadata travels
through worker announces, MCP lists, and the web roster independently of worker
build numbers. The full Python suite passed (143 tests); metadata follow-up
checks and browser tests for the agent picker, Maps link, and APK badge passed.

The server was deployed with the previous worker feed. Cachyrig then passed the
signed in-band canary, completed its health window, and answered
`codex-history.pull` through Rook. Both worker download artifacts and the signed
manifest were published afterward. All 25 compatible workers reached build 140
and finalized their update health windows, verified through read-only Rook calls.
The two online native Android workers still need the normal APK upgrade.

## Cached overview and responsive terminal deployed, 2026-09-09

Both services run from `/opt/rook-releases/overview-20260909-8bf7f1f`, source
`8bf7f1f`. The operator-only `/api/band/overview` endpoint combines the existing
in-memory roster with shared cached worker chat summaries. A demand-driven
server collector polls at most four workers concurrently, with a 15-second
interval between rounds and a 90-second idle cutoff. HTTP requests return
without awaiting those calls. Band filtering, worker moves, bans, expiration,
and bounded previews are covered by tests.

The terminal fetches one overview about every two seconds off its input thread.
It also polls active conversations in the background, discarding responses on
room changes. The previous synchronous, per-worker chat scans are removed.
The 139-test suite passed, followed by the additional room-switch regression
test; blocked-network tests exercise navigation, typing, quit, retained cached
rosters, and shared bounded collection across concurrent readers.

Cachyrig passed the signed in-band canary and update health window. A real
pseudo-terminal using its installed bundle and live dashboard login measured
402 ms to first frame, 0.7 ms for selection movement, and 32 ms to quit, with
the background worker PID unchanged. Five live aggregate requests had a median
204 ms response time while returning 26 workers and seven cached chats.
Unauthenticated overview access returned 401.

Signed worker build `138.cranky.heron` is published through both worker download
endpoints, with signature and SHA-256 verified:
`96cbf60d4561239cc8205c1dd4a42ede3e04acdee93c83fbbddd8390973960b1`.
The server endpoint was deployed with the old worker feed first; the new worker
feed was published after canary verification. Override backups are under
`/var/backups/rook/overview-20260909-8bf7f1f`, and the prior release remains intact.
All 25 compatible workers converged to build 138 and completed their update
health windows, verified through read-only Rook calls.
The APK, band credentials, and migrations are unchanged.

## Bundled desktop dashboard deployed, 2026-09-09

Both services now run from `/opt/rook-releases/cli-20260909-6756198`, source
`6756198`. Signed worker build `135.sparse.toucan` includes the terminal dashboard
and a managed `rook` launcher. New worker installs explicitly install the CLI;
installed workers also install/repair it at boot after an OTA update. The launcher
uses the worker venv and current bundle, with PATH setup for Bash, Zsh, Fish,
and Windows. The dashboard keeps its separate saved dashboard login. Native
Android runtimes are excluded, and the published APK is unchanged.

All 131 Python tests passed. A real local pseudo-terminal verified the bundled
launcher, dashboard authentication/rendering, private saved config, and clean
exit. Cachyrig received the signed bundle through an in-band canary transfer;
a fresh Fish shell resolved `rook`, opened the live dashboard with its existing
login, and quit without changing the background worker PID. The updater completed
its health window before the general release. Windows launcher generation was
tested, but physical Windows installer execution was not performed.

The public manifest signature and both worker download hashes were verified:
`f4efbbdb776613472f9967d510fab05dc9660be18517bf215eab1c9009b73734`.
The normal update feed now distributes this build to compatible workers.
All 25 compatible workers converged to build 135; read-only checks confirmed
the managed launcher on every host and completed update health windows. The
two online native Android workers remained on their existing APK runtimes.
Service override backups are under `/var/backups/rook/cli-20260909-6756198`;
the previous release remains intact. No band migration or credential rotation
was performed. This release also deploys `da515cd`: Install uses the shared 3D
rook and its pause control in place of the static illustration.

## Hand-drawn wireframe deployed, 2026-09-09

Both services now run from `/opt/rook-releases/ink-20260909-086249e`, source
`086249e`. The rook uses irregular black structural edge strokes, generated once
in object space, with a faint retraced pass. Toon material and a stationary
shadow-casting directional light replace the procedural hatch shader. The crown
casts real shadows on the shaft as the object rotates. The original sidebar
chess glyph is restored; the larger Install illustration remains.

The dashboard browser harness and WebGL lifecycle checks passed. The latter
counts main framebuffer draws separately from shadow-map rendering. Public HTTPS
checks verified the scene, restored brand, pause/resume, worker controls, and
mobile layout with no JavaScript errors. Worker build 125 and APK are unchanged.
Backups are under `/var/backups/rook/ink-20260909-086249e`; the prior layout release
remains available for rollback. Temporary verification session and SSH access
were removed after testing.

## Compact inventory and illustrated branding deployed, 2026-09-09

Both services previously ran from `/opt/rook-releases/layout-20260909-a2e07e6`.
Source `a2e07e6` includes pinned worker filters/group/sort/view controls and compact
status cards, grouped masonry/list views with a remembered preference, nested
capability disclosures, a sidebar narrowed by 40px, and left-aligned half-width
Bands, Tokens, and Sessions workspaces on desktop. The rook shader uses layered
ink strokes for shadows. User-supplied artwork, prepared against the site background,
appears beside the wordmark and on Install; the rotating scene pauses on Install.
Follow-up `1205361` keeps charging battery badges within masonry cards.

Local browser checks passed for grouped layouts, keyboard capability disclosure,
view persistence, pinned controls, charging batteries, account/token workflows,
artwork, and responsive pages. Six band-management tests and the dedicated WebGL
lifecycle harness passed. Public HTTPS browser checks verified the layouts,
controls, artwork, and responsive pages with no JavaScript errors. Temporary
operator verification sessions and SSH access were removed after testing.

Worker build 125 and the APK are unchanged. Backups are under
`/var/backups/rook/layout-20260909-a2e07e6`; the preceding art release remains
available for rollback. Image preparation provenance and prompt are recorded in
`rook/web/scene/illustration.md`.

## Sketchbook rook background deployed, 2026-09-09

Both services previously ran from `/opt/rook-releases/art-20260909-1e36124`, source
commit `1e36124`. This web-only release adds a self-hosted Three.js rook with
faceted shading, cross-hatching, drafting guides, and slow rotation. The art
pauses in hidden tabs, defaults to still on mobile/reduced-motion settings, and
retains an SVG sketch when WebGL is unavailable. Users can toggle motion.

The dashboard browser harness, six band-management tests (including conditional
asset requests), and dedicated WebGL lifecycle checks passed. Public HTTPS
browser checks verified rendering, pause/resume, worker menu interaction, and
mobile layout with no JavaScript errors. Signed worker build 125 and the APK
are unchanged. Backups are under `/var/backups/rook/art-20260909-1e36124`;
the descriptions release remains available for rollback. Temporary verification
session and SSH access were removed after testing.

## Persistent worker descriptions deployed, 2026-09-09

Both services previously ran from `/opt/rook-releases/descriptions-20260909-030efbc`.
Source commit `030efbc` publishes signed worker build `125.murky.tractor`, SHA-256
`b8f38dce8e6dce2cda744fdb69ad0d0fc1f9e61d9297004b8e06b0787a7b8394`.
The public manifest signature and downloaded artifact hash were verified.

All 124 tests passed, plus local dashboard browser checks. A disposable worker
running the actual signed bundle set/read/cleared its description over the band
and retained it across a new-process restart with the same worker ID. Public
MCP and HTTPS dashboard checks verified the description in the worker list,
Edit description menu, search, and mobile layout. The canary was cleared and
stopped; its temporary worker state was removed. No permanent device roles were
assigned during deployment.

All 24 compatible workers advertised the new description capabilities after
rollout. Native Android workers still need an APK update; the public APK is
unchanged. Unit, SQLite, and token-store backups are under
`/var/backups/rook/descriptions-20260909-030efbc`. The previous integrated
settings release remains available. Temporary verification sessions and SSH
access were removed after testing.

## Integrated account, tokens, and device inventory deployed, 2026-09-09

Both services previously ran from `/opt/rook-releases/settings-20260909-fdbbbea`.
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
