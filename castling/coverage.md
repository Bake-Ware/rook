# Castling coverage: can the v1 manifest say what today's plugins need?

**Update, manifest 1.1 (wave 2, 2026-10-30):** most gaps below are closed by additive 1.1 keys
(knowledge page castling-manifest-lifecycle lists them). Each gap now says **Closed** or **Open**.

Converter agent, 2026-10-30. Baseline: this branch (cut from origin/castling = master d15e79bf),
457 caps. Decisions: Rook knowledge castling-caps (`castling/tools/cap-decisions.json`, copied from
cachyrig `~/rook-backups/castling-caps/decisions.json`), with Bake's C3 amendment applied (decide keeps
`decide.drive`; the decisions file still bins it).

- `castling/tools/convert.py` reads every `@capability` (worker, hub, phone) by AST, applies the
  decisions and writes `castling/manifests/<plugin>/plugin.yaml`. Rerun it after any change; the test
  `tests/test_castling_convert.py` fails when the committed manifests are stale.
- Hand-written config actions live in `castling/tools/overlays/<plugin>.yaml` and are merged last.
- **Validation:** every draft is read back from its YAML and checked by `rook.kit.manifest` (JSON
  Schema plus semantic checks); errors fail the converter and its test. All 63 pass with no errors.
  Warnings left: 88 `unknown-contract` (no contract schemas yet, B4), 12 `unknown-key` (record
  `aliases`, C5), 11 `effect-risk-mismatch` (read caps tagged `physical` today, E). Not checked: file
  existence (`scripts/*.py` don't exist yet: every `run:` points at today's function).

## How the manifests come out

| Mapping | Count | What it means |
|---|---|---|
| config (`action:`) | 21 caps in 7 plugins | kvm 8, torrent 7, cec 2, battery, notification-desktop, screen-desktop, camera.capture |
| run (`run:`) | 187 caps | `scripts/<module>.py:<Class>.<method>`, today's function (31 split caps `bind` the old action argument) |
| builtin | 37 caps in 12 core namespaces | `builtin: true`, no action/run: caps, hub, help, llm, notify, page, plugin, setting, stream, update, worker, approval |
| records | 31 old caps -> 13 record types (search stays `run:`, C4) | change, job, trust, user, memory, persona, agent, attention, knowledge, task, project, concept, claudecode.rule |
| collapses into streams | 59 caps | binned with `stream.*` as replacement: claude_history 12, proc 6, huddle 5, chat/sessions/terminals/work 4 each, rooms/roles/hermes 3, ... |
| binned otherwise | 151 caps | left out, as decided |

One manifest per 0.5 plugin; core, agents and workbench caps get one manifest per namespace (one
plugin, one namespace). Caps in one of the 12 shared contracts (call C1) keep the contract name as
their key and are listed under `provides`.

### The easy plugins

| Plugin (today) | 0.5 | Config | Still `run:` and why |
|---|---|---|---|
| pikvm | kvm (appliance) | screen.capture, hid.type, hid.move, hid.click, power.on/off/reset/status | hid.key (A4, A5); api_get/api_post (raw path into a URL and a query dict: A3, A12) |
| deluge | torrent | list, get, add, pause, resume, remove, delete | status (two commands: A5) |
| msg | notification-desktop | notification.post (Linux, Windows, macOS) | - (inbox read/clear binned) |
| battery | battery | status (Linux upower, Windows CIM, macOS pmset, Termux) | - (1.1: per-OS parse; heartbeat via `hooks.status: {call}`) |
| cec | cec (appliance) | send, status | raw line can't share `send` (A6) |
| screenshot | screen-desktop | screen.capture (Linux try-chain, macOS, Windows) | preview dropped (A11); Windows ignores region |
| camera | camera | capture (Linux, Termux) | list; Windows (ffmpeg dshow needs a device name) |
| info | - | - | everything is core (`worker.ping`) or binned: nothing left for a plugin |

## The real ports (wave 3)

Plugin-ports agent, 2026-10-30. Five plugins are real 0.5 folders now (`rook/plugins/<name>/`:
`plugin.yaml`, `docs/manual.md`, scripts only for kvm), tested through the worker host
(`tests/plugins/`, fake binaries on PATH, a local HTTPS fake kvmd). Their drafts in
`castling/manifests/` are superseded; the overlays stay as the converter's record.

| 0.5 plugin | Config | `run:` | What changed from the draft |
|---|---|---|---|
| torrent | all 9 caps | - | `status` is `deluge-console status` (one command; format from Deluge's source); new `torrent.version` is the `check` (needs no daemon); fail_if checked against real deluge-console 2.2 output (`Could not connect to daemon` / `Password does not match`, exit 0) |
| kvm | screen.capture, hid.type/key/move, power.* | hid.click, api_get, api_post | hid.key is config via kvmd `send_shortcut` (`join` + `map`); hid.type `enter` and power.off `force` by `map`; hid.click takes the contract's pixel x/y (reads `/api/streamer`, moves, clicks); read caps lose `physical`; no `check` (N4) |
| battery | status (Linux upower + sysfs fallback, Windows, macOS, Termux) | - | `requires.hardware: [battery]`; no battery is an error, not `null` |
| notification-desktop | notification.post (Linux, Windows, macOS) | - | text/title bounded; no `check` (N4) |
| screen-desktop | screen.capture (Linux try-chain, macOS, Windows) | - | `result: {type: file}` (contract); `check` takes one screenshot |
| info | - | - | nothing to port, by decision: `worker.ping` and `worker.status` are core (N7) |

kvmd behaviour the kvm port relies on, read from kvmd's source (master): `send_shortcut?keys=`
splits on commas and drops empty items (so no mods sends `,KeyC`), presses in order and releases in
reverse; `send_mouse_button` without `state` presses and releases; `/api/hid/print` types `\n` as
Enter; key names are KeyboardEvent codes. So A5 no longer touches hid.key, only hid.click at a pixel.

Contract fit (B4): with core.yaml's contracts every port validates with no errors or warnings.
What it took: kvm hid.click gained `x`/`y`, hid.type `enter`, hid.key a typed `mods` list,
power.off `force` (replacing the draft's `mode` enum, which also had YAML 1.1's `off -> false`
bug), screen.capture `result: {type: file}` (both screen plugins); kvm stops claiming
`hid.move`/`power.on`/`power.reset`/`power.status` in `provides` (no such contracts).

### What the ports found

- **N1. `returns` can't pick a key that isn't a dotted identifier.** `parse: kv` on
  `deluge-console status` gives keys like `Total upload`; the schema's `dotted_path` refuses them, so
  torrent.status parses with a regex instead. Fix if it recurs: a rename map whose values may be any
  key of the parsed map (no dots = one key).
- **N2. No mapping on output.** `map` translates the caller's args only; torrent.list returns
  deluge's state letters (S, D, ...) and the doc lists them. Fix if wanted: `map` on a `result` field.
- **N3. The worker host can't serve appliance caps.** `Host.handle`/`Host.call` ignore the request's
  `appliance`, nothing builds `appliance_values(instance, settings)` for an action, and `run:` code
  has no `ctx.appliance`. Spec 5.7 says "instance settings and credentials travel with the call",
  but the request table (6.1) carries only the instance name: where the values travel is unspecified.
  kvm's script reads `ctx.appliance` and its tests stand in for the host
  (`TODO(castling:worker-host)`). Fix: spec a request field (or a delegate-side instance store) and
  pass `appliance=` in `Host._exec` and `ctx.appliance` for scripts.
- **N4. `check` caps are called with `{}` and no appliance, and a failing check rolls back.** An
  appliance plugin can't have a check at install (kvm has none); a cap with a required arg can't be
  one (notification-desktop has none); and a check must not depend on outside state (torrent.status
  fails while the daemon is down, so it would roll back a good install: hence torrent.version).
  Fix: appliance checks run per instance at onboarding; maybe `check: [{cap, args}]`.
- **N5. `requires.hardware` has no vocabulary.** battery needs `battery`, the desktop plugins
  `display`; nothing defines those names or says core reports them in the worker's facts, and until
  it does these plugins never load. Fix: core.yaml (or the spec's announce facts) lists the hardware
  values core detects: `display`, `battery`, `pty`, ...
- **N6. `requires.bins` isn't per OS.** notification-desktop can't say "notify-send on Linux", so a
  missing tool is a call-time error (`not installed on this worker: notify-send`), not an
  unavailable cap. Minor.
- **N7. core.yaml declares no caps yet** (contracts, events and presets only), so `worker.ping` and
  `worker.status`, where info's caps went, aren't declared or implemented anywhere on castling.
- Not a gap, a known limit kept from 0.4: on KDE/GNOME Wayland grim fails and scrot may exit 0 with
  a black frame through XWayland; spectacle and gnome-screenshot come after scrot in the try-chain.

## The real ports, batch 2 (wave 4)

Plugin-ports agent, 2026-10-30 (PR on branch c4/ports2). Eight more plugins are real 0.5 folders,
tested through the worker host or, for appliance caps, through the bound action/script with the
instance's values (N3 still open): fake binaries, a fake Pico-CEC TCP bridge, a fake SSAP websocket
TV, a fake dongle on a PTY pair, real `sh` and real PTYs for shell. Every manifest validates against
core's contracts with no issues; their drafts in `castling/manifests/` are superseded. A test now fails any `scripts/*.py` that starts a process other than
through `rook.core2.worker.security.launch` (spec 7.1).

| 0.5 plugin | Config | `run:` | What changed from the draft |
|---|---|---|---|
| shell | - | exec, which, env, open | `shell.open` is new: the producer of `terminal` (PTY) and `process` (pipes) streams, so **proc merges here** (castling-streams); output is `bytes` events, input events `text`/`signal`/`resize`, the stream closes with `{exit}`; the PTY is the child's controlling terminal (Ctrl-C works). exec: errors raise, a timeout kills the process group; `elevation: may` but never re-run on its own (a half-run command): a denied-looking failure answers `denied: true`. `env` absorbs `env.list` (`prefix`) and masks credential-like names (0.4's wconfig masking has no 0.5 source). 0.4's Windows shell model (PowerShell default, Git sh, PATHEXT/npm/python-stub resolution) is kept; `cmd.exe` goes through argv (a line with `"` in it is better sent to PowerShell). No `check` (every cap needs an arg, N4). |
| file | - | read, write, list, search, stat | Errors raise and name the path; `file.exists` -> `file.stat` (+ mode, symlinks as themselves); search walks lazily and stops at `limit` or 50 s (a sync function's thread can't be cancelled); arg names `hidden`/`limit`; never elevates (in-process, no launch: elevation is shell's) |
| cec | send, status | - | appliance (instance `port`); `fail_if` is a positive match (`OK` / `pong`), so a silent bridge fails too; `cec.raw` dropped by decision (A6); no `check` (N4); lowercase hex only, as 0.4 sent it. Unverified on the firmware: with no operands the line ends `send 0 36 \n` (a trailing space) |
| camera | capture (fswebcam -> ffmpeg; Termux; Windows ffmpeg dshow with `device`) | list (sysfs names, termux-camera-info, ffmpeg -list_devices) | `personal` effect on capture; `quality` for fswebcam; ImageMagick downscale dropped (A11); macOS not supported (as 0.4); no `check` |
| adb (appliance) | - | all 9 (scan, shell.exec, battery.status, screen.capture, ui.screen, hid.type/key/click/drag) | phones are instances (`discover: adb`, instance `serial`, blank = instance name); `adb.devices`/`path` settings gone (`requires.bins: [adb]`); contract shapes (hid.key `key`+`mods`, hid.drag `duration`, hid.click refuses no x/y or a non-left button, ui.screen `{app, items}` + `limit`, screen.capture `result: file`, JPEG through ImageMagick when present); `shell.exec`/`battery.status` have no contract, so no `provides`; a per-device lock keeps two callers' taps apart; no heartbeat poll, no `check` |
| tv (appliance) | - | all 19 | `webos.tvs` setting gone: TVs are instances (SSDP), instance `client_key` (vault) and `mac`; `appliance.pair: tv.pair` (`admin`, `persists`) returns `{paired, settings: {client_key}}` for onboarding (N10); one SSAP connection per instance, closed by `hooks.stop`; no background loop (subscriptions, heartbeat and the `tv.sleep` timer gone: schedule `tv.power.off` with a job); contract shapes (hid.key `key`+`mods`, hid.type `enter`, notification.post `text`/`title`, sound.set absorbs `tv.mute`, media.seek `by` sign only, power.off `force` accepted and ignored); `screen.on`/`screen.off` replace `screen {on}`; `requires.python: [websockets>=12]` (0.4 used it too) |
| hid-desktop | hid.type, hid.move (Linux try-chain, Windows PowerShell) | hid.key, hid.click, hid.drag, hid.status | typed text goes on stdin, never argv; ydotool 1.x key codes; Termux `input` and evdev backends dropped; `platforms: [linux, windows]`, `requires.hardware: [display]`; `check: [hid.status]` |
| dongle | - | status, display, hid.key, keycodes, hid.move, hid.click, hid.scroll, hid.release | stdlib serial (termios + flock): pyserial gone, `platforms: [linux, macos]`; every cap `exclusive: dongle`, `requires.settings: [port]`; status absorbs display_probe; display is `write`; raw usage codes are `dongle.keycodes` (aliases `dongle.keyboard`, `dongle.consumer`); hid.click refuses x/y (relative mouse); a firmware `ok: false` is an error; no `check` |
| proc | - | - | nothing to port: `shell.open` is the process producer; `proc.*` become `stream.*` (decided) |

Screen captures: adb's is the only worker-side one left (`adb.screen.capture`); the phone's
(`screen-android`) is an APK plugin and the TV has none.

### What batch 2 found

- **N8. Streams aren't wired on the worker.** `shell.open` needs `Host(streams=...)`; the worker
  runtime builds its Host without one, and the `stream.*` caps don't exist yet, so on a real worker
  `shell.open` answers `streams are not available here`. Fix (wiring): a bounded `Streams` per worker,
  the `stream.read/write/list/close` builtins, and the hub relay.
- **N9. Config exec actions don't go through `launch`** (the known `TODO(castling:manifest-v11)`).
  Every config cap that runs a tool (camera.capture, hid.type/move, screen.capture, battery, torrent,
  notification) runs it as the worker process, not through the normal-user/elevation path the scripts
  now use.
- **N10. `launch` needs `run_as` on a root worker and nothing gives plugin code one.** shell, adb and
  camera scripts call `launch(argv, ctx.lease)`; on a worker running as root that raises "no normal
  account (run_as)". Fix (wiring): put the worker's `run_as` on the call context, or let `launch` read
  it from core.
- **N11. A pairing result carries a secret.** `tv.pair` returns the TV's client key as a call result,
  so it lands in the journal unless the hub diverts it. Fix: a convention (a pair cap's
  `result.settings` goes to the instance's vault entries and is redacted from the journal) or
  `ctx.appliance.set(...)`.
- **N12. `ctx.file(path)` under `ctx.tmp` can't work.** `invoke` removes the per-call tmp dir before
  the host reads a `FileResult` path, so a script must return `data=` bytes (adb does). Fix: read the
  file before cleanup, or document it.
- **N13. `map:` is silently ignored on `run:` caps** (it only applies when an action renders args).
  tv and hid-desktop map key names in their scripts. Fix: say so, or warn in the validator.
- **N14. `map:` is per arg, not per OS or per try step.** xdotool keysyms, ydotool input codes and
  Windows virtual-key codes differ, which is why hid.key/click/drag are scripts though each is one
  command per tool.
- **N15. A try-chain falls through on failure for physical caps too.** A tool that half-typed before
  failing would be retried by the next; acceptable for xdotool's real failure (no display, nothing
  typed), worth knowing for any cap with side effects.
- **N16. `requires.python` isn't checked when a worker loads a plugin** (bins, hardware and settings
  are). A worker without `websockets` serves tv caps that fail with ImportError. Fine while
  distribution resolves deps with uv; a hand-loaded plugin could be marked unavailable instead.
- **N17. No serial port on Windows without pyserial**, and `requires.python` can't be per OS, so the
  dongle is POSIX-only now (0.4 covered Windows through pyserial).
- **N18. Elevation and in-process caps.** file.* can't elevate (nothing to launch); the gate's
  "re-run once with a lease" for `elevation: may` is unsafe for a shell command that may have
  half-run, so shell.exec never asks for it by itself (`denied: true` instead). Spec 7.2 could say
  `may` re-runs are for caps that are safe to repeat.
- **N19. No PTY on Windows.** ConPTY (0.4 `rook/worker/conpty.py`) isn't ported; `shell.open` takes
  `pty=false` there.
- Still open from batch 1: N3 (appliance values on the host call: adb, tv and cec tests stand in),
  N4 (checks with `{}`: shell, file, cec, camera, adb, tv and dongle have none), N5 (`display` for
  hid-desktop has no vocabulary yet).

## Gaps

What a plugin needs that manifest v1 can't say, grouped by section. Each has the plugin that hits it
and the smallest fix that would cover it. **Bold** = Bake should decide. Status lines are from the
1.1 pass: the overlays and converter use the new keys where they close a gap.

### A. Built-in actions

1. **Per-OS overrides can't change `parse`/`returns`.** battery: upower, pmset and
   termux-battery-status print three formats. Windows is made to print upower's shape from its
   script; macOS and Termux had no way, so battery is `platforms: [linux, windows]`. Fix: allow
   `parse` and `returns` inside `linux|windows|macos|android`.
   **Closed:** per-OS `parse`/`returns`; battery now covers macOS (`pmset`) and Termux.
2. **`try` only falls back when the binary is missing.** screen-desktop: grim is installed on many
   X11 desktops and fails there; today's code picks grim only under `WAYLAND_DISPLAY`. Fix: fall back
   on non-zero exit too (and say so).
   **Closed:** `try` falls back on a failing exit too; the last failure is the error.
3. **Unset optional args outside argv are undefined.** Nested-list dropping exists only for exec argv.
   kvm `quality` (http query), cec `operands` (tcp text). Fix: an unset arg drops its query/json/header
   key; in text it renders empty.
   **Closed:** an unset arg drops its query/json/header/form key and renders empty in text.
4. **No list rendering.** kvm hid.key `mods` (kvmd wants `keys=ControlLeft,KeyC`), cec operands
   (space-separated bytes). Fix: say how a list arg renders inside a string (e.g. `join: ","` on the
   arg).
   **Closed:** arg `join:`; cec operands are a list joined by spaces. kvm hid.key is config too
   (kvmd's `send_shortcut`, see the real ports).
5. **No sequences.** kvm key combos (press mods, key, release) and today's mouse click were several
   requests per call. hid.key stays `run:`; hid.click relies on kvmd's press-and-release when `state`
   is omitted (to verify on the PiKVM).
   **Open by decision:** sequences stay `run:`. Ports: only kvm hid.click at a pixel needs one;
   kvmd presses and releases a button without `state` (kvmd source).
6. **No value mapping or conditionals.** deluge state letters (S -> Seeding); pikvm `force=true` ->
   `off_hard` (done as a `mode` enum instead); cec.send "also takes a raw firmware line" (decision)
   can't share one `send` template with the structured frame. Fix options: split caps (cheap), or an
   arg-level `map:`.
   **Closed:** arg `map:` (caller value -> rendered value). cec's raw line still can't share `send` (no conditionals, by design).
7. **No failure read from output.** deluge-console exits 0 when it can't reach the daemon (torrent.list
   would return an empty list instead of an error); PiKVM's streamer answers 200 with a JSON error
   when there is no video; Pico-CEC replies `ERR ...`. Fix: `fail_if: { regex: ... }` on an action.
   **Closed:** action `fail_if: {regex}`; used by torrent (daemon down), kvm (streamer error) and cec (`ERR`).
8. **Argv-slot escaping doesn't protect tools that re-parse an argument.** deluge-console takes the
   whole command as one argument, splits on `;` and shlex-splits it, so `{{args.torrent_id}}` inside
   `"rm {{args.torrent_id}}"` could smuggle `; ...` or `--remove_data`. The overlay guards with arg
   `pattern`s (tested), at the cost of names with spaces and Windows paths. Fix: validator warns when
   an argv item mixes literal text with an arg template and the arg has no `pattern`.
   **Closed:** the validator warns (`argv-arg-unguarded`) unless the arg has a pattern, an enum or a number type.
9. **Option injection.** An arg alone in an argv slot that starts with `-` becomes an option
   (notify-send text `-x`). The overlay uses `--`. Fix: validator warns on an arg-only argv item with
   no preceding `--` and no pattern refusing a leading `-`.
   **Closed:** the validator warns (`argv-option-injection`) unless `--` comes first or the arg has a pattern, a safe enum or a number type with `min >= 0`.
10. **Parsed values are strings.** Regex captures and text (battery `percent: "87"`). Fix: coerce
    parse output to the cap's `result` field types.
   **Closed:** parse output is coerced to the cap's `result` types (battery, torrent).
11. **No post-processing step.** screenshot preview downscaled with ImageMagick after capture;
    `preview` is dropped for screen-desktop (kvm has it natively). spectacle/gnome-screenshot silently
    ignore a region.
   **Open by decision:** post-processing stays `run:`.
12. **URL paths and dict args.** kvm api_get/api_post take a raw `/api/...` path and a query dict;
    "URL parts are escaped" breaks slashes and there's no way to spread a dict into `query`. They stay
    `run:` (they're the raw escape hatch anyway).
   **Open by decision:** raw paths stay `run:`.
13. **Regex `each` semantics.** torrent.list anchors `^...$` per line; the page doesn't say whether
    `each` matches per line or with MULTILINE over the output. Fix: say per line.
   **Closed:** `each` matches line by line.
14. **Appliance `port` vs an instance setting named `port`** (cec defaults 9526): precedence unstated.
    Also `android` in per-OS keys means Termux here, but the APK is a different host (see D6).
    The overlays also assume a per-OS override inherits the base exec's other keys (`env`, `output`)
    and may set its own `env`; the page shows only `argv` in one.
   **Closed:** instance settings win (`rook.kit.actions.appliance_values`); per-OS overrides inherit the base exec's other keys; Termux is `termux` (D6).
15. **`hooks.status` needs Python.** battery puts `{percent, charging}` on the heartbeat; with only
    `run:` allowed the config plugin becomes code just for that, so the overlay drops it. Fix:
    `hooks.status: { call: battery.status }`.
   **Closed:** hooks take `call:` as well as `run:`; battery's heartbeat is `{call: battery.status}`.
16. `tls.verify` can't come from a setting (pikvm `insecure`). The overlay hardcodes `false`, like the
    page example. Minor.
   **Closed:** `tls.verify` may be one `{{settings.x}}` or `{{appliance.x}}`; kvm uses `verify_tls`.

### B. Caps, names and contracts

1. **A cap named just its namespace** (`help`): no key can express it. The converter uses `""`.
   Fix: bless `""` or a top-level shorthand.
   **Closed:** the cap key `""` is the bare namespace.
2. **Two routing modes behind `contracts`.** The page routes a contract to one provider chosen by a
   hub setting (llm.complete, notify.send). C1 device contracts route by *target*: `screen.capture` on
   kaiju vs on the TV vs on the phone. Fix: `contracts.<name>.route: provider | target`.
   **Closed:** `contracts.<name>.route: provider | target`.
3. **Contract caps get two names.** kvm declares `screen.capture` (full name `kvm.screen.capture`) and
   `provides: {screen.capture: kvm.screen.capture}`. Plugins named after a contract (battery, shell,
   voice) declare it directly with no `provides`. Fix: a plugin that `provides` a contract declares the
   cap under the contract name only.
   **Open.**
4. **No contract schemas exist and today's providers disagree** (the validator's "schema
   compatibility" check would fail them all). Someone has to write the 12 contracts into core's
   manifest; the converter doesn't invent them. Disagreements:
   hid.click (adb x,y / dongle buttons,pan,wheel,x,y / hid-desktop button,x,y / kvm button),
   hid.key (keys / key+mods / key+modifiers / tv name), hid.drag (x1..y2,duration vs
   start_x..end_y,duration_ms), hid.type (kvm slow, tv app/enter/replace), app.launch (package,label,
   screen vs app,params), notification.post (13 phone args / title,text / tv message), power.off
   (kvm mode / tv minutes), screen.capture (quality / preview / region), screen.on, sound.set,
   media.* (phone keeps a package arg), ui.screen.
   **Closed for the ports:** core.yaml has the contracts (wiring) and all five ports fit them (see
   the real ports). Other providers still disagree until they're ported.
5. **Splits on `run:`.** `run` can't bind a fixed argument, so a split Python cap needs a thin wrapper
   per new name: device.intent (8 names), device.sound (3), media.control (6), voice.speak (2),
   browser.tabs (2), dongle.mouse (3), tv.media (4), tv.screen (2). The converter removes the old
   `action`/`kind`/`op` arg from split caps; their `run:` target still expects it.
   **Closed:** cap-level `bind:`; the converter binds the old argument, its value guessed from the new name (check each when porting; media.seek was forward/rewind).
6. **Open args.** 22 surviving caps take `**kwargs` (browser.* 14, dongle.* 6, cec 2). Fix: say
   undeclared args are refused (the converter declares only named ones).
   **Closed:** undeclared args are refused.
7. **No "never time out".** decide.drive, home.ask/role.ask (agent.ask), worker.update are
   `timeout=None` today. Fix: `timeout: none`.
   **Closed:** `timeout: none`.
8. **Paging and projection.** 15 caps today use core-enforced `limit=`/`fields=` (list, history, runs,
   sessions, plugins, ...). No manifest key. Fix: `limit:`/`fields:` on caps, or leave it to records.
   **Closed by decision:** explicit `limit`/`offset` args, or records.
9. **Decisions route names outside their plugin.** device.intent splits into call.dial, clock.*,
   location.*; one namespace has one owner, so the converter sends them to plugins `call`, `location`
   (each now draws on two phone files) and a new `clock`. **Bake: confirm.**
   **Open:** Bake.
10. **`voice` is both a C1 contract (device speech out) and the hub voice service's namespace**
    (`voice.status` in the agents package). **Bake: rename one** (e.g. the service as `speech`).
   **Open:** Bake.

### C. Records

1. **Generated names have a record segment.** The kit generates `<plugin>.<record>.<verb>`; the
   decisions name most record caps `<plugin>.<verb>` (change.list, task.get, memory.search). The
   converter names the record after its plugin. Fix: a record named like its plugin drops the middle
   segment.
   **Closed:** a record named like its plugin generates `<plugin>.<verb>`.
2. **`history` isn't a generated verb.** `history: true` keeps revisions but the caps list on the page
   is list/get/search/create/update/archive/delete; the decisions keep 8 `*.history` caps. Fix:
   add `history`.
   **Closed:** `history` is generated when `history: true`.
3. **Non-kit verbs** stay `run:` caps beside the record: job.run/cancel/validate/templates,
   task.claim/release/link/comment, knowledge.link, whiteboard.read/write. `link` shows up twice:
   candidate kit verb.
   **Open:** they stay `run:` caps.
4. **Fields aren't derivable.** Schemas live in SQL migrations (6 hub plugins) or ad-hoc tables; the
   converter writes `fields: {}`. The kit agent writes them per type.

   **Open:** also, `search` needs search fields, so the converter keeps today's search as a `run:` cap until the port declares them.
5. **Generated record caps have no aliases.** Old names of record caps (e.g. `tasks.list`) have
   no key; the converter writes `aliases` on the record (an unknown key, warned). **Open.**

### D. Plugin level

1. **Dynamic availability.** 25 plugins override `available()`. Covered: bins/hardware (`requires`),
   appliance instances (pikvm, cec). Not covered: "only where a setting is set" (hub huddle, jobs,
   knowledge, memory, tasks, voice gate on config). Fix: `requires: { settings: [x] }`.
   **Closed:** `requires.settings` (plugin or cap). The converter can't infer which settings gate a plugin; the port adds them.
2. **Placement.** 32 hub plugins `run="one"`; `has('pty')`; `os != 'android'`. `runs_on: hub` covers
   the first if one-per-band is implied (say so). `pty` could be a `requires.hardware` value.
   **Closed:** `runs_on: hub` means one per band; `pty` is a `requires.hardware` value.
3. **Settings attributes lost:** `apply` (101 settings need reload/restart), legacy env names
   (`env`, `env_aliases`), `overridable` (3), `bootstrap` (2), group/order/advanced, label vs help, and
   types `url`/`path`/`resource` (connection strings, 9). Fix: at least `apply: reload|restart`.
   **Partly closed:** `apply: live | reload | restart` (the converter carries it) and types `url`, `path`. Env aliases, overridable, bootstrap and grouping are not carried.
4. **GUIDANCE (13 hub plugins) and SKILL fragments (38).** SKILL maps to `skills:` but describes 0.4
   cap names, so the converter doesn't carry it. GUIDANCE (operator-editable guidance slots) has no
   home.
   **Open.**
5. **DEPENDS** (attention -> approvals, huddle -> chat, tasks -> knowledge) hands over the other
   plugin's instance. 0.5 allows caps only: becomes `uses:` + `ctx.call` in the port.
   **Open:** `uses:` + `ctx.call` in the port.
6. **Android app vs Termux.** `place(ANDROID_APP)` (14 APK plugins) vs `ANDROID_APP_REPLACED`
   (Termux, 4). `platforms: [android]` can't tell them apart. Fix: `app` facet (`android-app`).
   **Closed:** platform and per-OS key `termux`; `android` is the app.
7. **Python contract vs today's functions.** Every `run:` names `Class.method` on a stateful plugin
   instance (`self.settings`, sockets, caches, the hub's own objects), not `func(ctx, **args)`. Also
   27 plugins with live caps have start/stop/heartbeat hooks; background loops inside `start` (jobs
   scheduler, home) should become `hooks.every` or jobs, which the converter can't see. **Decide:**
   allow `Class.method` with one instance per load, or port every cap to module functions + ctx.
   **Closed:** `run:` may name `file.py:Class.method` (one instance per plugin load, built with the plugin context; castling-manifest-python).
8. **Hub web pages and HTTP** (access, attention, ccmod, huddle, jobs `web.py`): map to `routes:` and
   `pages:`; not emitted.
   **Open.**

### E. Security facts the converter changed or didn't

- Changed: msg.send was `scope: external`; notification.post (a desktop pop-up) is `local`.
  torrent.remove keeps the files, so no `destructive`; torrent.delete has it. kvm power.on dropped
  `destructive`. Every `*.delete` cap gets `destructive` (castling-caps rule 3). Args named
  `*token|*password|*secret|api_key` are `type: secret` (e.g. stream.open `mcp_token`).
- Not inferred: the four new flags (`sends_as`, `spends`, `persists`, `taints`) have no 0.4 tag.
  Candidates for whoever ports them: sms.send, call.dial (`sends_as`); browser.goto/read/snapshot,
  homeassistant reads (`taints`); worker.update, plugin.install, claudecode.install,
  harness.shim.install, user.grant, user.assign_key, job.create (`persists`).

## Generated tables

<!-- generated:start -->
### Today's plugins -> 0.5

| side | plugin | caps | kept | binned | streams | goes to |
|---|---|---|---|---|---|---|
| core | node | 1 | 1 | 0 | 0 | caps |
| hub | access | 16 | 11 | 5 | 0 | user |
| hub | approvals | 2 | 2 | 0 | 0 | approval |
| hub | attention | 6 | 1 | 5 | 0 | attention |
| hub | ccmod | 9 | 5 | 4 | 0 | claudecode |
| hub | changes | 10 | 4 | 6 | 0 | change |
| hub | decide | 7 | 3 | 4 | 0 | decide |
| hub | discord | 2 | 0 | 2 | 1 | - |
| hub | help | 1 | 1 | 0 | 0 | help |
| hub | home | 18 | 1 | 17 | 2 | agent |
| hub | huddle | 10 | 1 | 9 | 5 | huddle |
| hub | info | 2 | 1 | 1 | 0 | hub |
| hub | jobs | 2 | 2 | 0 | 0 | job |
| hub | knowledge | 2 | 2 | 0 | 0 | knowledge |
| hub | llm | 3 | 3 | 0 | 0 | llm |
| hub | llmanifold | 1 | 0 | 1 | 0 | - |
| hub | manual | 4 | 0 | 4 | 0 | - |
| hub | marketplace | 2 | 2 | 0 | 0 | plugin |
| hub | memory | 8 | 6 | 2 | 0 | memory |
| hub | node | 3 | 3 | 0 | 0 | plugin |
| hub | notify | 2 | 2 | 0 | 0 | notify |
| hub | persona | 7 | 5 | 2 | 0 | persona |
| hub | policy | 4 | 0 | 4 | 0 | - |
| hub | roles | 15 | 6 | 9 | 3 | agent |
| hub | rooms | 4 | 0 | 4 | 3 | - |
| hub | routines | 3 | 0 | 3 | 0 | - |
| hub | serves | 3 | 3 | 0 | 0 | appliance |
| hub | settings | 9 | 5 | 4 | 0 | setting |
| hub | show | 4 | 1 | 3 | 0 | page |
| hub | tasks | 2 | 2 | 0 | 0 | concept, project, task, whiteboard |
| hub | telegram | 2 | 0 | 2 | 1 | - |
| hub | trust | 5 | 5 | 0 | 0 | trust |
| hub | updates | 6 | 4 | 2 | 0 | update |
| hub | voice | 1 | 1 | 0 | 0 | voice |
| phone | attention_android | 7 | 2 | 5 | 1 | notification-android |
| phone | battery_android | 1 | 1 | 0 | 0 | battery-android |
| phone | calendar_android | 4 | 4 | 0 | 0 | calendar |
| phone | calllog_android | 1 | 1 | 0 | 0 | call |
| phone | contacts_android | 1 | 1 | 0 | 0 | contact |
| phone | device_android | 20 | 16 | 4 | 0 | app-android, call, clipboard-android, clock, location, phone, screen-android, sound-android, worker |
| phone | hid_a11y | 5 | 5 | 0 | 0 | hid-android |
| phone | location_android | 1 | 1 | 0 | 0 | location |
| phone | media_android | 2 | 2 | 0 | 0 | media-android |
| phone | notify_android | 5 | 5 | 0 | 0 | notification-android |
| phone | screen | 2 | 1 | 1 | 0 | screen-android |
| phone | sms_android | 2 | 2 | 0 | 0 | sms |
| phone | speak_android | 5 | 4 | 1 | 0 | voice-android |
| phone | ui_android | 8 | 5 | 3 | 0 | ui-android |
| worker | adb | 11 | 9 | 2 | 0 | adb |
| worker | admin | 7 | 1 | 6 | 0 | plugin |
| worker | agenthost | 10 | 2 | 8 | 2 | harness |
| worker | battery | 1 | 1 | 0 | 0 | battery |
| worker | browser | 14 | 14 | 0 | 0 | browser |
| worker | camera | 2 | 2 | 0 | 0 | camera |
| worker | ccmod | 4 | 3 | 1 | 0 | claudecode |
| worker | cec | 3 | 2 | 1 | 0 | cec |
| worker | chat | 4 | 0 | 4 | 4 | - |
| worker | claude_history | 12 | 0 | 12 | 12 | - |
| worker | codex_history | 1 | 0 | 1 | 1 | - |
| worker | config | 4 | 0 | 4 | 0 | - |
| worker | core | 1 | 0 | 1 | 0 | - |
| worker | deluge | 7 | 7 | 0 | 0 | torrent |
| worker | devices | 1 | 0 | 1 | 0 | - |
| worker | devkit | 5 | 5 | 0 | 0 | appliance |
| worker | dongle | 7 | 6 | 1 | 0 | dongle |
| worker | enrollment | 5 | 0 | 5 | 0 | - |
| worker | file_ops | 5 | 5 | 0 | 0 | file |
| worker | hermes | 10 | 0 | 10 | 3 | - |
| worker | hid | 6 | 6 | 0 | 0 | hid-desktop |
| worker | homeassistant | 8 | 8 | 0 | 0 | homeassistant |
| worker | info | 3 | 1 | 2 | 0 | worker |
| worker | log | 2 | 1 | 1 | 0 | worker |
| worker | memory | 5 | 0 | 5 | 0 | - |
| worker | msg | 3 | 1 | 2 | 1 | notification-desktop |
| worker | persona | 2 | 0 | 2 | 0 | - |
| worker | pikvm | 9 | 9 | 0 | 0 | kvm |
| worker | proc | 6 | 0 | 6 | 6 | - |
| worker | screenshot | 3 | 1 | 2 | 0 | screen-desktop |
| worker | selfupdate | 9 | 4 | 5 | 0 | worker |
| worker | session_mirror | 1 | 0 | 1 | 1 | - |
| worker | session_shim | 3 | 3 | 0 | 0 | harness |
| worker | sessions | 4 | 0 | 4 | 4 | - |
| worker | shell | 4 | 3 | 1 | 0 | shell |
| worker | terminals | 9 | 5 | 4 | 4 | stream |
| worker | wake | 2 | 0 | 2 | 1 | - |
| worker | webos | 19 | 16 | 3 | 0 | tv |
| worker | work | 5 | 0 | 5 | 4 | - |

### 0.5 manifests

| plugin | package | caps | form | provides | from |
|---|---|---|---|---|---|
| adb | devices | 9 | run 9 | battery.status, hid.click, hid.drag, hid.key, hid.type, screen.capture, shell.exec, ui.screen | worker:adb |
| agent | agents | 1 | records 1 + run 1 | - | hub:home, hub:roles |
| app-android | devices | 5 | run 5 | app.launch, app.list, app.open_url, app.settings, app.share | phone:device_android |
| appliance | core | 5 | run 5 | - | hub:serves, worker:devkit |
| approval | core | 2 | builtin | - | hub:approvals |
| attention | agents | 0 | records 1 | - | hub:attention |
| battery | devices | 1 | config 1 | - | worker:battery |
| battery-android | devices | 1 | run 1 | battery.status | phone:battery_android |
| browser | devices | 15 | run 15 | - | worker:browser |
| calendar | devices | 4 | run 4 | - | phone:calendar_android |
| call | devices | 2 | run 2 | - | phone:calllog_android, phone:device_android |
| camera | devices | 2 | config 1 + run 1 | - | worker:camera |
| caps | core | 1 | builtin | - | core:node |
| cec | devices | 2 | config 2 | - | worker:cec |
| change | core | 0 | records 1 | - | hub:changes |
| claudecode | agents | 3 | records 1 + run 3 | - | hub:ccmod, worker:ccmod |
| clipboard-android | devices | 2 | run 2 | clipboard.get, clipboard.set | phone:device_android |
| clock | devices | 3 | run 3 | - | phone:device_android |
| concept | workbench | 0 | records 1 | - | hub:tasks |
| contact | devices | 1 | run 1 | - | phone:contacts_android |
| decide | agents | 3 | run 3 | - | hub:decide |
| dongle | devices | 7 | run 7 | hid.click, hid.key, hid.move, hid.release, hid.scroll | worker:dongle |
| file | devices | 5 | run 5 | - | worker:file_ops |
| harness | agents | 5 | run 5 | - | worker:agenthost, worker:session_shim |
| help | core | 1 | builtin | - | hub:help |
| hid-android | devices | 5 | run 5 | hid.click, hid.drag, hid.key, hid.status, hid.type | phone:hid_a11y |
| hid-desktop | devices | 6 | run 6 | hid.click, hid.drag, hid.key, hid.move, hid.status, hid.type | worker:hid |
| homeassistant | devices | 8 | run 8 | - | worker:homeassistant |
| hub | core | 1 | builtin | - | hub:info |
| huddle | agents | 1 | run 1 | - | hub:huddle |
| job | core | 4 | records 1 + run 4 | - | hub:jobs |
| knowledge | workbench | 2 | records 1 + run 2 | - | hub:knowledge |
| kvm | devices | 11 | config 8 + run 3 | hid.click, hid.key, hid.move, hid.type, power.off, power.on, power.reset, power.status, screen.capture | worker:pikvm |
| llm | core | 3 | builtin | - | hub:llm |
| location | devices | 3 | run 3 | - | phone:device_android, phone:location_android |
| media-android | devices | 7 | run 7 | media.next, media.pause, media.play, media.previous, media.seek, media.status, media.stop | phone:media_android |
| memory | agents | 1 | records 1 + run 1 | - | hub:memory |
| notification-android | devices | 6 | run 6 | notification.act, notification.dismiss, notification.get, notification.list, notification.post, notification.update | phone:attention_android, phone:notify_android |
| notification-desktop | devices | 1 | config 1 | notification.post | worker:msg |
| notify | core | 2 | builtin | - | hub:notify |
| page | core | 1 | builtin | - | hub:show |
| persona | agents | 0 | records 1 | - | hub:persona |
| phone | devices | 5 | run 5 | - | phone:device_android |
| plugin | core | 6 | builtin | - | hub:marketplace, hub:node, worker:admin |
| project | workbench | 0 | records 1 | - | hub:tasks |
| screen-android | devices | 4 | run 4 | screen.capture, screen.keep_on, screen.on, screen.status | phone:device_android, phone:screen |
| screen-desktop | devices | 1 | config 1 | screen.capture | worker:screenshot |
| setting | core | 5 | builtin | - | hub:settings |
| shell | devices | 3 | run 3 | - | worker:shell |
| sms | devices | 2 | run 2 | - | phone:sms_android |
| sound-android | devices | 3 | run 3 | sound.dnd, sound.set, sound.status | phone:device_android |
| stream | core | 5 | builtin | - | worker:terminals |
| task | workbench | 5 | records 1 + run 5 | - | hub:tasks |
| torrent | devices | 8 | config 7 + run 1 | - | worker:deluge |
| trust | core | 4 | records 1 + run 4 | - | hub:trust |
| tv | devices | 19 | run 19 | app.launch, app.list, app.open_url, hid.key, hid.type, media.pause, media.play, media.seek, media.stop, notification.post, power.off, power.on, screen.off, screen.on, sound.set | worker:webos |
| ui-android | devices | 5 | run 5 | ui.click, ui.screen, ui.scroll, ui.type, ui.wait | phone:ui_android |
| update | core | 4 | builtin | - | hub:updates |
| user | core | 10 | records 1 + run 10 | - | hub:access |
| voice | agents | 1 | run 1 | - | hub:voice |
| voice-android | devices | 5 | run 5 | voice.ask, voice.replies, voice.speak, voice.stop, voice.voices | phone:speak_android |
| whiteboard | workbench | 2 | run 2 | - | hub:tasks |
| worker | core | 6 | builtin | - | phone:device_android, worker:info, worker:log, worker:selfupdate |

Totals: 457 caps today; keep 165, reshape 83, bin 209; 63 manifests, 245 declared caps, 13 record types.
<!-- generated:end -->
