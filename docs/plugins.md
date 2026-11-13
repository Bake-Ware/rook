# Plugins

A plugin is how anything above core reaches Rook: tasks, knowledge, device drivers, and later shell and files too (PL7, K5). It is one folder with a manifest that says what runs at the hub, what runs on workers and what it adds to the site (PL3). Plugins add, never replace (PL1), and none hooks into routing, auth or the journal (PL2).

What is built so far: the manifest, installing from a folder, hot loading, the library a plugin's hub code uses, and the worker part for Python workers, pushed to them and hot loaded there. The site section is written down below as a proposal. The hub keeps it and reports that it is there, and does nothing else with it yet.

## The folder

```
notes/
  plugin.json     the manifest
  hub.py          the hub module (if the plugin runs at the hub)
  worker.py       the worker module for Python workers (if it runs on workers)
  SKILL.md        a skill (optional, PL5)
  ...             anything else the plugin ships
```

## The manifest: `plugin.json`

A JSON object (JSON, because the standard library reads it).

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `name` | string | yes | Lowercase letters and digits, with hyphens between them (`notes`, `lg-tv`). It names the plugin on this hub. Installing a plugin with a name that is already installed replaces it. `core` is taken: it owns the `blob` record type. |
| `version` | string | yes | The plugin's version, free text (`1.0.0`). Shown by `plugin.list`. |
| `hub` | object | no | What runs at the hub. See below. |
| `worker` | object | no | What runs on workers, by worker form. See below. |
| `site` | object | no | What it adds to the site. **Proposal**, see below. |
| `skill` | string | no | A file in the folder that teaches an agent how to set up and use the plugin (PL5). |

The hub refuses a manifest that is missing, is not JSON, or breaks any rule above, and the error says what to fix. Fields it does not know are kept and ignored.

### `hub`: what runs at the hub (built)

```json
"hub": {"module": "hub.py"}
```

`module` names one Python file in the folder. The hub imports it inside its own process (plugin code is not sandboxed) and calls its `setup(plugin)`. Each load imports the file under a module name of its own, so one plugin cannot disturb another, and a reinstall gets fresh code.

```python
from rook_hub.library import Error        # optional: refuse a call, saying what to try next


def setup(plugin):
    plugin.record_type("note")             # this plugin owns record type "note" (K2)

    @plugin.cap("note.create", "Make a note with a title and some text.", {
        "type": "object", "required": ["title", "text"],
        "properties": {"title": {"type": "string"}, "text": {"type": "string"}}})
    def create(call, args):
        note = call.create("note", args["title"], body={"text": args["text"]})
        call.emit("note.created", {"note": note["guid"]})
        return {"note": note["moniker"]}
```

In `setup(plugin)` a plugin declares:

- **caps** with `@plugin.cap(name, description, schema)` on a function `handler(call, args)`. The name follows W3 (`note.create`). The description is one line and the schema is a JSON Schema object. The hub serves the cap like its own caps, so agents call it with worker `"hub"` and every call is journaled (L1). The handler returns any JSON value.
- **record types** with `plugin.record_type(name)`. The plugin owns them (K2).

Each handler gets a `call`, the plugin library (PL6), which is the plugin's only way to records:

| Method | What it does |
|---|---|
| `call.user`, `call.token` | Who is calling: the user's GUID, and the token's GUID when an agent called. |
| `call.create(type, title, description="", body=None, owner=None)` | A record of a type the plugin owns. The caller is its creator (K1). |
| `call.get(record)`, `call.update(record, title=, description=, body=)`, `call.delete(record)` | Read, change and delete a record by GUID or moniker. |
| `call.search(type, text=None, limit=50)` | Records of one type the plugin owns (or `blob`), optionally holding every word of `text`. |
| `call.link(a, b)`, `call.unlink(a, b)`, `call.links(record)` | Links between records (K3). `links` shows only records the plugin may reach. |
| `call.store_blob(data, mime, title="", description="")`, `call.read_blob(record)` | Blobs (K3). |
| `call.emit(name, payload)` | Put an event on the bus, as the caller (PL1, J2). |

Through `call` a plugin reaches only the records of the types it owns, plus blobs. Anything else is refused with `type_not_owned`. To refuse a call itself, a handler raises `Error(code, message, next_step)`. Any other exception becomes an error reply naming the plugin (`plugin_failed`), and the hub carries on (C3).

What the hub refuses at install, keeping nothing of the plugin (an install is all or nothing):

- hub code that fails to import, has no `setup`, or raises in `setup` (`plugin_load_failed`, `bad_plugin`);
- a cap whose name, description or schema breaks W3 (`bad_plugin`);
- a cap name the hub or another plugin already serves (`cap_taken`);
- a record type that core or another plugin already owns (`type_taken`).

A plugin that is installed again keeps its own cap names and record types, and its GUID and moniker (G1).

### Drivers: devices at the hub (built)

A device is anything with an API that cannot run a worker: a TV, a lamp, a printer (D1). Its driver is something a plugin adds (PL1, D2). In `setup(plugin)`:

```python
import json


def setup(plugin):
    lamp = plugin.driver("lamp")                   # a driver name: lowercase, hyphens between words

    @lamp.cap("lamp.on", "Turn the lamp on.", {"type": "object"})
    async def on(device, args):
        answer = await device.http(device.config["url"] + "/on", method="POST",
                                   headers={"x-lamp-key": device.config["key"]}, wait=5)
        return json.loads(answer["body"])

    @lamp.status                                   # optional: the hub polls it (D6)
    async def status(device):
        return json.loads((await device.http(device.config["url"] + "/state", wait=5))["body"])
```

A driver's handlers are `async`, because each network step goes to a worker and back. A handler gets a `device`:

| | |
|---|---|
| `device.config` | The device's config, kept at the hub (D7). A value that is exactly `secret:<name>` arrives resolved: the device's own scope first, then its band's, then the hub's (X1, X2). |
| `device.guid`, `device.moniker`, `device.name` | Which device. |
| `await device.http(url, method="GET", headers=None, body=None, wait=None)` | One HTTP request: `{status, headers, body}` (or `body_base64` for bytes that are not text). |
| `await device.tcp(host, port, data, wait)` | One TCP send; what came back within `wait` seconds: `{data}` or `{data_base64}`. |
| `await device.udp(host, port, data, wait)` | One UDP send; the first reply within `wait` seconds, or `{"data": null}`. |

These are the driver's only way to the network, and it never names a worker. The hub carries each step out by the device's route (D4): itself (direct), through one worker (pinned), or through any eligible worker on the device's band (floating, D5). Every worker serves the steps as the caps `net.http`, `net.tcp` and `net.udp` (D3). An agent adds a device with the hub cap `device.add`, naming the driver. A driver name that another plugin already declares is refused (`driver_taken`); so is a handler or status function that is not `async` (`bad_plugin`).

### Installing (built)

Agents install through MCP with the hub caps (PL4):

- `plugin.install {path}` takes a plugin's folder on the hub's disk. The hub checks the manifest, copies the folder to `<state dir>/plugins/` and loads it at once, with no restart. A plugin of the same name is replaced at once, and its records stay.
- `plugin.list` shows each plugin: name, GUID, moniker, version, the caps and record types it added, whether it has a worker section, a site section and a skill, and the workers that loaded its worker part (`loaded_on`) or failed to (`failed_on`, with the error).

When the hub starts it loads every installed plugin. One that fails to load is logged (logger `rook_hub.plugins`) and skipped, and the hub carries on. A skipped plugin's worker part is not pushed to workers either.

### `worker`: what runs on workers (built for Python workers)

PL3 says the hub pushes worker parts "to the right worker forms", and U1 says cores are written per language. The section is keyed by the worker form that a heartbeat reports in `facts.form` (Bake 13 Nov):

```json
"worker": {
  "python": {"module": "worker.py"},
  "esp32":  {"image": "esp32/plugin.bin"}
}
```

- Each key is a worker form. For `python` the value names one Python file in the plugin's folder. Install refuses, saying what to fix, a `module` that is missing, is not a file, leaves the folder (`../x.py`, or a link to outside it) or is not UTF-8 text (`bad_manifest`). Whether the code loads is not checked at install: a worker loads it and reports how it went.
- Other forms (`esp32` above) are kept as written and reported, and nothing is pushed to them yet. Their checks come with the slice that pushes to them.
- A worker whose form has no entry gets nothing from that plugin, and the plugin's hub part still works.

**Delivery.** The hub sends a Python worker one `plugins` message ([protocol.md](protocol.md#plugins-hub-to-worker-optional-pl3)) that lists every worker part it should have now: each plugin's name, version and module source, in install order. It sends it when the worker connects (unless the worker holds none and should have none) and again, to each worker whose list changed, when a plugin is installed or replaced or a worker's opt-out changes. The message does not depend on how it is carried.

**On the worker.** The worker makes its state match the list, with no restart (PL3):

- a part that is no longer listed, or whose version or source changed, is dropped: its caps are no longer served or announced, a call to one fails with a reason (W5), and every stream they opened is closed;
- a part that is new or changed is loaded; one that failed last time is tried again;
- then it heartbeats, so the caps appear and disappear with the plugin (W5).

A part that fails to load (it raises, calls `sys.exit()`, has no `CAPS`, or declares a bad cap) adds nothing, and the worker keeps running with its own caps and its other plugins. It reports each part in its heartbeat's `plugins`, with an `error` for one that failed. The hub journals a failure (kind `plugin`, under the worker) and `plugin.list` shows each plugin's `loaded_on` and `failed_on` workers.

**One namespace.** A worker's own caps and its plugins' caps share one set of names. A part that declares a cap the worker already serves, its own or another plugin's, fails to load as a whole, with the error `<cap> is already served by this worker` (or `by plugin <name>`). Nothing is replaced (PL1). Parts load in install order, so the plugin installed first keeps the name.

**The opt-out (P4).** A worker can be opted out of plugins at the hub, as it can be opted out of serving devices (W4). The hub cap `worker.plugins {worker, receive}` sets it, and so does the "Receives plugins" switch on the worker's row on the Workers page. Both are journaled under who did it (L1). An opted-out worker is sent an empty list, so it drops every part at once; opting it back in sends it the list again.

#### The worker module

A worker module declares its caps the way the worker core declares its own: a module-level dict `CAPS` of cap name to `Cap`, the class in `rook_worker`.

```python
import asyncio

from rook_worker import Cap


async def hello(args):                       # a cap that returns a result
    return {"greeting": f"Hello, {args.get('name', 'you')}."}


async def echo(args, stream):                # a cap that opens a stream (S1)
    async def answer():
        while (data := await stream.read()) is not None:     # input, as bytes; None once closed
            await stream.send(b"heard: " + data)
    asyncio.create_task(answer())
    return {"title": args.get("title", "echo"), "description": "Echoes what is written to it."}   # S3


CAPS = {
    "greet.hello": Cap("Say hello.", {"type": "object", "properties": {"name": {"type": "string"}}}, hello),
    "greet.echo": Cap("Open a stream that echoes input.", {"type": "object"}, echo, opens_stream=True),
}
```

- `Cap(description, schema, handler, opens_stream=False)`: a one-line description, a JSON Schema object for the arguments and an `async` handler (W3). The name follows W3.
- A plain cap's handler is `handler(args)` and returns any JSON value. A stream cap's handler is `handler(args, stream)` and returns `{"title", "description"}`. It keeps `stream` to `send` output, `read` input and `close` it (S1 to S4).
- A handler that raises, or calls `sys.exit()`, fails that call with the error `cap_failed`, as for the worker's own caps; the worker keeps running. Ctrl-C and cancellation still stop the worker.
- The worker runs the module's source inside its own process (not sandboxed, PL2), as a module with a name of its own each load, so a replaced version starts fresh.

A plugin's worker caps are called and journaled exactly as any worker cap (PL2). Nothing else is needed yet. The shared helper library (PL6) is where anything that plugin worker code repeats will go.

### `site`: what it adds to the site (proposal, not built)

PL1 lists pages, settings sections and Vue components; P1 says plugin UI and core UI are the same mechanism; P2 says a segment appears in the nav only when something installs a page into it. The proposal:

```json
"site": {
  "pages":      [{"segment": "Notes", "title": "Notes", "file": "site/notes.js"}],
  "settings":   [{"title": "Notes", "file": "site/settings.js"}],
  "components": [{"name": "note-card", "file": "site/note-card.js"}]
}
```

- `pages`: each is a Vue 3 component file (no build step, P1) shown under its `segment` in the nav.
- `settings`: sections added to the Settings page (P4).
- `components`: Vue components other pages may use (PL1; the Agents chat pane, P5, arrives this way).

Open: how a page calls the plugin's caps from the browser, and how pins (P6) name a plugin's page.

### `skill` (kept, not served)

`skill` names a file in the folder (PL5). The hub keeps it and `plugin.list` reports it. How it reaches an agent is not decided yet.

## Not built, and open

- Install from a URL (PL4): a URL to what is not decided.
- Uninstalling, disabling or enabling a plugin, and what happens to its records and record types.
- A plugin's hub code calling caps on workers (other than a driver's three network steps).
- Serving a plugin's skill to agents (PL5).
- Pushing worker parts to forms other than `python`, such as `esp32` (PL3), and site parts (PL1, P1).
- Core's own caps (shell, files, delegate primitives, health) as plugins installed by default (PL7).
- The registry (PL4: LATER).
