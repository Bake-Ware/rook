---
id: updates
title: Updates and keeping Rook healthy
order: 21
features:
  - features/hub-owned-updates-manage-updates
  - plugin/hub/updates
  - settings/updates
  - features/ota-self-update
  - features/post-update-health-gate
  - features/reliability
  - plugin/worker/selfupdate
  - settings/watchdog
since: 0.1.0
deprecated: false
---
# Updates and keeping Rook healthy

## Manage > Updates

**Manage > Updates** on the dashboard (operator account) is where you keep
the whole band up to date: the hub, your workers, the Android app and
firmware devices. It shows what each one runs, what the newest Rook release
is, its release notes, and a history of every update.

The hub checks Rook's releases on GitHub every few hours and tells you when a
new one comes out, on this page and in chat (Telegram or Discord, when set
up). **Check now** looks straight away.

### Updating the hub

A new release shows two steps: update the hub first, then your workers
(workers get their updates from the hub).

- **Update the hub to …** downloads the release, checks it, and installs it
  the way your hub was installed. A hub set up with `rook hub deploy` or with
  the one-command installer (per-user) can do this itself; if the new
  version does not start healthy, `rook hub deploy` puts the old one back.
  The page says when your hub cannot update itself (a system-wide install,
  Docker or a source checkout) and shows the command to run instead.
- A hub set up with `rook hub deploy` whose services run as system services
  under their own user cannot restart them itself (on purpose: the hub never
  gets administrator rights). It shows **Prepare the hub update to …**
  instead: the hub downloads and checks the release the same way, asks you to
  acknowledge a breaking update first, and leaves a signed copy ready on the
  hub machine. The page then shows one `sudo … rook hub … deploy … --yes`
  command with a **Copy** button. Run it on the hub machine: it checks the
  signature, backs up the databases, and rolls back by itself if the new
  release does not come up healthy. History records the update once the hub
  is back on the new release.
- The hub restarts, so the page reconnects after a minute. History records
  whether the update landed, and has a **Roll back** button.

### Breaking updates

Some releases stop supporting old devices: very old workers, an old Android
app, or old hubs. Rook calls these **breaking**. Before you update, the page
lists every device that would stop working, why, and how to fix it: update
that worker first (one button), install the new app on the phone, or
reinstall the machine.

The hub update stays switched off until you have dealt with those devices,
or ticked "I understand" and pressed **Acknowledge**. You are also asked to
acknowledge when Rook cannot tell what a release changes (an older release
without that information), and when you skip releases and one of the
releases in between was breaking: updates add up.

### Updating workers

The hub keeps a copy of each worker version it has had, and serves one of
them to workers: the *released* version. A newer version (after a hub
update) does not reach your workers until you roll it out:

1. Pick workers: all of them, one band, one kind of computer (Linux, Windows,
   macOS), or one by one. Optionally choose the test worker.
2. **Update.** The test worker updates first. The hub waits for it to come
   back on the new version, answer, and stay on it.
3. The rest follow a few at a time. The first worker that does not come back
   healthy stops the rollout before anyone else is touched.
4. A rollout to all workers releases the version, so workers that were off
   get it when they come back.

The **Phones and apps** tab can publish a release's Android app through your
hub and ask phones that are behind to update ([[manual-phones-and-tablets]]);
the **Firmware** tab shows firmware versions (flashing stays by hand).

You are told in chat when a new worker version is waiting. If you switch the
**updates** plugin off (Plugins page), or the update service stops running,
the hub goes back to offering its newest version to every worker.

### Settings

Under **Settings** on the Updates page (also Settings > Plugins > Updates):

- **Updates** (`updates.mode`): *off* never checks; *notify* (the default)
  checks and tells you; *auto* also installs updates by itself during the
  maintenance window. Auto mode is careful about the hub: it only installs
  a small bug-fix release of the version you run, a day after it became a
  stable release, never a breaking one, and only when it can prove the release was built by
  Rook's own release process (this needs the `gh` command on the hub). It
  rolls out new worker versions the same way you would, test worker first.
- **Release channel**: *stable*, or *prerelease* to also see test releases.
- **Maintenance window**: when auto mode may install, like `02:00-05:00`.
- **Canary workers**, **Workers at a time** and the health check window: how
  rollouts go.
- **Check build provenance** (`updates.verify_attestations`, advanced): also
  prove a release came from Rook's release process before a hub update you
  start (needs the `gh` command; the update is refused if the check fails).
  Every download is always checked against the release's checksums.

The full list, with defaults, is on the Settings page.

## Machines update themselves

When a new worker version is released (see above), the hub offers it to every
machine that is behind. Each machine checks that the update is signed by your
hub and has not been changed, tests it, installs it and restarts. You do not
need to visit each machine.

**Safety net.** After updating, a machine must prove it can still talk on the
band: it asks the hub a question and waits for the answer. If it cannot within
three minutes, it puts the previous version back and restarts into it, so an
update can never strand a machine where you cannot reach it. A version that
keeps crashing is also rolled back. A machine will not try a version it rolled
back again for a day (an hour if the problem was probably the network, not the
update). The Workers page and `rook_workers` show when a machine rolled back
and why.

If the hub itself goes down during an update, updating machines roll back and
try again later. That is the price of never losing a machine.

**Holding a machine back.** `worker.hold` keeps one machine on its current
version, for example while you test something. `worker.check` with `force`
makes one machine update now, useful for trying a version on one machine first.

Phones update through the app ([[manual-phones-and-tablets]]); the USB dongle
has its own firmware updates ([[manual-hardware]]).

## The hub keeps itself healthy

- **Agent connections are limited.** If too many agents are connected, the one
  that has been idle longest is disconnected (it reconnects by itself), so one
  misbehaving agent cannot lock everyone out.
- **The watchdog** (optional) checks the hub every minute: that agents can
  connect, that its services run, how many machines are online and how much
  memory is free. A second copy can run on another machine and check the hub
  from outside. Problems are sent to you once on Telegram (or through the
  hub's notifications), with an hourly reminder while they last and a message
  when they are fixed. **It is not set up for you:** neither the installer nor
  Docker runs it. An administrator schedules
  `python3 -m rook.band_mcp.watchdog --mode hub` once a minute (a systemd
  timer) with its settings in an environment file. Its settings are listed
  under **Watchdog** on the Settings page.

## Updating the hub

The easiest way is **Manage > Updates** (above). By hand, how to update the
hub depends on how it was installed; see [[manual-running-a-hub]]. After the
hub updates, it may tell you that this
manual changed and offer to update the copy in your knowledge base
([[manual-help]]).
