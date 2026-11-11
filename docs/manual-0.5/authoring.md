# Writing a plugin

A plugin is a thin adapter: it drives software, a device or an API that
already exists, so an agent gets one clear call instead of a ten-step shell
struggle. There is one way to write one: a manifest plus Python scripts that
run on the hub. Workers only reach things (run a command, read and write
files, open a stream); the script on the hub knows what to say.

**How to write one**, step by step with two worked examples (a worker's
cap, a record type with a page), what `ctx` gives a script, how to check
it and how it gets onto the hub: the agents' skill,
[plugins](../../rook/plugins/agents/skills/plugins.md) (agents read it with
`agent.skill name=plugins`). This page is the reference for every key.

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
other; they call each other's caps. The JSON Schema is
`conformance/manifest/manifest.schema.json`; each section also has a
knowledge page, `castling-manifest-<section>` (index: `castling-manifest`).

## Top-level keys

| Key | |
|---|---|
| `manifest` | `1`. Required |
| `plugin` | the name, and its caps' namespace. Required |
| `version` | semver; bump it for every change. Required |
| `title`, `doc` | a display name; one line on what it is for |
| `package` | `core`, `agents`, `workbench` or `devices`: which level installs it |
| `segment` | where in the site its pages and settings belong: `Agents`, `Machines`, `Work`, `Hub` or a new name; none: `Hub` |
| `runs_on` | `hub` for every new plugin ([below](#plugins-that-run-on-a-worker)) |
| `platforms` | `linux`, `windows`, `macos`, `android`, `termux`, `ios`: the workers its `on: worker` caps fit |
| `uses` | every cap its code calls, globs allowed; `ctx.call` refuses any other |
| `requires` | `python` (pip requirements), `bins`, `hardware`, `settings` |
| `settings` | [Settings](#settings) |
| `caps` | [Cap keys](#cap-keys) |
| `records` | [Records](#records) |
| `appliance` | [Appliances](#appliances) |
| `emits`, `hooks` | [Hooks and events](#hooks-and-events) |
| `jobs` | ready-made jobs: `{doc, trigger, call, args, as, persona, enabled, catch_up}` ([Jobs](jobs.md)) |
| `pages`, `components`, `shell` | [Pages](#pages-components-and-the-shell) |
| `presets` | [Presets](#presets) |
| `contracts`, `provides` | [Contracts](#contracts) |
| `personas`, `skills` | agents and their playbooks ([Agents](agents.md)) |
| `check` | caps run after an install; a failing one rolls a worker plugin back |
| `docs` | `{manual: docs/manual.md}`: what each cap does, its settings, its quirks |
| `license`, `source` | |

## Cap keys

A cap's name is `<plugin>.<key>`. `doc`, `risk` and `scope` are required
([Caps](caps.md)).

| Key | |
|---|---|
| `doc` | what it does and returns, for an agent reading it cold |
| `on` | `worker`: a cap of a worker or appliance ([below](#on-worker-caps)); `hub`: a cap of the hub (`worker=rook`) |
| `run` | `scripts/<file>.py:<function>` (or `:Class.method`) |
| `risk` | `read`, `write`, `exec`, `admin` |
| `scope` | `self`, `local`, `multi-machine`, `multi-user`, `external` |
| `effects` | e.g. `destructive`, `physical`, `sends_as` |
| `args` | name: `{type, required, default, enum, min, max, pattern, items, fields, to, doc}` |
| `result` | the same type language |
| `timeout` | a duration (`20s`, `10m`) or `none` |
| `idempotent` | `true` when calling twice changes nothing more |
| `undo` | the cap that reverses this one |
| `examples` | `[{args, result, doc}]` |
| `stream` | the cap answers a stream: `{types, input, durable}` |
| `platforms` | narrows the plugin's `platforms` for this cap |

Types: `str`, `int`, `float`, `bool`, `time`, `duration`, `markdown`,
`json`, `file`, `secret`, `ref`, `list`, `map`, `url`, `path`, `worker` (a
worker's name).

An argument a script passes to a program goes in its own `argv` slot, never
into a shell string. A tool that re-parses its arguments (deluge-console
splits on `;`) needs a `pattern` on the arg: that is the injection guard.

## `on: worker` caps

The hub lists an `on: worker` cap among the caps of every worker whose
`os` is in `platforms` (none: every os) and that serves the caps in
`uses`, and an agent calls it there (`worker=kaiju cap=battery.status`).
The script runs on the hub with `ctx.worker` (`{id, name, os}`) and
reaches the machine through the worker's primitives: `shell.exec`,
`shell.env`, `file.*`, and streams (`shell.open`; on the Rust worker also
`net.open`, `net.udp`, `serial.open`). Each of those calls is journaled as
its own call, linked to the outer one by `parent`. A cap without `on:
worker` in a hub plugin is a cap of the hub itself.

## Settings

`settings:` name: `{type, default, doc, secret, where, setup, apply, enum,
min, max, pattern}`, set on the hub ([Settings](settings.md)) and read as
`ctx.settings["name"]`, secrets resolved. A hub plugin's setting is the
hub's unless `where: person` makes it each person's own
(`ctx.settings_for(person)` reads someone else's). Give every setting a
one-line `doc`: the Settings page shows it as the setting's description.

## Appliances

A device that runs no worker (a TV, a PiKVM, a bridge on the LAN) is an
**appliance**: an instance with a name, onboarded with `appliance.add`
([Workers](workers.md#appliances)), reached through a delegate worker on its
network. Its plugin's `on: worker` caps are called by the device's name;
the script gets the instance as `ctx.appliance` (`{name, host, port,
settings}`, secrets resolved) with the delegate as `ctx.worker`.

| `appliance:` key | |
|---|---|
| `discover` | hints for an onboarding search, one kind per entry: `mdns`, `ssdp`, `http: {url, match}` (`{{host}}` in the url), `usb`, `adb`, `ble`, `serial` |
| `settings` | per-device settings, same keys as `settings`; `setup` tells the person what to give |
| `pair` | a cap `appliance.add` runs first; a secret it returns goes to the vault |

`rook.kit.net` is what the device plugins share: `Stream` (a worker's TCP,
TLS, UDP or serial stream, read until a condition), `http` (one HTTP/1.1
request over it), `WebSocket`, and `values(ctx.appliance)` (name, host,
port, then its settings, flat). Examples: `rook/plugins/cec` (TCP),
`rook/plugins/kvm` (HTTP), `rook/plugins/tv` (WebSocket).

## Records

`records:` type: the kit gives storage, change history, events and caps.

| Key | |
|---|---|
| `fields` | name: the arg keys, plus `unique`. Required |
| `index` | fields to filter on |
| `search` | fields `search` matches words in |
| `history` | keep every revision (adds the `history` cap) |
| `private` | each record seen only by its creator (and owners) |
| `retain` | a duration: records not updated for that long are erased |
| `caps` | which of `list`, `get`, `search`, `create`, `update`, `archive`, `delete`, `history` (default: all but `delete`, `search` with search fields, `history` with history) |
| `risk` | per verb, over the defaults (`read` for looks, `write` for changes) |

The caps are `<plugin>.<type>.<verb>`, or `<plugin>.<verb>` for a type
named like its plugin. Writes emit `<plugin>.<type>.created`, `.updated`
and `.archived`. Scripts use `ctx.records.<type>`.

## Pages, components and the shell

- `pages:` name: `{title, file, caps, events}`: a sandboxed page (no
  network) that calls only the caps it lists, as whoever views it
  ([The hub site](dashboard.md)). It sits in the sidebar under the
  plugin's `segment`.
- `components:` name: `{title, doc, file, props, caps}`: a part a layout
  page mounts (`page.components` lists them).
- `shell:` name: `{file, caps}`: script and style files the hub site's own
  pages load, outside the page sandbox, with the caps they may call as the
  signed-in person (`shell: {dock: {file: shell/dock.js, caps:
  [agent.chat.send]}}`; the agents package's chat dock is one).

## Hooks and events

`hooks:` runs code without a call: `start` and `stop` (open and close
connections), `status` (a small live status announced with the worker),
`every: [{interval, run | call, args}]` for timers, and `on: [{event,
filter, call | run, args}]` to react to an event. Hooks act as the plugin
itself, with only its own namespace and its `uses`. Events a plugin sends
are declared in `emits:` (`{doc, payload, taints}`) and can be granted like
caps. An event about one person's data carries a `person` field (declare
it in the payload): pages show it only to that person and owners. An event
with `taints: true` (outside content: an inbound message) that names nobody
reaches owners' pages only.

`ctx.emit(name, payload)` sends one. On a worker it runs that worker's own
`on:` hooks and goes to the hub, which takes it only when the payload fits
the declared fields (no others) and the worker's ceiling grants the name.
The hub sets `person` (whoever owns the worker) and `worker` (its name) on
the payload: an event from a machine is its owner's data, and a hook (or a
cap a job hands the event to) can answer on the same machine with
`ctx.call_on(payload["worker"], ...)`.
While the hub is away the worker keeps the newest 1000 events and sends
them when it is back.

## Presets

`presets:` name: `{grants, max_risk, effects, expires}` adds grants to a
preset (`child: {grants: ["homework.item.*"]}` lets children use those
caps). A plugin may add grants only inside its own namespace, and never
past a preset's limits.

## Contracts

`contracts`/`provides`: answering a shared cap like `screen.capture` or
`llm.complete`. Declare the cap under the contract's name and list it,
`provides: [screen.capture]`; a grant on `screen.capture` then covers your
cap when someone calls it by that name.

## Plugins that run on a worker

A few plugins still run on a worker, because what they do has to live on
that machine (`shell`, `file`, the browser, the phone app's plugins): they
have no `runs_on: hub`, the hub pushes them to a worker
([Plugins](plugins.md#installing-a-plugin-on-a-worker)), and their scripts
start processes only through core's launch path,
`rook.core2.worker.security.launch(argv, getattr(ctx, "lease", None), ...)`,
never `subprocess` directly ([Workers](workers.md#normal-user-elevation-on-request)).
A new plugin is a hub script.

A plugin with any `.py` file is treated as code: reviewed and signed like
code. Keep dependencies few; list them in `requires.python`.
