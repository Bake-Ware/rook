---
description: Write a Rook plugin - a manifest plus Python scripts that run on the hub - so agents get one clear call for a device, program or service, or a new record type with a page.
when: something shows up that agents can't reach with one call (a device, a program on a worker, a web API), someone wants a new kind of record or a page backed by it, or a job needs logic.
---
# Writing a plugin

One way: a folder with a `plugin.yaml` (the manifest) and, if it needs
code, Python scripts that run **on the hub**. Workers only reach things;
your script knows what to say to them.

**First, the test.** Does it give an agent access or capability it lacks
today, using a mechanism Rook already has? Look before you write:
`caps match="<word>"`, `call worker=rook cap=hub.manifests`. If a cap
already does it, use it. If the person only wants a page over caps that
exist, that is `page.create` (the hub-pages skill), not a plugin.

## 1. Pick the kind

- **A worker's cap**: run a program or read files on a machine that has a
  worker. Cap `on: worker`; the script reaches the machine through the
  worker's primitives: `shell.exec`, `file.*`, streams (`shell.open`; on
  the Rust worker also `net.open`, `net.udp`, `serial.open`). Example A.
- **A device behind a delegate**: a TV, a KVM, a bridge on the LAN that
  runs no worker. Add an `appliance:` section; `on: worker` caps then get
  the device as `ctx.appliance` (`{name, host, port, settings}`) and the
  worker that reaches it as `ctx.worker`; talk to it with `rook.kit.net`
  (`http`, `Stream`, `WebSocket`). Copy `rook/plugins/cec` (TCP) or
  `rook/plugins/kvm` (HTTP). `appliance.discover` on a nearby worker finds
  it, `appliance.add {name, plugin, worker, host, port, settings}`
  onboards it; then it is called by name like a worker.
- **A record type with a page**: data people and agents keep (a
  storyboard, homework). `records:` gives the caps for free; `pages:`
  shows them. No script. Example B.
- **A job's logic**: a cap `on: hub` whose script does the work (call
  workers, check results, branch, loop). The job itself is one trigger and
  one call to that cap (`job.create`, or a `jobs:` entry in your
  manifest). Never put steps in a job.

## 2. The manifest: fewest keys that work

Top level: `manifest: 1`, `plugin` (the caps' namespace), `version`,
`runs_on: hub`, a one-line `doc`, `segment:` (where in the site its
settings and pages go: `Agents`, `Machines`, `Work`, or a new name; none
is `Hub`). `uses:` lists every cap your script calls
(`ctx.call` refuses any other). `platforms:` (linux, windows, termux, ...)
limits which workers an `on: worker` cap shows up on.

Every cap: `doc` (what it does and returns, for an agent reading it cold),
`on` (`worker` or `hub`), `risk` (`read`, `write`, `exec`, `admin`),
`scope` (`self`, `local`, `multi-machine`, `multi-user`, `external`),
`run: scripts/<file>.py:<function>`. Add `args` (each `{type, required,
doc}`), `result`, `timeout`, `idempotent: true` when it only looks.

Add a section only when you need it: `settings` (read as
`ctx.settings["name"]`, each with a one-line `doc` people read on the
Settings page; `secret: true` for vault values, `type: worker` for a
worker's name), `emits` (events
you send), `hooks` (`start`, `stop`, `every`, `on` an event), `jobs`,
`docs: { manual: docs/manual.md }`. Full key tables:
`docs/manual-0.5/authoring.md`.

## 3. The script and `ctx`

A function, sync or async, gets `ctx` and the cap's args as keywords and
returns JSON-able data, `ctx.file(path)` or `ctx.stream(...)`.
`raise ctx.Error("plain reason")` to fail (`retry=True` if later may work).

- `await ctx.call_on(ctx.worker["id"], cap, **args)`: a cap on the worker
  this cap was called on; `ctx.call(cap, **args)` wherever the hub finds
  it. Every call lands in the journal.
- `ctx.worker` `{id, name, os}`, `ctx.appliance` (devices).
- `ctx.person`, `ctx.agent`, `ctx.session`, `ctx.why`: who asks and why.
- `ctx.settings`, `ctx.data` (the plugin's own folder), `ctx.tmp`,
  `ctx.log`, `ctx.emit(event, payload)`.
- `ctx.records.<type>.create(...)`, `.get(id)`, `.list(where=...)`,
  `.search(query)`, `.update(id, ...)`.

Pass arguments in `argv` slots, never pasted into a shell string. Don't
check for programs first: run the command and report its real failure.

## Example A: a worker's cap (from rook/plugins/battery, Linux only)

`plugin.yaml`:

```yaml
manifest: 1
plugin: battery
version: 0.1.0
doc: Charge level and charging state of a worker's battery.
runs_on: hub
platforms: [linux]
uses: [shell.exec]
caps:
  status:
    doc: "The battery's charge in percent and its state: charging, discharging, fully-charged, ..."
    on: worker
    risk: read
    scope: local
    idempotent: true
    run: scripts/battery.py:status
```

`scripts/battery.py`:

```python
import re

UPOWER = ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]
LINES = re.compile(r"state:\s+(?P<status>[\w-]+)[\s\S]*?percentage:\s+(?P<percent>[\d.]+)%")


async def status(ctx):
    out = await ctx.call_on(ctx.worker["id"], "shell.exec", argv=UPOWER, env={"LANG": "C"}, timeout=8)
    if out["code"] != 0:
        raise ctx.Error(out["stderr"].strip() or f"upower exited {out['code']}")
    m = LINES.search(out["stdout"])
    if m is None:
        raise ctx.Error(f"unreadable battery report: {out['stdout'][:200]}")
    return {"percent": float(m["percent"]), "status": m["status"]}
```

## Example B: a record type with a page (from rook/plugins/projects)

`plugin.yaml`:

```yaml
manifest: 1
plugin: storyboard
version: 0.1.0
doc: Storyboards, one per piece being planned.
runs_on: hub
records:
  storyboard:
    fields:
      title: { type: str, required: true }
      body: { type: markdown, doc: "The frames, in order." }
      state: { type: str, enum: [draft, done], default: draft }
    search: [title, body]
    history: true
pages:
  boards: { title: Storyboards, file: pages/boards.html, caps: ["storyboard.list"], events: ["storyboard.*"] }
```

That alone gives `storyboard.list`, `get`, `search`, `create`, `update`,
`archive` and `history`. A record type named like its plugin drops the
repeat (`storyboard.create`); another type is `<plugin>.<type>.create`.

The page goes in the sidebar under your plugin's `segment:`. It runs
sandboxed with no network: `rook.call` (only the caps the
page declares), `rook.on` (its declared events), `rook.esc`, `rook.ago`,
`rook.open("plugin/page")`, and the site's base style.

`pages/boards.html`:

```html
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Storyboards</title></head><body>
<table><thead><tr><th>Storyboard</th><th>State</th><th>Changed</th></tr></thead>
  <tbody id="rows"><tr><td colspan="3" class="muted">Loading…</td></tr></tbody></table>
<script>
async function load() {
  const boards = await rook.call("storyboard.list", { limit: 200 });
  document.getElementById("rows").innerHTML = boards.length ? boards.map((b) =>
    `<tr><td>${rook.esc(b.title)}</td><td>${rook.esc(b.state)}</td><td class="muted">${rook.ago(b.updated)}</td></tr>`).join("")
    : '<tr><td colspan="3" class="muted">No storyboards yet.</td></tr>';
}
load().catch((x) => { document.getElementById("rows").innerHTML = `<tr><td colspan="3" class="bad">${rook.esc(x.message)}</td></tr>`; });
rook.on("storyboard.*", () => load().catch(() => {}));
</script>
</body></html>
```

## 4. Try it

`python -m rook.kit.manifest validate <folder>` with the hub's Python (it
has `jsonschema`): no errors, and read the warnings. `... diff <old> <new>`
says which version bump a change needs. In a Rook checkout,
`on_worker(plugin, cap, args)` from `tests/plugins/port_helpers.py` runs a
worker's cap as the hub would (see `tests/plugins/test_port_battery.py`).

## 5. Put it on the hub

The hub's plugins live in its library (`library:` in its `hub.yaml`,
relative to that file), one folder per plugin. With a worker on the hub's
machine (the installers put one there):

1. `hub.manifests` gives the folder of a plugin already loaded, so its
   parent is the library. Else find the hub: `shell.exec {cmd:
   "systemctl --user list-units '*hub*'"}`, then `systemctl --user cat
   <unit>`: its `ExecStart` is `<python> -m rook.core2.hub --config
   <hub.yaml>`. `file.read` that config for `library:`.
2. `file.write {path, content, create_parents: true}` each file into
   `<library>/<plugin>/`.
3. `hub.load {plugin}` on `rook`. The hub checks the folder and loads it
   while it runs: its caps, pages, records and jobs, and the caps it gives
   workers, at once. A loaded version is replaced (its hooks stop). A
   folder with errors is refused with the validator's words, and the old
   version runs on; fix and call again. A record type can only gain
   fields: changing a field's type is refused the same way.
4. Bump `version` for every later change, write the files, `hub.load`
   again. Only taking a plugin out of the library needs a hub restart.

## Keep it small

- One way: a manifest and hub scripts. No second format, no wrapper caps
  around caps that exist, no plugin that imports another (call its caps).
- No new settings, options or modes unless the person needs them now.
- No checks, limits, owner-only rules or approvals of your own.
- Write `docs/manual.md`: what each cap does, its settings, its quirks.
