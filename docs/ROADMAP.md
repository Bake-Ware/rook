# Rook roadmap

_Last audited 2026-09-02 against master `4adc4c1` and the live band (24 workers)._

This file is the single list of what exists, what is missing, and what is next.
Update it when a feature ships or is deliberately dropped. If something feels
"lost", check the inventory below before rebuilding it.

## 0. Why features keep going missing

The audit of the three reported regressions found that **none of them were
deleted from git**. What actually happens, repeatedly:

| Pattern | Evidence |
|---|---|
| Work lands on a deployed box as uncommitted edits and never reaches master | bakenetca ran a diverged clone for 2 months (resolved 2026-08-20); rook-remote's service clone had hand-applied edits; the multi-band dashboard was reconciled from uncommitted edits (`aa7bae5`) |
| History was squashed | master starts at `89f790c` "Initial public release (sanitized)" (2026-06-26). Everything before that lives only in `~/rook-legacy-2026-06.bundle` |
| A feature exists on one surface but not the one you reach for | Dongle serial console existed only on the dongle's local HTTP/WS + USB CDC; from the band it "always looked missing" (`2b97388` commit message says exactly this) |
| Docs describe a design, not what shipped | `docs/DESIGN-band-services.md` marks a full settings page as shipped; only a 4-field `/setup` form exists |
| Nothing runs the install paths | 5 test files (950 LOC) against 117 modules (26.9k LOC); no CI; no Makefile; the `rook` command is three different programs depending on how it was installed |
| Devices drop off silently | The dongle fell off the band 2026-08-29 18:48 UTC and nothing noticed until this audit |
| Two products share one tree | `pyproject.toml` and `rook --help` still describe the pre-band "knowledge graph / voice agent" Rook; about 15 of those modules no longer import (`rook/core`, `rook/tools`, `rook/memory`, `rook/net/hub.py`, `rook/mcp_server.py`, ...). `rook/tasks` and `rook/voice` are empty packages |
| Build inputs are staged by hand | The APK bundles a git-ignored copy of `rook/worker` made by `android/stage_worker.py`; the copy on cachyrig is currently behind master (`proc.py` lacks the Windows hard-kill fix, `claude_history.py` lacks session resume). Any APK built without re-staging ships old code |

**Guardrails (do these first, they are cheap and stop the bleeding):**

- [ ] **Feature inventory** = section 1 of this file. Keep it current.
- [ ] **CI**: GitHub Actions running `pytest` plus an import smoke test of every
      `rook/**` module and every `python -m rook <cmd> --help`. Today the suite
      only runs as `/home/bake/telesthete/.venv/bin/pytest -q` (51 pass) because
      `tests/conftest.py` needs the telesthete sibling checkout; write that down
      in README and add a `dev` extra with pytest.
- [ ] **`stage_worker.py` runs inside the gradle build** (or CI fails if the staged
      copy differs from `rook/worker`), so the APK can never ship stale worker code.
- [ ] **`rook doctor`**: one command that prints which surfaces are installed and
      reachable (TUI config, worker service, hub, dongle mDNS, band roster).
- [ ] **Deploy only from git**: `git status --porcelain` must be empty on
      `/opt/rook-remote/rook-src` before `build_band_worker.py` runs; refuse otherwise.
- [ ] **Roster watchdog**: alert (ntfy worker is on the band) when a named worker
      is absent > 10 min. The dongle and the two APK phones are the first customers.
- [ ] **README = reality**: every subcommand, cap namespace, and HTTP route gets a
      one-line entry, or gets removed.

## 1. Feature inventory (what exists today)

Legend: ✅ shipped and verified live · ⚠️ exists but partial/unverified · ❌ missing

### Site (rook.bakeforge.com, `rook/remote/bootstrap.py`)
- ✅ Installer endpoints `/worker`, `/install`, `/rook`, `/hub`, `/apk`, OS-aware (Linux, Termux, Windows)
- ✅ Signed-manifest OTA push loop (fleet on build 111)
- ✅ Dashboard: workers, cap forms from `caps.describe`, chat, sessions, install page, multi-band selector, ban/unban, battery pills
- ⚠️ **First-run setup wizard** `/setup`: exists, gates unconfigured hubs, 4 fields (band name, hub, domain, PSK). No settings page, no link from the dashboard, no tests, cannot edit anything beyond those 4 fields. Design doc promises much more.
- ❌ Console rooms UI (MCP-only today)
- ❌ Install-URL gate (`/worker` hands the PSK to anyone)

### Band MCP (`rook/band_mcp`)
- ✅ 23 MCP tools: `rook_workers/caps/call`, journal, handoffs, chat v2 + wake + presence, config OTA (`rook_config_*`), console rooms (`rook_console_*`), bearer tokens + claude.ai OAuth shim. README documents 3 of them.
- ✅ Federation code deployed to hub, inert (no second hub); lives in the telesthete repo, no pointer from here
- ⚠️ Design-doc drift: journal cap is a global 20k-row cap, not per-worker-by-size; "one threads store" is four sqlite files plus the vault; no config-epoch column on the dashboard; no curator/review queue; no `memory.ask`; no ACLs
- ❌ Tests: none for chat, journal, handoffs, memory vault, config OTA, tokens

### Worker (`rook/worker`, pyz build 111)
- ✅ shell/file/info/msg/chat/log/proc/config/selfupdate/deauth/customcap/plugin admin; hid+screenshot on desktops; hermes, deluge, cec, camera, claude-history, memory vault where gated
- ⚠️ `proc.*` on Windows: fixed by inspection only, never run on a Windows worker (both FLOPHOUSE VMs offline)
- ⚠️ In-band OTA over telesthete Drop: code present, never smoke-tested, no disarm timeout
- ⚠️ Android APK workers (Bakephone, XaviersTablet): build 0, outside OTA, no `proc.*`; need rebuild+reinstall per change

### `rook` CLI (`rook/__main__.py`, `rook/cli/`)
- ✅ `rook band` TUI (also what `curl .../install | bash -s -- cli` installs as the whole `rook` binary)
- ✅ `rook sessions|history`, `rook tmux`, `rook sync`, `rook extract` work via `python -m rook`
- ❌ `rook hub` (needs `telesthete`, not in pyproject), `rook agent` (needs `python-dotenv`, not in pyproject), `rook discord` (needs `discord`): all die with a raw `ModuleNotFoundError`
- ❌ Not installed on cachyrig at all (`which rook` empty; `pip show rook` empty)
- ⚠️ `rook/cli/graph.py` is an orphan library module with no subcommand; imports `kuzu`
- ⚠️ README documents only `rook band`; `--help` advertises 7 more
- ⚠️ Three unrelated programs are called `rook`: the pyproject console script, the installed band TUI, and the legacy `rook/remote/worker.py` bash shim

### Dongle (LilyGo T-Dongle-S3, `firmware/`, v0.6.7)
- ✅ USB HID keyboard/consumer, BLE HID, MSC, LCD, APSTA Wi-Fi + mDNS `rookdongle.local`, telesthete UDP band worker (`kvm.*`, `info.*`, `serial.*`)
- ✅ **Local serial terminal**: `rook>` REPL on USB CDC at 115200 (`serial_cli.cpp`, wired in `main.cpp:85,93`): `help status ip wifi{list,add,rm,forget,scan,reconnect} hub band worker admin reboot factory`
- ✅ **Band serial passthrough**: `serial.write/read/status` (600 B per poll, base64)
- ❌ **Off the band since 2026-08-29 18:48 UTC** (last journaled call was `kvm.type` from a Claude web session). Hub still listens on `0.0.0.0:7474` UDP, so this is device-side (power, Wi-Fi, or crash loop). Not reachable via mDNS from cachyrig either.
- ❌ No interactive terminal front-end anywhere: dashboard shell button is gated on `shell.exec`, `rook_console_open` refuses workers without `proc.start`, TUI has no serial mode
- ❌ **Bug**: `addToSerialBuf()` feeds every byte to both the passthrough ring and the CLI ring, so a getty on the host side gets its output parsed as dongle commands and `unknown: ...` echoed back into the session
- ❌ Unsigned OTA (`/ota`, HTTP Basic, password reused)
- ⚠️ Docs stale: DEVLOG module map omits `serial_cli`, `ble_hid`, `telesthete`, `settings`; `serial_cli.h` says USB-Serial-JTAG but the code targets CDC

### Security (from the 2026-07 audit)
- ✅ F1 nonce reuse (worker side), F3 deauth/ban
- ❌ F1 residual on firmware sender, F2 weak shared PSK, F4 unsigned dongle OTA, F5 reused passwords in unit files, install-URL gate

## 2. Now: the three reported regressions

### 2a. Dongle serial terminal
1. **Get the dongle back on the band.** Physically check it; if it is up on
   Wi-Fi, open `rookdongle.local` or the CDC console and run `status` and `hub`.
   If it is crash-looping on v0.6.7, reflash and note the reason in DEVLOG.
2. **Fix the CLI/passthrough conflict** in `serial_buf.cpp`: a mode switch
   (`serial mode cli|pass`, persisted in NVS) or an escape prefix so the
   `rook>` CLI only parses bytes typed on the CDC port, never host-side output.
   Default to passthrough when a band peer has opened the stream.
3. **Give the passthrough a real front-end** so it stops "looking missing":
   - Dashboard: unlock the terminal button for workers advertising `serial.write`
     (poll `serial.read` on a timer; same xterm pane as console rooms will use).
   - MCP: let `rook_console_open` accept a serial-backed session (pump `serial.read`
     instead of `proc.read`; pull-not-push already matches the design).
   - TUI: `s` key on a worker with `serial.*`.
4. Fix the two doc lies (DEVLOG module map, `serial_cli.h` comment) and add a
   "Serial console" section to README with the `picocom /dev/ttyACM0 115200` line.

### 2b. Web setup wizard
1. Add a **Settings** view to `rook/web/index.html` that is the wizard's second
   life: same fields plus web user/password, update URL, retention caps, and the
   worker-config OTA form (`rook_config_apply` already exists server-side).
   Link it from the nav. `/setup` becomes "Settings, first-run mode".
2. Move secrets out of the systemd `ExecStart` (F5) into the same `data/setup.json`
   so the wizard can rotate them.
3. Add `tests/test_setup_wizard.py`: unconfigured hub redirects to `/setup`,
   submit persists, dashboard loads afterwards, bands list survives a re-save.
4. Reconcile `docs/DESIGN-band-services.md` §1 with what shipped, or ship it.

### 2c. `rook` CLI
0. **Decide the legacy stack first**, because it is what makes `rook --help` lie:
   either move `rook/{core,tools,memory,modules,tasks,voice,interfaces,net}`,
   `rook/scheduler.py`, `rook/mcp_server.py` and `config.yaml` to `attic/` with a
   README, or fund them with deps and tests. Recommendation: attic. `sessions`,
   `tmux`, `sync`, `extract` stay because they work and are used.
1. **One `rook`.** Make the installed `rook` the full `rook/__main__.py`
   dispatcher (zipapp of `rook.cli` + `rook.__main__`, same as the worker pyz),
   not a copy of `band_tui.py`. Keep bare `rook` = `rook band` for muscle memory.
2. Declare deps honestly in `pyproject.toml` (`telesthete`, `python-dotenv`),
   make `discord`/`kuzu` optional extras, and catch `ModuleNotFoundError` in the
   dispatcher so a missing extra prints "install rook[discord]" instead of a trace.
3. Decide `rook/cli/graph.py`: give it a subcommand or move it to `rook/memory`.
4. Fix `rook/remote/worker.py` Windows install/uninstall bodies (they run the
   Linux systemd teardown) or delete that legacy installer entirely.
5. Termux branch for `install_cli` (`~/.local/bin` is not on Termux PATH).
6. Add `tests/test_cli_dispatch.py` that runs every `--help`, and put it in CI.
7. Install it on cachyrig and verify `rook`, `rook band`, `rook sessions` from a fresh shell.

## 3. Next (after section 2)

- [ ] **README refresh**: repo layout (missing `android/ server/ pikvm/ tests/` and the
      legacy tree), the 20 undocumented MCP tools, the `battery/cec/log/memory/proc/agent.wake/worker.config_*`
      namespaces, console rooms, Android voice client + phone caps. `android/README.md`
      parity table lists 5 namespaces; the APK ships 16.
- [ ] **Design doc status lines**: correct §1 (partial), §2 (ring semantics), §7 (four stores) in
      `docs/DESIGN-band-services.md` so the durable record stops overstating.
- [ ] **Console rooms UI** in the dashboard (rooms list, search, live pane) and a
      stdin write-lock so two agents cannot interleave into one session.
- [ ] **Verify Windows `proc.*`** on WIN10/WIN11-FLOPHOUSE (bring the VMs up first).
- [ ] **Android APK**: rebuild with build 111 worker (gains `proc.*`), reinstall on
      both devices, and design a way for APK workers to at least *report* their
      staged build instead of `0`.
- [ ] **In-band OTA (Drop)**: smoke-test once on one worker, add the disarm timeout.
- [ ] **Signed dongle OTA** (F4) using the existing ed25519 manifest; include
      `ROOK_FW_VERSION` in the manifest so the push loop can show "firmware behind".
- [ ] **Firmware F1 residual**: randomize the sequence start in `telesthete.cpp`.
- [ ] **Install-URL gate**: strong install token minted at `/tokens`; stop serving
      the PSK to anonymous `GET /worker`.

## 4. Later

- [ ] PSK rotation via `worker.reconfigure`, then per-worker identity (F2).
- [ ] Rotate reused passwords out of unit files (F5).
- [ ] Federation: stand up a second hub and turn `HUB_FED_*` on.
- [ ] Dongle BT-tether uplink (DEVLOG TODO).
- [ ] Heartbeat metrics beyond battery (temp, load, disk) via `Plugin.heartbeat()`.
- [ ] Decide the fate of the pre-band "Rook 2.0" agent surfaces still in the tree
      (`rook agent`, `rook discord`, `rook/voice`, `rook/memory` graph): support them
      with deps and tests, or move them to an `attic/` with a note.

## 5. Where old code lives

- `~/rook-legacy-2026-06.bundle` (also on bakenetca): full pre-squash history back to
  2026-03-15. Contains the only copy of the wire fragmentation PoC (`fb196fd`) and the
  original firmware serial-CLI commit (`b27bfac`, 2026-05-18). Everything else in it
  was superseded on master.
- `git tag known-good-build76` and `~/restore-points/rollback.sh` on bakenetca.
