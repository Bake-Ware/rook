# Updates (Manage > Updates)

The hub keeps Rook up to date for the whole band: itself, the workers, the
Android app and (by hand) firmware devices. It checks Rook's GitHub releases,
tells you when a new one comes out, and updates things in a safe order when
you ask it to, or on its own during a maintenance window.

Open **Manage > Updates** on the dashboard (operator account).

## What you see

- **Overview.** The hub, workers, phones and apps, firmware and the relay,
  each with what it runs and whether something newer is available. A newer
  release shows its notes and two steps: update the hub, then your workers.
- **Workers.** Every worker with its build, and the rollout controls.
- **Phones and apps.** Android apps and their versions; ask them to update.
- **Firmware.** ESP32 and RP2040 dongles and their firmware versions.
- **History.** Every hub update, worker rollout, acknowledgement and app
  publish, newest first, with a button to roll the hub back.

## Settings

Under **Settings** on the Updates page (also Settings > Plugins > Updates):

| Setting | What it does |
|---|---|
| Updates (`updates.mode`) | **off**: never check. **notify** (default): check every 6 hours and tell you here and in chat (Telegram/Discord, when set up). **auto**: also install updates during the maintenance window. |
| Release channel (`updates.channel`) | **stable**, or **prerelease** to also get test releases (release candidates). |
| Maintenance window (`updates.window`) | Hub local time, like `02:00-05:00`. Auto mode only installs inside it. Empty: any time. |
| Canary workers (`updates.canary`) | Worker names that always update first. Empty: the first selected worker that is online. |
| Workers at a time (`updates.batch`) | After the canaries, how many workers update together (2). |
| Health check window (`updates.gate_secs`) | How long a worker has to come back healthy on the new build (300 s). |

Advanced: `updates.repo` / `updates.relay_repo` (GitHub repositories),
`updates.api_base` (`ROOK_UPDATES_API`, for GitHub Enterprise or a mirror),
`updates.token` (`ROOK_UPDATES_GITHUB_TOKEN`, optional, raises the rate limit),
`updates.verify_attestations`, `updates.check_hours`.

Auto mode never installs a **breaking** update (below): those always wait for
you. It tries each release and each worker build once; a failure is recorded
in the history and waits for you too.

## Updating the hub

Your workers get their updates from the hub, so the hub goes first. How it
updates depends on how it was installed; the page says which applies:

- **`rook hub deploy`** (docs/operations/hub-deploy.md): the hub repacks the
  release's source archive as a hub release, signs it with its own update key
  and runs `rook hub deploy` in a transient systemd unit (outside the hub's
  own service, since the deploy restarts it). Preflight checks, database
  backups, health checks and the dead-man rollback all apply: if the new
  release does not come up healthy, the hub goes back on its own. A hub whose
  units are system units and that does not run as root cannot do this
  itself; the page shows the command to run.
- **The one-command installer** (`scripts/install-hub.sh --user`): the hub
  downloads the release's own `install-hub.sh`, checks it, and re-runs it
  with `--version <tag>`. Data, keys and passwords are kept. A system-wide
  install (`--system`) runs as the unprivileged `rook` user and cannot
  reinstall itself: the page shows the `sudo` command to run.
- **Docker**: the image is the release. Pull the new image and recreate the
  container (`docker compose pull && docker compose up -d`).
- **A source checkout**: `git fetch --tags && git checkout <tag>`, then restart.

The update survives the hub restart: when the hub comes back on the new
release, the history records it; if it is still on the old one 20 minutes
later, the update is recorded as failed.

**Roll back** (History): `rook hub rollback` for a `rook hub` install, or the
installer again with the previous version.

## Updating workers

The hub keeps a copy of every worker bundle it has had, by build, and serves
one of them to workers that check in on their own: the **released** build.
When the hub gets a newer bundle (after a hub update), nothing happens to the
workers until you roll it out:

1. Pick workers: all, by band, by system (Linux, Windows, macOS), or one by
   one; and a test worker, or let Rook pick one.
2. **Update N workers.** The test worker updates first. Each worker checks
   the signed bundle, swaps it in and restarts; its own health gate undoes
   the update if it cannot reach the band. The hub then waits for it to come
   back on the new build, checks that it answers, and that it stays on it.
3. The rest follow a few at a time. The first worker that fails stops the
   rollout before anyone else is touched.
4. A rollout to **all** workers that finishes releases the build: workers
   that were offline get it when they check in. After a partial rollout,
   **Release build N to everyone** does the same by hand.

Workers that are pinned (`worker.hold`) are skipped; workers that cannot take
updates over the band (very old builds, the Android app's built-in worker) are
listed to update by hand. A worker cannot be moved back to an older build
from here (signed builds only go forward): its own health gate is what rolls
back a bad build.

Before this page existed the hub pushed its newest bundle to every worker as
soon as the bundle changed. Turning the updates plugin off restores that.
When the update service starts for the first time it releases the bundle the
hub already has, so nothing changes for the workers until the next build.

## Breaking updates

A release is **breaking** when it raises the oldest worker build, Android app
or hub build Rook still supports (`rook/core/compat.py`), or when its release
says so. The page then lists every device that would stop working with the
new hub, why, and how to fix each:

- a worker the hub can update now: **Update these workers first**;
- an Android app: install the new app on the phone (the app does not update
  itself from an old version; see docs/android-updates.md);
- anything else: reinstall it with the one-line install command.

The hub update stays disabled until those devices are updated, or until you
tick "I understand ... will stop working" and **Acknowledge**. The Workers page
also tags each device that is too old for this hub, or for a pending breaking
update, with a link to Updates.

## Phones and apps

The Android app updates itself from the hub's app feed (`/apk.json`), with
Android asking the owner to confirm when it must (docs/android-updates.md).
When a release carries an app (`rook-worker.apk` and `rook-worker-apk.json`),
**Publish the app** checks both against the release's `SHA256SUMS`, makes it
the app this hub serves at `/apk`, and asks each phone that is behind to check
now (`device.update`). `ROOK_PUBLIC_APK_SHA256`, when set, must still match.
A phone checks the feed its app was built for (`ROOK_SERVER`), so this reaches
phones whose app points at this hub.

**Desktop app.** It asks the hub `GET /updates/desktop.json?version=0.1.0&platform=linux`
(`linux`, `macos` or `windows`) and gets
`{"current", "latest", "update_available", "notes_url", "published_at", "downloads": [{"name", "url"}]}`;
downloads are the release's `rook-desktop*` files for that platform. Public,
like the release page itself.

## Firmware

The ESP32 dongle reports its firmware version in its announce; an RP2040
dongle is asked through the worker it is plugged into (`dongle.status`). The
newest versions come from the release's metadata. Rook does not flash
firmware: releases carry no firmware images, and the ESP32's own update page
needs the device's admin password. Flash by hand (the Firmware tab says how).

## Release metadata and trust

Each release publishes `release.json` (written by
`tools/release_prep.py --release-json`): version, build, channel, the
compat floors, whether it is breaking and why, the relay it pins, and the app
and firmware versions in it. The hub reads it before it offers an update.

Every download is checked against the release's `SHA256SUMS`, and a cached
file is checked again before use. That proves the file is what the release
published, not who published it: the release's build provenance attestations
do that, and need Sigstore's trust root to check. With
`updates.verify_attestations` on and the `gh` CLI installed, the hub runs
`gh attestation verify` on each download too; otherwise run it yourself
(`gh attestation verify <file> -R Bake-Ware/rook`). Worker bundles are signed
by the hub's own update key either way.

The hub asks GitHub at most every few hours, with the last `ETag` (an
unchanged list costs nothing), and backs off after errors (15 minutes,
doubling to a day, or what GitHub's rate limit says).

## Files

All in `$ROOK_DATA_DIR/updates` (`ROOK_UPDATES_DIR` to move it): `state.json`,
`history.jsonl`, `summary.json`, `github/` (release lists and verified
downloads), `worker/<build>/` and `feed.json` (the worker feed), `apk/`,
`hub/` (hub releases made from release archives) and `logs/` (output of hub
updates).
