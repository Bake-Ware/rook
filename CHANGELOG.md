# Changelog

All notable changes to Rook are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are
tagged `vX.Y.Z` (the Python package version); workers identify themselves by
build number (for example `111.stinky.goat`). Before the first tag the package
version was `0.1.0`.

## Unreleased

Preparation for public use.

### Worker CLI and onboarding
- `rook` on a worker machine gained commands for the person sitting at it:
  `status`, `doctor` (plain-language fixes; exit 1 on a problem), `ask` and
  `chat` (the home agent, through the saved dashboard login or the worker's
  band link), `tasks`, `rooms`, `knowledge search|get`, `plugins
  list|enable|disable` (the hub's protected plugins stay on), `logs [-f]
  [--audit]`, `pair <code>`, `login` and `completion bash|zsh|fish|powershell`.
  Both the bundle's launcher and the package's `rook` have them. See
  docs/operations/worker-cli.md.
- The worker serves a local control link for them on 127.0.0.1 (token in
  `<state dir>/control.json`, mode 600; `ROOK_CONTROL=0` turns it off) and
  keeps its recent log lines in memory for `rook logs`.
- Enrollment prints numbered steps and "Done: <worker> joined <band>", prompts
  only at a terminal (`--yes` / `ROOK_YES=1`, `--band` / `ROOK_BAND`), sends
  the worker's `--name`, and reports failures in one plain sentence.
- Worker installers: plain step names, details in `install.log`, "Done:
  <worker> joined <band>"; unattended runs (piped, cloud-init, systemd, CI,
  `--yes`) never prompt and get a user service; keyboard/mouse setup is
  skipped on machines without a screen (`ROOK_HID=0|1`); Windows installs a
  missing Python 3.12 from python.org for the current user after checking a
  pinned SHA-256 (then `winget --scope user`, then one clear instruction).
  The `/install` and `/hub` installers no longer die under cloud-init at a
  prompt.
- A password-only hub's sign-in page asks for the password alone. The setup
  wizard and install-hub.sh's last screen use plain words; the wizard fills
  in its own address.

### Hub merge
- One hub process: `rook hub serve` (console script `rook-hub`) runs the
  dashboard and the MCP server together on the dashboard port (7005): the MCP
  paths (`/mcp`, `/band`, `/tokens/account-api`, `/authorize`, `/token`,
  `/.well-known/*`, `/skill/*`, `/healthz`) are served there through an
  in-process ASGI bridge, and the old MCP port (8765) keeps serving the MCP
  server alone (`--mcp-bind` / `ROOK_MCP_BIND`, `off` to drop it), so proxy
  routes, agent configs and workers need no change. Both halves share one band
  client, authorizer and set of stores, and the account pages reach the MCP
  side in memory instead of over loopback HTTP: on a local hub with two
  workers, 96 MB RSS instead of 65 + 92 MB. `rook-dashboard` and `rook-mcp`
  still work as separate processes (compat; to be removed in a later
  release). Migration runbook: docs/operations/hub-deploy.md.
- `scripts/local-hub.sh` starts the relay and one hub process
  (`ROOK_HUB_SPLIT=1` for the old two); installers and the printed MCP URL use
  the dashboard port. `compose.yaml` has one `hub` service (upgrade with
  `docker compose up -d --remove-orphans`).
- Upgrade note (Docker): the image's default command changed from
  `rook-dashboard` to `rook-hub`, which runs the MCP server too. A setup that
  starts the image without a command as its dashboard and also runs a
  separate `rook-mcp` container on the same data volume must drop that
  container (or give the dashboard container `command: ["rook-dashboard"]`):
  two MCP servers on one volume would share the stores and both join the band.
- The MCP paths refuse request bodies over 16 MiB with 413
  (`--mcp-max-body` / `ROOK_MCP_MAX_BODY`, 0 = no limit), and answer 503
  while the hub shuts down.
- `rook hub` deploy configs can name a single `hub` service
  (`rook.hub.serve`); the watchdog's unit list is configurable
  (`ROOK_WATCHDOG_SERVICES`).
### Release pipeline and hub install
- One-command hub install: `curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash`
  sets up Python, Rook from the release wheel, the prebuilt relay, generated
  secrets (mode 600) and two systemd units, `rook-relay` and `rook-hub` (the
  single-process hub; `--system` or `--user`), waits for
  health and prints the dashboard URL and password. Re-runs upgrade in place;
  `--uninstall [--purge]`; `--no-systemd` for containers and CI. See
  docs/operations/install.md.
- Releases: a `vX.Y.Z` tag publishes the sdist and wheel (version from the
  tag), a GitHub Release, and `ghcr.io/bake-ware/rook` for amd64 and arm64.
  Build names (`<build>.<adjective>.<noun>`) are unchanged; a hub installed
  from a wheel now stamps its worker bundles with the release's build number
  (they were build 0, so their workers never updated).
- The Docker image uses the prebuilt, sha256-checked relay from a telesthete
  release (`RELAY_SOURCE=source` compiles it as before), and builds again: the
  build context lacked a file `pyproject.toml` force-includes. `compose.yaml`
  runs the published image; `compose.build.yaml` builds from a clone.
- The release wheel requires telesthete by hash-pinned wheel URL instead of
  git, so installing it needs no git.
- CI: pytest on Python 3.11 and 3.12, generated-file checks, `node --check`,
  shellcheck, and an installer end-to-end run. Voice-service tests skip when
  that service's packages are missing instead of failing.
- The relay is pinned (`RELAY`, and each archive's sha256 in `RELAY_SHA256`,
  in `rook/_release.py`); the installer, `scripts/fetch-relay.py` and the image
  verify downloads against that pin, and the installer checks the Rook wheel
  against the release's `SHA256SUMS`. Releases publish as prereleases and are
  marked latest only after the image and an install test pass; artifacts and
  images carry build provenance attestations. Base images are pinned by digest.
- Installer: `--uninstall --purge` asks first (or needs `--yes`) and refuses a
  directory that isn't a hub data directory; `--user` never uses sudo; it never
  signals a pid that isn't its own service; it only falls back to building from
  git when there is no release at all; it warns when the hub would listen on a
  public address.

### Protocol features
- Addressed routing (telesthete SPEC §10.1, needs telesthete 0.3.0): workers
  and band clients declare a route id; targeted calls and replies (request
  `rr`) go only to their addressee on a routing relay. Old relays and peers
  see the same traffic as before. `ROOK_BAND_ROUTE=0` turns it off.
- Large replies (> 32 KB, `ROOK_BAND_DROP_THRESHOLD`) ride telesthete Drop
  with receiver-driven retransmit when the caller accepts it; `BandClient.call`
  returns the normal reply. A 1 MB reply now completes under 1% fragment loss.
  Worker replies are capped (`ROOK_BAND_MAX_REPLY_BYTES`, 32 MiB) with a clear
  error.
- Negotiated zlib compression of whole requests/replies (feature `z`), which
  also carries base64 attachments (screenshots, camera, files) compactly.
- In-band OTA uses one native DROP frame per Drop packet toward workers that
  announce `drop-native` (half the frames); `ROOK_BAND_DROP_NATIVE=0` opts out.
- Announces carry `features` and `caps_hash`; clients keep known caps across
  hash-only announces (opt-in on workers with `ROOK_ANNOUNCE_CAPS_HASH=1`
  after every client is upgraded).
- Workers can dial the relay's native WebSocket listener (`--ws-url`,
  `ROOK_HUB_WS_URL`; installers pass it when `ROOK_HUB_PUBLIC_WS_URL` is set).
  The `/band` bridge stays the default and gets 1 MiB socket buffers.
- telesthete pin (and the hub image's relay) moved to 0.3.0 (ad2865b); bundle
  builders refuse a vendored telesthete without `protocol/route.py`.

### Transport
- Band transport split into a shared frame codec (`rook.worker.wire.codec.
  BandCodec`) and thin UDP/WS links behind the unchanged
  `TelestheteHubTransport` facade. Frames shorter than header + AEAD tag
  (43 B) are now dropped before decryption (was `< 27`).
- One writer task per transport replaces the send lock that held the link for
  every fragment of a message: single-fragment messages (keepalives,
  announces, most replies) go first, multi-fragment messages are interleaved
  fragment by fragment, so a multi-MB reply no longer delays everything else
  for seconds. A call that times out before its message starts sends nothing;
  one that started is finished. Wire format unchanged.
- UDP bulk frames are paced (`ROOK_BAND_UDP_PACE_RATE`, default 2000
  frames/s, `ROOK_BAND_UDP_PACE_BURST` 64) and UDP sockets ask for 4 MB
  buffers, so a large reply no longer overflows the relay's shared queue.
- Keepalives are skipped while other frames flow.
- Inbound dispatch is bounded (`ROOK_BAND_DISPATCH_CONCURRENCY`, default 64,
  plus `ROOK_BAND_DISPATCH_BACKLOG` 1024); overflow is dropped with a
  rate-limited warning.
- `rook.core.backoff.Backoff` (jittered exponential) is used by the WS band
  reconnect, the terminal follower, the Android worker runtime and the KVM
  bridge serial stream, which now reconnects after the dongle reboots.
- Removed the unused `rook.worker.wire.channel` compatibility shim.
### Transport follow-up
- Addressed traffic (targeted requests, replies to `rr`, pass-through Drop
  to a declared route) is sent as telesthete ADDRESSED (0x06) frames, sealed
  with empty AAD so deployed receivers still open them when an old relay
  broadcasts them; the routing relay now routes only ADDRESSED frames, and
  CHANNEL frames are always broadcast. Only peers announcing `route` are
  addressed. Receivers open only CHANNEL and ADDRESSED frames.
- Inbound bursts no longer lose replies: receive loops yield every 32
  frames, only new requests can be shed (a worker answers a shed targeted
  request `busy`), and replies, announces and Drop packets get backpressure
  instead. 3,000 queued replies: 1,912 dropped before, 0 now.
- Large-reply pulls: `BandClient.call`'s timeout now bounds the Drop pull
  (a pull that can't finish returns `ok: false`, `timeout: large reply ...`);
  pass-through Drop packets go out in order (no window stalls on the
  receiver's replay check); the receiver is linear-time (telesthete fix, 32
  MiB pull ~28 s CPU -> 0.3 s); per-drop-id pull locks are released.
- Workers release unpulled reply drops at their TTL on a timer and hold at
  most 32 MiB of them (was 128 MiB, pruned only on the next large reply).
- In-band OTA refuses `drop_id` 0x8000 and up (large-reply ids).
- KVM bridge: malformed stream messages are skipped instead of ending the
  stream, and the old socket is closed before reconnecting.
- `ROOK_ANNOUNCE_CAPS_HASH` must stay off until every hub (> build 426) and
  APK (> 0.4.1) reads `caps_hash`; documented in core spec 14.5.
- telesthete pin and the hub image's relay moved to f3419d2 (audit/hub; move
  to the merged main commit after the telesthete PR lands). Bundle builders
  refuse a telesthete git checkout at any other commit
  (`ROOK_TELESTHETE_ANY_REV=1` to override).

### OTA health gate
- After an OTA update, a worker keeps the new build only once it has completed
  a band round trip, meaning a reply to its own request (it probes
  `caps.describe` on the hub node or another peer it heard announce). Before,
  staying up for 60 s was enough, so a build with a broken transport stranded
  the worker off-band.
- With no round trip within `ROOK_UPDATE_HEALTH_SECS` (setting
  `core.worker.update_health_secs`, default 180 s, 0 = off), the worker
  restores `band-worker.pyz.prev` and restarts into it at once. The reason goes
  to `update_state.json`, a new `update_result.json` and the log.
- The restored build reports `hb.worker.update = "rolled back: no band
  contact: …"` in its announce (shown by `rook_workers`), and `worker.status`
  returns `update.state` / `update.last`.
- A rolled-back build isn't re-applied by the hub push, `worker.apply`,
  `worker.ota_begin` or the poll loop for 24 h (`action: skipped`;
  `worker.check(force=true)` overrides). Pushes during verification are
  skipped as well, so `.prev` is never overwritten by an unverified build.
- The gate applies only if the old build heard the band in the 10 minutes
  before the swap, so a hub that was already unreachable doesn't make every
  worker roll back a good build. Updates from releases older than the gate
  get the old 60 s alive check. Crash-loop rollback (3 boots) and the wire
  format are unchanged.
- Review follow-ups:
  - The gate is also skipped when the old build had nobody to probe (a hub
    without its hub node) or the swap is over 10 minutes old.
  - A band that is heard but has no announcing peer keeps the build on the
    alive check instead of rolling it back.
  - A suspend gap of more than 30 s extends the window.
  - Late probe replies count.
  - After a rollback, the restored build rechecks the band. If it can't reach
    it either, the result is `inconclusive` and the build is held back 1 h,
    not 24 h.
  - Plugin reloads, and restarts after the run stayed up 60 s, no longer
    count as crash boots (all boots are capped at 8).
  - A failed rollback copy is recorded as `rollback_failed`.

### Plugin host
- Runtime plugin enable now uses the boot path (`PluginHost.enable`/`disable`/
  `reload`): placement, stored settings, collision safety, status and the
  worker's `bind_worker`, so re-enabling `work`, `sessions`, `config`, `shell`
  and others no longer crashes. New caps `worker.plugin.reload`,
  `hub.plugin.enable`/`disable`/`reload` (admin).
- Settings marked `apply="reload"` now take effect: a hub plugin reloads when
  one changes, and a worker push that only touches live/reload settings is
  applied in place instead of restarting the worker.
- Capability calls are bounded: `ROOK_CAP_TIMEOUT` (default 300 s, `0` = off),
  per-cap `@capability(timeout=…)`, a cap's own `timeout` argument plus 15 s.
  A timed-out call answers `timeout: …` instead of hanging.
- Every built-in cap (Android's too) declares its risk tier and has a
  docstring; the hand-written tier table shrank to core caps and the floor is
  read from the plugins. Android caps that silently resolved to `exec` now
  carry their real tier (e.g. `ui.text`, `sms.list` read; `device.update` admin).
- The Android app's native plugins load through the plugin host with
  placement (`app == 'android'`) instead of being registered by hand; a cap
  collision no longer crashes the worker. The skill reference lists them.
- `notify.send` reaches every running chat integration, including third-party
  ones. Optional `aiohttp`/PyNaCl imports are lazy, so plugins load and
  `tools/gen_*_reference.py --check` refuse to run with modules missing.
### Plugin kit
- `ROOK_WORKER_HOME` now moves all worker state (identity, enrollment, pushed
  config, audit log, plugin data, chat/inbox, deauth flag, work DB, trust
  anchors), not only the session mirror; the installed bundle stays in
  `~/.rook-band-worker`. Unset, nothing moves.
- New `rook.core.pluginkit` (ships in the worker bundle): `run()` (kills on
  timeout, decodes with `errors="replace"`), `which()`, `is_termux()`/
  `is_windows()`/`is_android()`/`has_display()`, `state_path()`, `fail()` and
  `require_str`/`require_int`/`opt_int`/`require_choice`. Worker plugins use it
  instead of their own copies. New node fact `termux`.
- `cec`, `pikvm`, `agent` (wake), `memory` and `dongle` read their settings
  through the settings view, so values set in the Settings UI are used (env
  vars still win). `cec` and `hermes` always export their plugin and gate in
  `available()`, so their settings show up while they are off.
- Worker caps report failures as the reply's own `ok: false` + `error` instead
  of `ok: true` wrapping `{ok: false}` (file, memory, msg, chat, camera,
  screenshot, battery, wake, deluge, dongle, cec, hermes memory.read,
  sessions.shim.install, worker.config_apply refusals, shell.exec argument
  errors). Every in-tree consumer already handled both shapes.
- `deluge.*` refuses torrent ids that are not hex hashes (or `*`), so an id
  can no longer smuggle a second console command; `deluge.add/pause/resume/
  remove` fail instead of returning `ok: false` with the console output.
- Worker chat client: its watcher thread now stops on every exit path.
  `hermes` logs through `logging` instead of printing.
### Dashboard
- Removed the legacy exec worker (`rook/remote/worker.py`, served as
  `/worker.py`) and the dashboard's `/ws` endpoint it connected to. Every
  installer has deployed `band-worker.pyz` (which joins over `/band`) for a
  long time; `--token` is still accepted and ignored so old units start.
- Removed the MCP port's password-gated, server-rendered `/tokens` page and
  its form routes; the dashboard's Tokens view (and Account -> Band access
  for pairing and PSK rotation) replaced it. `/tokens` on the MCP port now
  redirects to `https://<installer domain>/#tokens`. `ROOK_MCP_AUTH_PASSWORD`
  / `--admin-password` are ignored (a notice is printed when set).
- The dashboard login, first-run setup, Permissions and standalone account
  pages are static files under `rook/web` (`login.html`, `setup.html`,
  `permissions.html`, `account-page.html`) on the shared `theme.css`, filled
  by `rook.remote.web_pages.render`; forms, CSRF fields and redirects are
  unchanged.
- The account JSON APIs (guidance, vault, tokens, knowledge, settings, jobs)
  share one auth gate, `rook.remote.account_api.account_api` (session 401,
  operator 403, JSON object 400, constant-time CSRF 403), and every session
  CSRF compare in the dashboard uses `accounts.csrf_ok`. A POST body that is
  not a JSON object now answers 400 everywhere (some routes said 403).
- `TokenWeb` is a `KnowledgeWeb` proxy like Guidance, Vault, Settings and
  Jobs. A non-operator calling an operator-only `/account/*/api` proxy now
  gets a JSON 403 instead of an HTML one.
- Dashboard scripts share `esc`, `el` and `ago` from `rook/web/manage.js`
  instead of private copies. The classic-script `esc` in `index.html` now
  escapes `'` too. Relative times read the same everywhere ("5 min ago";
  Knowledge and Settings used to say "5m ago").

### Compatibility cleanup
- Cap errors raised through the plugin kit (`fail()`, `CapError`) reach the
  caller as the bare message (`file not found: /x`), not
  `CapError: file not found: /x`, and no longer log a traceback on the
  worker or hub. `bad args:` replies and unexpected exceptions are unchanged.
- `rook/core/compat.py` sets the supported-peer floor: workers from build 142
  (the worker in the 0.4.1 APK, the oldest still deployed), hubs from build
  230. Removed with it: the `rook band` CLI's fallbacks for hubs without
  `/api/band/whoami` and `/api/band/overview`. The hub-side
  `caps.describe(prefix=)` filtering, the v1 deauth wrapper and the classic
  Work view's `read_page` fallback stay, because that APK still needs them.
  Firmware, Android-lagging caps and stored-state migrations are kept.
- `claude-history.send`, `claude-history.resume` and `codex-history.resume`
  say they are deprecated in their own descriptions (use `sessions.send` and
  `work.stream.open(resume=)`). Task hygiene nudges go through
  `sessions.send`.
- The quickstart, test-hub and hub-deploy e2e scripts and `compose.yaml` no
  longer generate or pass `ROOK_MCP_AUTH_PASSWORD`.
- State files are written atomically everywhere: `setup.json` (band PSK),
  the Telegram offset and the home-agent cursor were written in place, and
  `setup.json` was briefly world-readable before its chmod. Modes are kept
  (secrets and worker config 0600).
- Remove next release: the hidden, ignored `rook-remote --token` and
  `rook-mcp --admin-password` / `ROOK_MCP_AUTH_PASSWORD` (and its
  `core.mcp.admin_password` settings entry).

### Review fixes
- Android native caps are back at the tier master gave them (undeclared, so
  `exec`): `sms.list`, `contacts.search`, `calllog.list`, `location.get`,
  `notify.list`/`post`/`dismiss`, `ui.text` and `device.info`,
  `update_status`, `find`, `find_stop`, `wake`, `vibrate`, `torch`,
  `clipboard_get`/`clipboard_set`. Their new `sensitive`/`physical` tags
  stay. The hub, which does not ship the app's plugins, now carries them in
  `authz.CORE` so an Android peer cannot announce them lower. Any of them can
  be lowered later as a deliberate change (plugin, `authz.CORE` and
  `tests/fixtures/master_tier_floor.json` together).
- `tests/test_tier_snapshot.py` pins every cap's effective tier and tags to
  at least master's table (frozen fixture), Android caps included.
- Reloading or re-enabling the hub's knowledge plugin keeps the MCP bridge's
  wiring (attributed caller, handoff store, band list): it is now a plugin
  host bind hook (`PluginHost.add_bind_hook`) that runs on every activation,
  and repoints `rook_knowledge`, the hygiene loop and the Knowledge page API
  at the new instance. Before, a reload left bands collapsed to `default`
  and inline handoffs failing.
- Workers keep running on Python 3.10, as master's did: the per-cap timeout
  (and Codex direct input) use `rook.core.aiocompat.timeout`, a backport of
  `asyncio.timeout`, instead of the 3.11-only API, and a test keeps 3.11-only
  APIs out of the worker bundle. Workers are not refused on 3.10, since
  master's worker never needed 3.11.
- `deluge.pause`/`resume`/`remove`/`files` take a torrent name again (deluge
  matches names as well as hashes), passed to `deluge-console` as one quoted
  argument; only `;`, quotes, backslashes, control characters and a leading
  `-` are refused. `deluge.add` refuses `;` (it split the console command
  even inside the old double quotes) and single-quotes its argument.
- `screenshot.capture_region` accepts negative `x`/`y` again (monitors left
  of or above the primary one); the ImageMagick backend gets a signed crop
  offset.
- Plugin reload: `worker.plugin.reload`/`disable` and `hub.plugin.reload`/
  `disable` are tagged `destructive`. A reload keeps the plugin's caps
  registered until the new instance takes them over, so an announce sent
  mid-reload no longer drops them. A reload that fails leaves dependents with
  `dependency()` = `None` (status `dependency_unloaded`) instead of the
  stopped instance. `selfupdate` no longer adds another OTA binary handler
  on every reload.
- With `ROOK_WORKER_HOME` set, a worker copies state missing there from
  `~/.rook-band-worker` once at start (worker_id, enrollment, deauth flag,
  hold, update state, pushed config, plugin/custom-cap state, anchors, audit,
  chat, work DB, plugin data), keeping modes; older releases kept it there.
  The deauth flag in `~/.rook-band-worker` is honoured even when nothing is
  copied. `ROOK_WORKER_HOME_FRESH=1` starts a separate worker instead.
- Dashboard modules import `manage.js` with their own `?v=` version (and the
  Account/Tokens views and the standalone `/account/bands` page load
  versioned URLs), so a stale edge cache cannot pair a new module with an old
  `manage.js`.
- A database opened with a mode (the vault, enrollment, and every other 0600
  store) gets the same mode on its `-wal`/`-shm`/`-journal` files: existing
  ones are chmodded on open and new ones are created under a matching umask.
- `rook/core/compat.py` documents that the Sessions dashboard needs worker
  build 364+ (`sessions.*`): deployed pyz workers are at 401, and the 0.4.1
  APK's embedded worker (142) gets the read-only `work.sessions` fallback,
  which stays until the peer floor passes 364. No deployed worker sits
  between 143 and 363.
- Atomic state writes fall back to an fsync'd in-place write when the file
  cannot be replaced (`EBUSY`/`EXDEV`), so `setup.json` saves again when
  `ROOK_SETUP_PATH` is a single bind-mounted file. Modes are kept.
- `PIKVM_URL` without a scheme (`kvm.lan`) means `https://kvm.lan`; one that
  is not an http(s) URL or host keeps `pikvm.*` unloaded with a warning.
  Before, it loaded and silently talked to `https://localhost`.

### Plugin management
- Manage > **Plugins**: one grid of every plugin (rows) on the hub and each
  online worker (columns, filterable by band and OS; a per-plugin summary on
  narrow screens). Each cell says on / off / not available and why
  (placement, `available()`, a missing dependency, the platform) or the
  error; click it to switch the plugin live there. A detail panel lists caps
  with their risk and docstring, links the plugin's settings, switches it on
  every eligible worker at once and reloads it. Core plugins (`selfupdate`,
  `config`, `enrollment`, `info`; hub `settings`, `policy`, `info`) stay on.
- Hub plugin switches persist: `hub.plugin.enable/disable` and the page
  store them in `core.plugins.hub` and the hub applies them at start.
  Turning on a plugin that has an `enabled` setting turns that on too; an
  environment variable for it still wins (docs/design/plugins.md §9.2).
- Defaults for new workers per band (`core.plugins.worker_defaults`):
  enrollment hands them to the worker as a plugin profile (a pairing code
  carries one only when the operator attaches it), and the worker applies its
  disables on its first start only (persisted like `worker.plugin.disable`;
  never widens `--enable`; older workers ignore it). Installers pass nothing new.
- Presets, "What do you want Rook to do?" (remote control, agent sessions,
  knowledge & tasks, home & voice agent, phone & chat alerts, hardware), each
  with its risk tiers and high-risk parts called out: on the Plugins page,
  as step 1 before "Get a pairing code" on the first-worker card and the
  Install page, and in the /setup wizard. Boxes show the stored choices;
  saving writes only the groups that changed.
- Marketplace preview: catalog format `rook-plugin-catalog/1`
  (docs/design/plugin-marketplace.md), `core.plugins.catalogs` /
  `ROOK_PLUGIN_CATALOGS`, a Marketplace tab listing entries (a bundled
  example catalog of fictional entries when none is set), and
  `plugin.marketplace.list` / `plugin.marketplace.install` on worker `rook`;
  installing is not implemented yet and says so.

### First-run onboarding
- One public base URL (`rook/remote/public_url.py`) for installers, one-liners,
  the account origin and the update manifest. `--domain`/`ROOK_DOMAIN` may now
  carry a scheme (`http://192.0.2.10:7005`); without one it stays `https`
  except for a local address (non-public IP, `localhost`, dotless or
  `.lan`/`.local`/`.internal` name) on this dashboard's own port, or port 80,
  so a LAN quickstart hub serves `http://` installers and a public hub's
  scripts are unchanged. Workers accept `--enroll http://` only
  for a non-public IP literal (LAN, loopback, tailnet).
- Dashboard and account cookies are set through one helper: `Secure` whenever
  the public base is `https` (as before, even if a proxy omits
  `X-Forwarded-Proto`) or the request came over HTTPS (`X-Forwarded-Proto`,
  `Forwarded`, `CF-Visitor`); only an `http://` hub reached over plain HTTP
  sends them without it. `rook_session` was never `Secure`; it now is on HTTPS
  requests. Fixes the endless redirect (ERR_TOO_MANY_REDIRECTS) and "Failed to
  fetch / Sign in" on Tokens and Account & access over plain HTTP.
- The dashboard-to-account sign-in bridge is loop-proof (a one-minute
  `rook_bridge` marker: a second miss answers 400 with what to fix) and also
  covers `/account/bands/api` and the Settings, Knowledge, Guidance, Vault and
  Jobs APIs. It only bridges GET/HEAD.
- A hub without a worker bundle builds one itself at start, in the background
  (`build_band_worker.py --output-dir`, signed with the hub's update key, the
  manifest `url` on the public base), into `$ROOK_DATA_DIR/worker-bundle`
  (else `~/.local/share/rook/worker-bundle`), and rebuilds it when the worker
  code is newer or the public URL changed. `/band-worker.pyz` and `.json`
  answer 503 "building, retry in a minute" meanwhile. An operator-built bundle
  beside the script still wins and is never rebuilt automatically.
  `/band-worker-enrollment.pyz` serves the same bundle unless an operator drops
  a separate one.
- `scripts/local-hub.sh` finds a `cargo install`ed relay in `~/.cargo/bin`
  (`$CARGO_HOME/bin`) when it is not on PATH yet, passes an explicit
  `http://` dashboard domain, and prints the Install-a-worker URL.
- Running from a git checkout reports `0.dev+<short commit>` (read from `.git`
  once at import, no subprocess) instead of a bare `0.dev`; build stays 0, so
  dev workers still never auto-update. Stamped bundles are unchanged.
- Install a worker: a "Get a pairing code" button mints a code for the
  selected (else primary) band and shows the complete Linux/macOS, Windows
  PowerShell and Android (Termux) commands with the code filled in and a
  self-renewing countdown. The Workers view shows an "Add your first worker"
  card with the same button while only the hub is on the band. Same owner-only,
  CSRF-checked pair action as Account & access (`manage.js` `mintPairing`,
  `pairingPanel`, `pairCommands`; the pairing dialog now uses them too and
  gains the Android line and copy buttons).
- README Quickstart lists the Debian/Ubuntu (and Fedora/Arch) prerequisites and
  the rustup one-liner; "Workers on your LAN" describes the Install-page flow
  over plain HTTP.
- Windows worker one-liner (`iex (irm ".../worker?band=CODE&os=windows")`):
  the script runs inside one script block and never `exit`s under `iex`, so
  the user's window stays open (no more "[process exited with code 1]") and
  its preferences are untouched. From a non-admin prompt it waits for the
  elevated run and reports the outcome there: "Done: <name> is installed and
  joining the band...", or the error, the exit code (`$LASTEXITCODE`) and the
  log path; a declined UAC prompt says so. The elevated run logs to
  `%USERPROFILE%\.rook-band-worker\install.log` and stays open on failure
  ("Press Enter to close"); it closes itself on success.
- Re-running an installer (upgrade, re-pairing to another hub) no longer fails
  on Windows with "band-worker.pyz ... being used by another process": the
  bundle downloads to `band-worker.pyz.download`, then the running worker and
  the `RookBandWorker` task are stopped and the file is moved into place; a
  failure after that restarts the previous worker. The Linux/macOS installer
  downloads beside the live bundle and renames it over (atomic), so a running
  worker never reads a half-written file.
- Both installers announce how long they take and print numbered progress
  (Python, venv, dependencies, bundle, enrollment, service); the bash one ends
  with the same "Done" line or a "FAILED (exit code N)" line.
- When enrollment fails on a pairing-code install (typically a code that
  expired during a slow first install), both installers ask for the fresh
  code the dashboard shows and retry instead of failing.
- The Install page's pairing commands carry a short note: UAC and 2-5 minutes
  on Windows; a per-user service, possible sudo prompt and 1-3 minutes on
  Linux/macOS.

### Added
- Sessions catalog (docs/design/sessions.md, workstream A): worker caps
  `sessions.list`, `sessions.follow`, `sessions.send` and `sessions.stop`
  give one record per agent session (state live/idle/closed, origin, view,
  input, inbox policy) on every OS; live-session detection now works on
  macOS and Windows too; `*-history.resume` opens a Rook terminal where the
  worker has one; the hub serves the merged list at
  `GET /account/work/sessions`; workers report `hb.sessions` counts. Claude
  Code mod 0.3.1 stops a terminal-resumed session by closing its terminal.
- Dashboard workers page: a **3D view** beside list and grid (the hub as a
  rook, each worker a pawn on a polar board; a quiet worker's pawn tips over,
  a heartbeat sends a spark to the hub, a ring marks a worker that hosts
  sites; click a piece for its details, hosted links and capabilities).
  Group headings fold and unfold on a click. An expanded worker lists the
  sites and services it hosts as links. The version spread lists every
  version.
- Worker menu (⋯), the same in every view: **Rename…** (`worker.reconfigure`
  with a new name; the worker saves it and restarts) and deauth / remove ban,
  which moved there from the expanded row. Capabilities show as chips by
  namespace in the list, the grid and the 3D card.
- The hub worker `rook` reports the release it runs (`ROOK_RELEASE`, set by
  `rook hub deploy`) as its version and build, in the worker scheme.
- `serves`: hand-written hosting metadata per worker, kept on the hub. Hub
  plugin `serves` with caps `serves.list` (read), `serves.set` and
  `serves.clear` (write) on worker `rook`; each worker's `{sites, services}`
  (lists of `{name, url, note?}`) appears as the `serves` field of its
  `rook_workers` row. Keyed by worker name; needs no worker update.
- Task and handoff tools sized for grooming a deck. `rook_task` deck rows carry
  `revision`, resolved `dependencies` with an `unblocked` flag and, when a
  claim has gone quiet, a `hygiene` reason; deck takes `data {states,
  done_days, fields, handoffs, outcome}` (`handoffs: true` lists every open
  handoff thread with the open tasks linked to it). New actions `note` (a
  dated remark with optional evidence, no revision needed) and `batch` (up to
  50 writes, one result each). `release data {actor}` frees another actor's
  claim once it has been idle for two hours. A project update with
  `data {cascade: true}` gives `paused` or `archived` to its open tasks (work
  in progress is skipped and listed). `rook_handoff_save` takes `task` (link
  to that task, not the claimed one) and `status="closed"` (close a thread).
- Task state `closed`: a task closed because a person said so, kept apart
  from `done`, which needs evidence of the work. It takes
  `data {closed_by: {who, quote, session}}`: the person, their words, and the
  session they said it in. Rook records them on the task and as a `closed_by`
  link. A closed task does not unblock tasks that depend on it.
- Core specification `docs/spec/core-v1.md` (v1.0, RFC 2119): band transport
  profile, messages, caps and tiers, placement, the hub worker `rook` with
  grants and tickets, chat rooms, plugin contract, compatibility rules.
  `conformance/` has test vectors generated from the reference
  (`generate.py`, checked by the unit suite) and a live harness that runs a
  candidate worker against a throwaway hub. Example ports in TypeScript
  (Node, no runtime dependencies) and Rust under `examples/ports/` pass all
  vectors and the live run.
- Hub chat rooms on the band: caps `chat.read`, `chat.write`, `chat.delete`,
  `chat.presence` on worker `rook` (same store as `rook_chat_*`). Band callers
  are recorded as `band:<identity>`; posting needs `ROOK_HUB_BAND_MAX_RISK=write`.
  `scripts/test-hub.sh start --band-max-risk` sets it for a test hub.
- **Persona** plugin (docs/design/persona.md): persona profiles (name, voice,
  rules, do/don't, formatting, per-harness addenda) stored on the hub with
  scoped assignments (user > agent family > band > default), versions and
  attributed history; edited on Settings > Persona or with `persona.get/list/
  render/history` (read) and `persona.set/assign/delete` (admin) on worker
  `rook`. Delivered in each MCP session's `initialize` instructions (after the
  server guidance, at most 1,200 characters; unchanged when nothing is
  assigned), by the worker cap `persona.apply`, which writes a delimited,
  idempotent marker block into CLAUDE.md / AGENTS.md / SOUL.md without
  touching anything else (dry run, diff, removal), in work launches
  (`--append-system-prompt` / Codex `developer_instructions`), in the skill's
  site notes, and to the voice service as `voice.assistant_name` /
  `voice.owner` through `settings.fetch("voice")`.
- One **Settings** area in the dashboard, rendered from the settings schema:
  hub, each band, each worker (with plugin enable/disable and per-worker
  values), each plugin or service (Voice, Decision engine, Knowledge, Tasks,
  Watchdog, worker plugins) and personal preferences. Every row shows the
  effective value, its source (default / setup.json / hub / band / worker /
  user / env or flag, which locks it), what it hides, how a change applies and
  a restart-required flag; conflicts are listed on the overview; search
  matches keys, labels and variable names. Backed by `settings.db` (beside
  `enrollment.db`, shared by both hub processes) with attributed history, and
  served as caps on worker `rook`: `settings.describe/get/set/reset/history/
  apply_worker`, `settings.fetch` for services with a scoped token
  (`core.settings.service_readers`) and `settings.worker_secret` for workers.
  Secrets are stored in the vault and never returned. Generated reference:
  `docs/operations/settings-reference.md`.
- `setting()` gains `apply`, `bootstrap`, `overridable`, env alias lists (and
  the canonical `ROOK_<NS>_<NAME>`), `flag`, `group`, `order`, `min`/`max`/
  `pattern`, `advanced`, `deprecated`, and `url`/`hostport`/`path` types.
- Worker settings are pushed from the Settings page with the commit-confirmed
  config apply; worker secrets go as `{{secret:…}}` references that the worker
  fetches from the hub at use (memory only, never on its disk). New worker cap
  `worker.settings_report`. Tasks has its own `enabled` setting (`ROOK_TASKS`).
- Telegram and Discord integrations as hub plugins (`telegram`, `discord`,
  both off by default), with no platform library needed. Each one:
  - bridges Rook chat rooms both ways with a `<platform>:<user>` sender,
    mention mapping, rate limits and loop prevention;
  - offers `telegram.send` / `discord.send`, plus `notify.send` for every
    running integration (the watchdog can use it with
    `ROOK_WATCHDOG_VIA_HUB=1`);
  - answers a fixed command set that runs as `integration:<platform>` and
    fails closed on the permission policy, so there is no exec or admin
    unless a rule grants it.

  The bot token lives in the vault and is masked everywhere. See
  `docs/integrations.md`.
- Work worklog view (default; `ROOK_WORK_V2=0` or the **Classic view** button
  restores the old one): rooms per project and host, live sessions as real
  xterm.js terminals, finished ones collapsed, one-click resume of any
  Claude/Codex history. New worker caps `work.stream.*` (PTY
  open/read/write/resize/signal/close/list with long-poll reads and compressed
  framing), `work.sessions` and `work.export` (`rook.transcript/1`). The hub
  fans each terminal out to many viewers with one input holder, a bounded
  replay ring and reconnect replay. Agent launch templates (claude, codex,
  hermes, shell) can get a session-scoped Rook MCP token. See
  `docs/web/worklog.md`.
- One plugin API for the hub and workers (`rook.core`, contract in
  `docs/design/plugins.md`): manifest with `core_api` range and
  `<build>.<adjective>.<noun>` versions, placement over node facts
  (`place("is_hub")`, `has('gpu', vram_gb >= 8)`), cap `risk`/`limit`/`fields`
  enforced by core, settings schema, `cap://` resources, migrations. Existing
  worker plugins load unchanged. Worker announces add optional `facts` and
  `tiers` keys.
- The hub appears on every band as the reserved worker `rook`, serving
  hub-placed plugins: `rook_call(cap="hub.info", worker="rook")`. Band calls
  to it may reach only read caps (`ROOK_HUB_BAND_MAX_RISK`);
  `ROOK_HUB_PLUGINS=0` turns it off. Caps declared `tool=True` get a generated
  MCP tool.
- Knowledge and tasks are hub plugins (`rook/hub/plugins/knowledge`,
  `rook/hub/plugins/tasks.py`): caps `knowledge.read`/`knowledge.write` and
  `task.read`/`task.write` on worker `rook` (band callers reach the read caps),
  a settings schema (`enabled` = `ROOK_KNOWLEDGE`, `db_path`, `semantic`,
  `embedder` = `ROOK_EMBED_URL` or `cap://any/embed.text`, `embed_model`) and
  plugin migrations. `rook_knowledge`, `rook_task`, `rook_project` and
  `rook_concept` keep their names, arguments and replies; an existing
  `knowledge.db` is used in place and upgraded without loss. Plugin guidance
  slots now appear in the guidance store. Core API 1.1 adds `DEPENDS` and
  wires settings before `available()`.
- Agent skill at `skills/rook/` (SKILL.md + install/admin/tools/usage
  references). `tools/gen_skill_reference.py` generates the tool and cap tables
  from the code; a test fails when they are stale. The hub serves it as MCP
  resources (`rook://skill/rook`) and `GET /skill/rook.skill`, with optional
  operator site notes (`ROOK_SKILL_SITE_PAGE` / `ROOK_SKILL_SITE_FILE`).
  `rook skill install [--harness claude|codex]` installs it.
- `scripts/local-hub.sh` runs the relay, dashboard and MCP server locally with
  generated credentials.
- `Dockerfile` and `compose.yaml` for running the hub in containers.
- Console scripts `rook-dashboard`, `rook-mcp` and `rook-worker`, and
  `rook dashboard` / `rook mcp` subcommands.
- `ROOK_DATA_DIR` keeps all hub state in one directory.
- `ROOK_*` environment variables for every hub and worker setting, documented in
  `.env.example`.
- `ROOK_UPDATE_PUBKEY` lets workers trust an operator's own OTA signing key.
- Dashboard `--bind`; it refuses to serve on a non-loopback address without a
  password unless `--insecure-no-auth` is given.
- README quickstart and architecture overview; feature tour moved to
  `docs/FEATURES.md`; `CONTRIBUTING.md`.
- Licensed under the Apache License 2.0, with a `NOTICE` for bundled
  third-party components.

- MCP roster filters: `rook_workers(name=, cap_prefix=, online=, fields=)` and
  `rook_caps(prefix=, worker=)`. `caps.describe` takes `prefix=` on the worker;
  through `rook_call` the hub filters it itself, so it works on older workers.
- `rook_call(text=true)` returns a successful result as plain text
  (`shell.exec`: stdout, then stderr and exit code only when set).
- `rook_knowledge`/`rook_task`/`rook_project`/`rook_concept` search and list
  accept `data.fields` (a list, comma string, or `"all"`).
- `tools/token_budget.py` measures what the MCP costs an agent (tools/list,
  instructions, common replies) against an in-process hub.
- `ROOK_MCP_ENVELOPE=legacy` restores the previous `rook_call` reply shape.
- Permissions (`docs/design/permissions.md`), shipped in **audit mode**: a
  policy engine (per-principal tier defaults, most-specific-wins rules with
  worker/group/fact target selectors, hard invariants) evaluated inside the
  hub's band client for every hub path, plus hub tools as caps on `rook`.
  Nothing is denied until `policy.json` sets `"mode": "enforce"`; would-be
  denials are journaled (`calls` gains `principal`, `decision`, `rule`,
  `policy_rev`, `tier`). Principals come from verified credentials only;
  tokens get a role at mint (agent, operator, readonly, integration, custom).
  `policy.explain/get/set/status` caps on `rook` and a `/permissions`
  dashboard page. Built-in cap tier table; a worker may declare a higher tier,
  never a lower one; unknown caps are exec.
- Signed role grants: the OTA root key signs an `is_hub` grant for a new hub
  operational key (`hub-op-key`, rotated every 30 days). The hub announces
  it with an op-key signature (proof of possession); only its holder resolves
  as `rook`, and anyone else announcing that name is quarantined as
  `rook~<id8>` and journaled as `audit.impostor`.
- Call tickets: targeted hub calls carry a short-lived, op-key-signed ticket
  bound to the worker, message id, cap and exact args. Workers verify them in
  `audit` mode by default (`ROOK_AUTHZ_MODE=off|audit|enforce-admin|
  enforce-exec|enforce-all`), record the result in `audit.jsonl` and announce
  their readiness under `authz`. Build-167 workers ignore the extra keys.

### Changed
- Over MCP, `rook_task`/`rook_knowledge` `get` returns only hand-made links
  plus `auto_links` (a count per kind) and the last 10 events; pass
  `data {links: "all", events: N}` for more. Deck outcomes are cut to 240
  characters unless `data {outcome: "full"}`. A revision conflict now names
  the current revision (`current_revision` in the reply).
- A claim idle for more than two hours no longer collects automatic links.
  Finishing, cancelling or archiving a task closes its handoff threads unless
  another open task uses them, and an inline `data.handoff` continues the
  task's existing thread instead of starting a new one.
- The dashboard's domain, relay address and band label now follow environment
  or flag > Settings page > `setup.json` > default; `setup.json` no longer
  silently overrides the environment, and a conflict is logged and shown.
- `ROOK_BAND_PSK` only seeds an empty enrollment database, for the dashboard
  and the MCP server; the MCP server no longer requires `--psk` and serves the
  bands in the shared enrollment database. The MCP server honours
  `ROOK_CHAT_DB`, and the dashboard follows the MCP's chat database.
- `worker.config_get`, `rook_config_get`, `rook_config_apply` and
  `shell.env.list`/`env.get` mask the band key and pushed env values.
- A plugin enabled at runtime that `--enable` leaves out now survives a
  worker restart; `worker.plugin.list` says why a plugin is not loaded.
- A worker started as `python -m rook.worker` restarts correctly (re-exec with
  `-m`).
- MCP replies are compact. Tool results are JSON without indentation and are
  sent once (no `structuredContent` copy), tool listings drop output schemas
  and pydantic schema noise, and the default tool descriptions and server
  instructions are shorter: `tools/list` went from about 32,400 to under
  12,000 characters for the same 29 tools. Details stay in cap tips and
  error messages.
- `rook_call` reply shape for MCP clients: `{ok, id, from, result|error}` where
  `id` is the journal id (`_journal_id` is gone), `from` is the worker's name
  instead of its hex id, and a `shell.exec` result drops empty
  `stdout`/`stderr` and the `ok` implied by `code` (`{"code":0}` for a silent
  success). `_task` and `_unread_chat` appear only when new or changed for the
  MCP session; the per-reply `_hint` line is gone (`_tips` still shows once
  per session, `hint=true` re-shows it). `ok`, `result` and `error` keep their
  meaning, so clients reading those are unaffected; the band wire protocol and
  the dashboard `/api/band/*` API are unchanged.
- `rook_workers` returns name, description, build, hb and last_seen_age_secs
  per worker by default (empty values omitted; worker_id added when a name is
  shared); `fields="all"` gives the previous rows. `rook_caps` returns
  `{workers: N, caps: {cap: "*" | {all_but: [...]} | [names]}}`.
- `caps.describe` through `rook_call` returns `{cap: "(args) — doc"}`.
- Knowledge search over MCP defaults to 5 results with 240-character excerpts
  and a small field set; list defaults to 20. The operator's Knowledge page is
  unchanged.
- Default operator tool tips (guidance `tool:*`) are empty; their content is in
  the tool descriptions. The slots remain editable.
- Removed the pre-band personal agent (`rook agent`, `rook hub`, `rook discord`,
  `rook sync`, `rook extract`, the `rook/core`, `tools`, `memory`, `modules`,
  `net`, `interfaces`, `voice`, `tasks` packages, `config.yaml` and the
  `[legacy]` extra). Its dashboard routes (`/api/workers`, `/api/facts`, `/ui/`
  and others) had been failing on every hub. It remains in git history.
- The repository no longer ships the maintainer's update signing key. Each hub
  creates its own on first start and the worker bundles it builds trust only
  that key. Workers run from source trust no update unless `ROOK_UPDATE_PUBKEY`
  is set.
- `telesthete` is now a declared dependency (installed from GitHub).
- The pre-band personal agent's dependencies (OpenAI, Anthropic, Discord, Kuzu, …)
  are no longer installed; screen capture moved to the `[desktop]` extra.
- Defaults point at localhost instead of the maintainer's hosts: worker `--hub`,
  terminal UI `--url` and user, voice service MCP URL.
- Maintainer deployment records, handoffs, incident notes and the roadmap are
  no longer kept in this repository.

### Fixed
- The deck showed a retracted handoff link as a task's latest handoff.
- The dashboard listed the hub's own worker `rook` under whichever band it
  had last announced on, offered to move it to another band and then asked
  for a worker update. The hub is now shown as on all bands, grouped as
  **HUB** when grouping by operating system, has no move action, and is no
  longer part of a band's roster for worker moves or key migration. Workers
  that announce platform facts are grouped by them without a device probe.
- `worker.deauth` accepted any validly signed OTA manifest as an order and
  skipped the target/age checks when fields were missing. It now requires a
  deauth v2 order with its own signature domain and mandatory `worker_id` and
  `issued_at`; the hub sends one nested in a legacy body so older workers
  still park.
- `worker.update(url=...)` installed a bundle with no signature check. It now
  needs a signed OTA `manifest` (sha256 and `--selftest` checked, no
  downgrades).
- `worker.reconfigure`, `worker.update` and `worker.config_apply` could
  repoint a worker at another hub or PSK without a signed order. Hub/PSK
  changes now need a verified hub ticket (`ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT=1`
  on the worker is the local escape hatch).
- The first-run `/setup` page required no login: anyone who could reach an
  unconfigured hub could set its band key, even with a dashboard password.
  With a password, `/setup` now needs the login first. A password-less hub
  (loopback only) keeps an open wizard, protected by a form token.
- Dashboard login cookies were a fixed hash of the username and password, so
  they never expired and could not be revoked. Each login now gets a random
  30-day session that ends on logout or when the password changes. Existing
  logins must sign in once more.
- The MCP server failed to start without `--public-url`.
- The MCP server rejected loopback Host headers on any port other than 8765.
- A hub configured only with `ROOK_BAND_PSK` / `--psk` stayed locked behind the
  `/setup` wizard.
- The relay's default 15s peer TTL evicted Rook peers between their 20s
  keepalives; the quickstart and compose file set it to 60s.
- Dashboard `/api/band/call` now accepts a worker name as well as its id.
- A worker now warns when saved enrollment or pushed config overrides the
  `--hub`/`--psk` it was started with.
- The worker bundle builder can use a pip-installed `telesthete`.

### Data layer and session history
- `rook tmux` is removed: it ran a second Claude Code session manager (its
  own `~/.rook/sessions.db`) beside Rook terminals. It now prints where its
  jobs went: the Sessions page, `rook band`, and `sessions.shim.install`.
- `rook sessions` reads transcripts with the worker's `claude-history`
  parsers, so titles, tool calls and counts match the dashboard.
- The dashboard (Sessions page and classic Work view) uses `sessions.follow`,
  `sessions.send` and `sessions.list` only; a worker that has just the older
  `<agent>-history.*` caps is asked to update instead of being driven through
  them. Classic "Resume on host" opens a Rook terminal (`work.stream.open`).
  `claude-history.send`/`resume` and `codex-history.resume` stay on workers
  and are documented as deprecated for direct use.
- `codex-history.resume` finds the real `codex` binary (never the session
  shim) and the Windows npm `codex.cmd`, like Claude's lookup.
- Internal: `rook.core.db` (SQLite open/transaction helpers, additive column
  backfill, atomic file writes) used by the hub and worker stores; one shared
  work-session store and command path for hub and worker; one manifest
  canonical form and file digest (`rook.worker._update_verify`) for worker
  bundles and hub releases; `rook.worker.agents` and `rook.worker.ring`.
  Database schemas, paths and file modes are unchanged.

## Development history (June to September 2026)

Unversioned work on `master` before public preparation, in broad strokes:

- **Agent workspace (September):** shared knowledge wiki with human
  verify/dispute and review mode; concepts, projects and tasks with claims,
  evidence and handoffs; a secret vault with `{{secret:name}}` placeholders;
  operator-editable agent instructions; per-token attribution of every MCP call;
  MCP session limits and a Telegram watchdog.
- **Accounts and enrollment (September):** Google and local accounts, pairing
  codes, certificate-backed worker enrollment, five-word band keys, band
  management with verified worker moves and PSK migration.
- **Work sessions (September):** Claude Code and Codex session history, review and
  resume across workers from the dashboard.
- **Android (August and September):** native worker app with screen capture,
  accessibility-based input, SMS, notifications, location and device controls;
  voice assistant with wake word; verified APK self-update.
- **Band services (July and August):** signed OTA self-update with in-band push,
  signed deauth and ban, console rooms, chat rooms with presence and wake,
  `rook band` terminal UI, Windows workers, custom command capabilities.
- **Hardware:** ESP32-S3 USB HID/serial dongle firmware, PiKVM and HDMI-CEC plugins.
- **Security:** fixed AEAD nonce reuse across peers (CSPRNG-seeded sequence
  numbers); the first public import was sanitized of credentials.
