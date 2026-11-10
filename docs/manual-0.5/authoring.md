# Writing a plugin

A plugin is a thin adapter: it drives software, a device or an API that
already exists, so an agent gets one clear call instead of a ten-step shell
struggle. There is one way to write one: a manifest plus Python scripts that
run on the hub. Workers only reach things (run a command, read and write
files, open a stream); the script on the hub knows what to say.

```
myplugin/
  plugin.yaml      the manifest: required
  scripts/*.py     the code each cap runs (run:)
  docs/manual.md   the plugin's manual page
  pages/*          dashboard pages
  skills/*.md  personas/*.md
```

The manifest is the whole contract: the hub validates it and answers "what
does this cap do" from it with the worker offline. Plugins never import each
other; they call each other's caps.

## The smallest plugin: a worker's cap

```yaml
manifest: 1
plugin: uptime
version: 0.1.0
doc: How long a worker has been up.
runs_on: hub
platforms: [linux]
uses: [shell.exec]
caps:
  get:
    doc: The worker's uptime line.
    on: worker
    risk: read
    scope: local
    idempotent: true
    run: scripts/uptime.py:get
docs: { manual: docs/manual.md }
```

```python
# scripts/uptime.py
async def get(ctx):
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=["uptime"])
    if out["code"] != 0:
        raise ctx.Error(out["stderr"].strip() or "uptime failed")
    return out["stdout"].strip()
```

Only `manifest`, `plugin` and `version` are required at the top. The
plugin's name is its caps' namespace: this cap is `uptime.get`. Every cap
needs `doc`, `risk` and `scope` ([Caps](caps.md)) and a `run:` target.

`on: worker` makes `uptime.get` a cap **of a worker**: the hub lists it
among the caps of every worker whose `os` is in `platforms` (none: every os)
and that serves the caps in `uses`, and an agent calls it there
(`worker=kaiju cap=uptime.get`). The script runs on the hub with
`ctx.worker` (`{id, name, os}`) and reaches the machine through the
worker's primitives: `shell.exec`, `shell.env`, `file.*`, and streams
(`shell.open`; on the Rust worker also `net.open`, `net.udp`,
`serial.open`). Each of those calls is journaled as its own call, linked to
the outer one by `parent`. The hub doesn't check for programs on the worker:
run the command and report the real failure. A cap without `on: worker` in
a hub plugin is a cap of the hub itself (`worker=rook`).

- An argument goes in its own `argv` slot, never into a shell string. A
  tool that re-parses its arguments (deluge-console splits on `;`) needs a
  `pattern` on the arg: that is the injection guard.
- `settings` are set on the hub ([Settings](settings.md)) and read as
  `ctx.settings["name"]`, secrets resolved.
- `undo` names the cap that reverses this one.

## A device: an appliance

A device that runs no worker (a TV, a PiKVM, a bridge on the LAN) is an
**appliance**: an instance with a name, onboarded with `appliance.add`
([Workers](workers.md#appliances)), reached through a delegate worker on its
network. The plugin declares how to find one and its per-device settings;
the same `on: worker` caps are then called by the device's name, and the
script gets the instance as `ctx.appliance` (`{name, host, port,
settings}`, secrets resolved) with the delegate as `ctx.worker`.

```yaml
manifest: 1
plugin: pikvm
version: 1.0.0
package: devices
runs_on: hub
uses: [net.open, stream.read, stream.write, stream.close]
appliance:
  discover: [ { http: { url: "https://{{host}}/api/info", match: kvmd } } ]
  settings:
    password: { secret: true, setup: Ask the user for the PiKVM web password. }
caps:
  power.status:
    doc: "ATX state: {power, hdd}."
    on: worker
    risk: read
    scope: local
    run: scripts/pikvm.py:status
```

```python
# scripts/pikvm.py
import base64, json
from rook.kit.net import http, values

async def status(ctx):
    a = values(ctx.appliance)            # name, host, port, then its settings
    auth = base64.b64encode(f"admin:{a['password']}".encode()).decode()
    code, _, body = await http(ctx, ctx.worker["id"], "GET", a["host"], 443, "/api/atx",
                               headers={"Authorization": "Basic " + auth}, tls=True, verify=False)
    if code != 200:
        raise ctx.Error(f"PiKVM answered {code}")
    leds = json.loads(body)["result"]["leds"]
    return {"power": leds["power"], "hdd": leds["hdd"]}
```

`rook.kit.net` is what the device plugins share: `Stream` (a worker's TCP,
TLS, UDP or serial stream, read until a condition), `http` (one HTTP/1.1
request over it) and `WebSocket`. `appliance.discover` hints tell an agent
what an onboarding search (`appliance.discover` on a worker) should look
for; `setup` tells the person what to give.

## The call context

A function (sync or async) gets `ctx` and the cap's arguments as keywords,
and returns JSON-able data, `ctx.file(...)` or `ctx.stream(...)`.
`ctx.Error("...")` fails cleanly with that message (`retry=True` if trying
again later may work). `ctx` also carries:

- who asks: `ctx.person`, `ctx.agent`, `ctx.session`, `ctx.why`;
- `ctx.worker` and `ctx.appliance` for `on: worker` caps;
- `ctx.settings` (secrets resolved), `ctx.data` (the plugin's own
  folder), `ctx.tmp` (removed after the call), `ctx.log`;
- `ctx.call(cap, **args)` and `ctx.call_on(worker, cap, **args)`: other
  caps, only those the manifest's `uses:` lists, and never more than the
  caller may do;
- `ctx.emit(event, payload)` for events the manifest declares in `emits:`;
- `ctx.records` for the plugin's own records.

A plugin with any `.py` file is treated as code: reviewed and signed like
code. Keep dependencies few; list them in `requires.python`.

A few plugins still run on a worker, because what they do has to live on
that machine (`shell`, `file`, the browser, the phone app's plugins): they
have no `runs_on: hub`, the hub pushes them to a worker, and their scripts
start processes only through core's launch path,
`rook.core2.worker.security.launch(argv, getattr(ctx, "lease", None), ...)`,
never `subprocess` directly ([Workers](workers.md#normal-user-elevation-on-request)).
A new plugin is a hub script.

## A hub plugin: records, presets, pages

A hub plugin can declare **records** instead of building storage: the kit gives each record type its caps
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
the payload: an event from a machine is its owner's data, and a hook (or a
cap a job hands the event to) can answer on the same machine with
`ctx.call_on(payload["worker"], ...)`.
While the hub is away the worker keeps the newest 1000 events and sends
them when it is back.

## Other sections

`contracts`/`provides` (answering a shared cap like `screen.capture` or
`llm.complete`: declare the cap under the contract's name and list it,
`provides: [screen.capture]`; a grant on `screen.capture` then covers your
cap when someone calls it by that name),
`personas`, `skills`, `components`, `jobs` ([Jobs](jobs.md)),
`shell` (script and style files the hub site's own pages load, outside the
page sandbox, and the caps they may call through the site as the signed-in
person: `shell: {dock: {file: shell/dock.js, caps: [agent.chat.send]}}`; the
agents package's chat dock is one). Each section has its own
knowledge page, `castling-manifest-<section>`; the index is
`castling-manifest`. The JSON Schema is
`conformance/manifest/manifest.schema.json`.

## Checking and trying it

```sh
python -m rook.kit.manifest validate myplugin/        # errors and warnings, then a summary
python -m rook.kit.manifest diff old/ new/            # breaking changes and the version bump they need
```

Then put the folder in the hub's library ([Plugins](plugins.md#the-hubs-library))
and restart the hub: its caps show up on every worker it fits. Bump
`version` for every change.
Write the plugin's `docs/manual.md` (what each cap does, its settings, its
quirks) and list it in the manifest as `docs: { manual: docs/manual.md }`.
