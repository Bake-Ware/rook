# Core updates on 0.5

How a 0.5 band updates its workers' core without a person at each machine
(switchover gate R6, coverage gap N27), and how the desktop app finds a new
version on a 0.5 hub (R10). Plugins keep updating through distribution
(`POST /plugins/push`, docs/castling-e2e.md); the hub updates itself by its
own deploy (the sidecar's `install.sh --upgrade`), not through these caps.

Code: `rook/core2/release.py` (the format), `rook/core2/worker/boot.py`
(which core a start runs), `rook/core2/worker/update.py` (`worker.update`),
`rook/core2/hub/updates.py` (`update.*`), manifests
`rook/core2/builtin/worker.yaml` and `update.yaml`.

## The flow

1. Upgrade the hub (its deploy). The hub now runs the code to release.
2. `update.prepare` (owners): the hub signs a release of the code it runs
   (or of a checkout on the hub machine, `source`). Without `version` the
   release is called `<pyproject version>+<digest of the code>`, so the
   same code is the same release and preparing it again is a no-op.
3. `update.rollout {version}` (owners): returns at once. Canaries first
   (`canary`, default the first worker in line), one at a time, then the rest
   `batch` at a time (default 2). The first worker that fails stops it.
4. For each worker the hub offers the release over Drop and submits a
   **change record** (`change.submit`, as for plugin pushes) **as the person
   who started the rollout**: its one script is `worker.update {bundle}`, its
   way back `worker.rollback` (so `change.revert` undoes a kept update), and
   dispatch and the journal see every step. A change that waits for a card
   (a risky change) stops the rollout; once it is answered the worker updates,
   and the next rollout skips it. Without the change plugin in the hub's
   library a rollout is refused, like a push.
5. The worker pulls it, checks it (below), installs requirements it lacks,
   runs the new core's smoke check in a child process, and restarts into it
   on **trial**.
6. The new process confirms the trial when it has served a call from its
   home hub all the way through (ticket, gate, host, plugin), not merely
   heard it. The hub polls `worker.status`, which is such a call, until the
   worker reports the release running and confirmed, for up to `gate`
   seconds (default 300).
7. `update.status` follows it; `update.history` keeps every release,
   rollout and per-worker outcome.

```sh
H="Authorization: Bearer $TOKEN"
curl -s -H "$H" -d '{"worker":"rook","cap":"update.prepare","args":{},"why":"Release the hub code to the workers."}' localhost:8740/call
curl -s -H "$H" -d '{"worker":"rook","cap":"update.rollout","args":{"version":"0.5.0+3f0a9c1d2e3f","canary":["cachyrig"]},"why":"Roll out the new core."}' localhost:8740/call
curl -s -H "$H" -d '{"worker":"rook","cap":"update.status","why":"Follow the rollout."}' localhost:8740/call
```

Workers that can't take an update are listed as skipped, with why: offline,
already on the release, or no `worker.update` cap (the phone app's worker,
whose core comes with the APK, and workers on a core older than this).

## The release

`rook-core-<version>.tar.gz` holds `release.sig` first, then the files of
the `rook/` package plus the wheel's `force-include` entries that land inside
it (the manifest schema, the manual, the skill, the voice engine), so a
release holds what an installed wheel does. From a git checkout only tracked
and not-ignored files go in (no gitignored model files or local data); no
caches, no symlinks. About 6 MB.

`release.sig` is a `rook-release` signed object (spec 3.3 and 13): `hub`,
`version`, `core` (the protocol version the code speaks), `files` (path →
SHA-256), `requires` (the pyproject's dependencies) and `iat`, signed with
the hub's operational key. Unlike `plugin.sig` it names no worker: one
release, signed once, for every worker.

A worker accepts it only when, exactly as for a plugin bundle:

- the signature verifies under a current operational key of a hub that has
  role `home` or `co-owner` on one of its bands;
- the tarball's files are exactly `files`, each with its signed hash;
- its `core` version is compatible with the worker's;
- the same version isn't already installed with different files (bump it).

It unpacks into `<data>/core/<version>/`. Requirements it doesn't meet are
installed into the worker's own environment first (uv when present, else
pip), like a plugin's pins; they aren't removed on rollback. The smoke check
runs `python -P -m rook.core2.worker --config <its config> check` with the
release first on `PYTHONPATH`: the new code must import, read the config and
validate core's manifests, from the release folder.

## Trial, confirmation, rollback

`<data>/core/state.json` names `current`, `previous` (kept for rollback),
the `trial` and the last `failed` update. `null` means the installed package
(the base). At every start `python -m rook.core2.worker` picks its core
(`boot.select`) and execs into it with the release first on `PYTHONPATH`;
once running, it puts `PYTHONPATH` back, so programs it starts never see the
release. Every start re-checks the chosen release's file hashes; a damaged
release isn't run (the previous one, else the base, is).

The trial is dropped and the worker goes back to the core it had when:

- it serves no call from its home hub within `ROOK_UPDATE_TRIAL_SECS`
  (120 s) of starting (a hub outage counts too: nothing proved the new core):
  the worker restarts itself into `current`;
- it fails to start `MAX_BOOTS` (3) times: each start that picks the trial
  counts, and the fourth runs `current`. **This needs a service manager that
  restarts the worker when it exits** (systemd `Restart=always` or
  `on-failure`, the desktop app). A worker run by hand that crashes on a new
  core stays down until it is started again, then rolls back.

Either way `worker.status` reports `release.failed` with the reason, and the
rollout marks that worker failed with it and stops.

At start a release whose signing hub no longer approves code on the machine
(re-enrolled elsewhere, the band removed) is refused and the worker restarts
on its installed core.

`worker.rollback` swaps `current` and `previous` (the installed core when
nothing else is kept) and restarts. Only the current, previous and trial
releases are kept on a worker; the hub keeps its three newest releases.

## Safety

- `worker.update` and `worker.rollback` are `admin`, `scope: self`,
  `persists`: the worker's ceiling and elevation apply as for any admin cap.
  `update.prepare` and `update.rollout` are owners only.
- Both refuse while terminals or streams are open on the worker (they would
  end) with a `busy:` error; the rollout marks that worker failed. Close them
  and roll out again.
- A worker that can't re-exec itself (the phone app's) refuses: its core
  comes with its app.
- A rollout never offers more copies of a release at once than Drop holds
  per band (64 MiB), whatever `batch` says.
- A rollout cut short by a hub restart is recorded as `interrupted`; start it
  again (workers already on the release are skipped).

## The desktop app's update check (R10)

`GET /updates/desktop.json` (no token, like 0.4's) lists the
`rook-desktop-*` files of Rook's latest GitHub release, cached for six hours
(ten minutes after a failed fetch; the last good answer is kept). The app
compares versions and only offers links into Rook's GitHub releases, so
nothing changes in how far it trusts the hub.

## Not here (yet)

- The hub updating itself from a release (0.4's `apply_hub`): its deploy does
  it.
- Release channels, maintenance windows and auto mode: a person starts each
  rollout.
- Windows: `worker.update` refuses there for now. `os.execv` starts a new
  process and the old one exits, so whatever runs the worker (the desktop
  app) sees it end; that needs trying on a Windows machine first.
- The phone app's 0.5 mode updates through the APK feed (R8 and step 4 of
  docs/castling-switchover.md).

## Tests

`tests/test_core2_updates.py` (format, boot selection, updater, hub rollout,
the desktop feed) and `tests/e2e/test_updates_e2e.py`: a real hub and worker,
a release of this checkout rolled out and confirmed, `worker.rollback` by
hand, and two broken releases (one crashes at start under a supervisor, one
never confirms) that the worker rolls back by itself.
