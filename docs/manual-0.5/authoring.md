# Writing a plugin

A plugin is a thin adapter: it drives software or an API that already
exists, so an agent gets one clear call instead of a ten-step shell
struggle. Most plugins are only a `plugin.yaml`. Write code only for a cap
that needs logic.

```
myplugin/
  plugin.yaml      the manifest: required
  scripts/*.py     code for run: caps, hooks and gates
  docs/manual.md   the plugin's manual page
  pages/*          dashboard pages (hub plugins)
  skills/*.md  personas/*.md  jobs/*.py
```

The manifest is the whole contract. The hub validates it, answers "what
does this cap do" from it with the worker offline, and signs the folder
when it installs it. You never write `plugin.sig`. Plugins never import each
other; they call each other's caps.

## The smallest plugin

```yaml
manifest: 1
plugin: hello
version: 0.1.0
caps:
  say:
    doc: Say hello.
    risk: read
    scope: local
    action:
      exec: { argv: [echo, hello] }
      parse: text
```

Only `manifest`, `plugin` and `version` are required at the top. The
plugin's name is its caps' namespace: this cap is `hello.say`. Every cap
needs `doc`, `risk` and `scope` ([Caps](caps.md)).

## A config plugin: Deluge

A cut-down version of the shipped `torrent` plugin. No code: each cap
maps to a built-in **action**.

```yaml
manifest: 1
plugin: deluge
version: 1.0.0
title: Deluge
doc: List and pause torrents through deluge-console.
requires: { bins: [deluge-console] }
settings:
  console: { default: deluge-console, where: worker, doc: The deluge-console program. }
caps:
  list:
    doc: Every torrent, one line each.
    risk: read
    scope: local
    idempotent: true
    action:
      exec: { argv: ["{{settings.console}}", info], env: { LANG: C } }
      fail_if: { regex: '(?i)^\s*could not connect' }
      parse: lines
  pause:
    doc: Pause one torrent by its hash.
    risk: write
    scope: local
    undo: deluge.resume
    args:
      id: { type: str, required: true, pattern: '^[0-9a-f]{40}$', doc: The torrent's 40-hex hash. }
    action:
      exec: { argv: ["{{settings.console}}", "pause {{args.id}}"] }
      parse: text
  resume:
    doc: Resume one torrent by its hash.
    risk: write
    scope: local
    args:
      id: { type: str, required: true, pattern: '^[0-9a-f]{40}$' }
    action:
      exec: { argv: ["{{settings.console}}", "resume {{args.id}}"] }
      parse: text
check: [deluge.list]
```

What each part does:

- `requires.bins`: a worker without `deluge-console` shows these caps as
  unavailable, with the reason, instead of failing strangely.
- `settings`: values a person sets per worker (`worker.yaml`) or on the hub
  ([Settings](settings.md)). Templates read them as `{{settings.console}}`.
- `exec.argv`: always a list; each item is one argument slot, never a
  shell string, so an argument can't break out of its slot.
- `pattern` on `id`: deluge-console re-splits its command on `;`, so the
  pattern is the injection guard. The validator warns when an argument
  sits inside a larger argv item without a `pattern`, `enum` or number
  type (`argv-arg-unguarded`), or could start with `-`
  (`argv-option-injection`).
- `fail_if`: deluge-console exits 0 when it can't reach the daemon; a
  matching line turns that into an error.
- `parse`: `text`, `lines`, `json`, `kv`, `csv`, `bytes` (a file), or
  `{regex: "(?P<name>...)", each: true}` for one item per matching line.
  `returns` picks fields from the result.
- `undo`: names the cap that reverses this one.
- `check`: caps called after every install or update; a failure rolls the
  plugin back by itself. Pick a cap that needs nothing running.

## More from the action engine

Five action types: `exec`, `http` (method, url, headers, query,
json/form/body, basic or bearer auth, `tls: {verify}`), `tcp` (send a line,
read the reply), `ws` (one message, one reply) and `file` (read or write).

```yaml
action:
  exec:
    argv: ["{{settings.console}}", info, ["-s", "{{args.state}}"]]   # a nested list is dropped unless its args are set
    windows: { argv: ["{{settings.console}}.exe", info] }           # per-OS override: linux, windows, macos, android, termux
    try: [ { argv: [grim, "{{tmp}}/s.png"] }, { argv: [scrot, "{{tmp}}/s.png"] } ]   # fallbacks, in order
    output: "{{tmp}}/s.png"                                          # return this file instead of stdout
```

Template variables: `args.*`, `settings.*`, `appliance.*`, `worker.os`,
`worker.arch`, `worker.name`, `tmp` (a per-call folder), `plugin.dir`,
`plugin.data`, and secrets as `{{secret:name}}`. URL parts are escaped and
headers can't carry newlines. There are no loops, conditionals or
variables in YAML: logic means `run:`. An `exec` whose program comes from an
argument must be `risk: exec`. The full rules are on the knowledge page
`castling-manifest-actions`.

## An appliance: PiKVM

An appliance plugin targets devices that run no worker; a worker that can
reach the device carries the call. A cut-down version of the shipped `kvm`
plugin:

```yaml
manifest: 1
plugin: pikvm
version: 1.0.0
package: devices
appliance:
  discover: [ { http: { url: "https://{{host}}/api/info", match: kvmd } } ]
  settings:
    password: { secret: true, setup: Ask the user for the PiKVM web password. }
    verify_tls: { type: bool, default: false }
caps:
  power.set:
    doc: Press the ATX power button.
    risk: write
    scope: local
    effects: [physical]
    args: { action: { type: str, enum: [on, off, off_hard, reset_hard], required: true } }
    action:
      http:
        method: POST
        url: "https://{{appliance.host}}/api/atx/power"
        query: { action: "{{args.action}}" }
        auth: { basic: [admin, "{{appliance.password}}"] }
        tls: { verify: "{{appliance.verify_tls}}" }
  screen.get:
    doc: Current screen as JPEG.
    risk: read
    scope: local
    effects: [sensitive]
    result: { type: file }
    action:
      http: { url: "https://{{appliance.host}}/api/streamer/snapshot", auth: { basic: [admin, "{{appliance.password}}"] }, tls: { verify: "{{appliance.verify_tls}}" } }
      fail_if: { regex: '\A\{\s*"ok"\s*:\s*false' }
      parse: bytes
```

- `appliance.discover`: how onboarding finds one (here: `/api/info`
  answering `kvmd`).
- `appliance.settings`: per-device values; `secret: true` keeps the
  password in the vault, fetched per call. `setup` tells the person (or the
  agent onboarding the device) what to do.
- Templates read the device as `{{appliance.host}}` and its settings as
  `{{appliance.<name>}}`; the caps take no host argument.
- `effects: [physical]` keeps `power.set` from children and guests;
  `sensitive` marks the screen as private content.

The hub can't onboard appliances yet ([Workers](workers.md#appliances)),
so test appliance caps through the action engine directly, as the kvm,
tv, adb and cec plugin tests do.

## Code: `run:`

When a cap needs logic, point it at a function:

```yaml
manifest: 1
plugin: uptime
version: 0.1.0
caps:
  since:
    doc: When this machine booted, as Unix time.
    risk: read
    scope: local
    idempotent: true
    result: { type: map, fields: { booted: { type: time } } }
    run: scripts/uptime.py:since
```

```python
# scripts/uptime.py
import time

def since(ctx):
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except OSError:
        raise ctx.Error("no /proc/uptime on this machine")
    return {"booted": int(time.time() - up)}
```

A function (sync or async) gets `ctx` and the cap's arguments as keywords,
and returns JSON-able data, `ctx.file(...)` or `ctx.stream(...)`.
`ctx.Error("...")` fails cleanly with that message (`retry=True` if trying
again later may work). `ctx` also carries:

- who asks: `ctx.person`, `ctx.agent`, `ctx.session`, `ctx.why`;
- `ctx.settings` (secrets resolved), `ctx.data` (the plugin's own
  folder), `ctx.tmp` (removed after the call), `ctx.log`;
- `ctx.call(cap, **args)` and `ctx.call_on(worker, cap, **args)`: other
  caps, only those the manifest's `uses:` lists, and never more than the
  caller may do;
- `ctx.emit(event, payload)` for events the manifest declares in `emits:`;
- `ctx.records` for the plugin's own records (hub plugins).

Start processes only through core's launch path,
`rook.core2.worker.security.launch(argv, getattr(ctx, "lease", None), ...)`,
never `subprocess` directly, so no plugin can skip the normal-user rule and
elevation ([Workers](workers.md#normal-user-elevation-on-request)). A
plugin with any `.py` file is treated as code: reviewed and signed like
code. Keep dependencies few; list them in `requires.python` and the hub
resolves them for each worker before it installs.

## A hub plugin: records, presets, pages

Plugins can run on the hub (`runs_on: hub`) and declare **records**
instead of building storage: the kit gives each record type its caps
(`list`, `get`, `search`, `create`, `update`, `archive`, `history`),
change history and events.

```yaml
manifest: 1
plugin: homework
version: 0.1.0
title: Homework
doc: Homework items for the family, with a board page.
runs_on: hub
uses: [notify.send]
records:
  item:
    fields:
      title: { type: str, required: true }
      due: { type: time }
      done: { type: bool, default: false }
emits:
  homework.overdue: { doc: An item is past due and not done., payload: { item: { type: str } } }
caps:
  overdue:
    doc: Items past due and not done.
    on: hub
    risk: read
    scope: multi-user
    idempotent: true
    run: scripts/homework.py:overdue
hooks:
  every: [ { interval: 1h, run: scripts/homework.py:check_due } ]
presets:
  child: { grants: ["homework.item.*", "homework.overdue"] }
pages:
  board: { title: Homework, file: pages/board.html, caps: ["homework.*"], nav: { section: Home } }
docs: { manual: docs/manual.md }
```

- `records.item` makes `homework.item.list`, `homework.item.create`, ...
- `presets.child` lets children use them. A plugin may add grants only
  inside its own namespace, and never past a preset's limits: these caps
  have no effects, so a child can use them.
- `pages.board` is a sandboxed page that may call only `homework.*`
  ([The hub site](dashboard.md)).

## Hooks and events

`hooks:` runs code without a call: `start` and `stop` (open and close
connections), `status` (a small live status announced with the worker),
`every: [{interval, run}]` for timers, and `on: [{event, call | run, args}]`
to react to an event. Hooks act as the plugin itself, with only its own
namespace and its `uses`. Events a plugin sends are declared in `emits:`
and can be granted like caps. An event about one person's data carries a
`person` field (declare it in the payload): pages show it only to that
person and owners. An event with `taints: true` (outside content: an
inbound message) that names nobody reaches owners' pages only.

`ctx.emit(name, payload)` sends one. On a worker it runs that worker's own
`on:` hooks and goes to the hub, which takes it only when the payload fits
the declared fields (no others) and the worker's ceiling grants the name.
The hub sets `person` (whoever owns the worker) and `worker` (its name) on
the payload: an event from a machine is its owner's data, and a hook or job
can answer on the same machine with `ctx.call_on(payload["worker"], ...)`.
While the hub is away the worker keeps the newest 1000 events and sends
them when it is back. Mark outside content (an inbound message, another
app's notification) `taints: true`: hooks and jobs that act on it run their
calls in a tainted session, which the security gate watches more closely.

## Other sections

`gate` (a security check at dispatch, [Security](security.md#writing-a-gate)),
`contracts`/`provides` (answering a shared cap like `screen.capture` or
`llm.complete`: declare the cap under the contract's name and list it,
`provides: [screen.capture]`; a grant on `screen.capture` then covers your
cap when someone calls it by that name), `routes` (HTTP endpoints of a hub plugin, under `/p/<plugin>/`),
`personas`, `skills`, `concepts`, `components`, `jobs` ([Jobs](jobs.md)),
`assets` (large downloads with their sha256). Each section has its own
knowledge page, `castling-manifest-<section>`; the index is
`castling-manifest`. The JSON Schema is
`conformance/manifest/manifest.schema.json`.

## Checking and trying it

```sh
python -m rook.kit.manifest validate myplugin/        # errors and warnings, then a summary
python -m rook.kit.manifest diff old/ new/            # breaking changes and the version bump they need
```

Then try it on one worker: add the folder to `plugins:` in `worker.yaml`
and restart that worker, or put it in the hub's library and push it
([Plugins](plugins.md#installing-a-plugin-on-a-worker)), which loads it
without a restart and rolls back a failing check. Bump `version` for every
change you push: a worker refuses the same version with different files.
Write the plugin's `docs/manual.md` (what each cap does, its settings, its
quirks) and list it in the manifest as `docs: { manual: docs/manual.md }`.
