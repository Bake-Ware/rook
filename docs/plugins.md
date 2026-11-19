# Plugins

A plugin is how anything above core reaches Rook: tasks, knowledge, device drivers, and core's own caps too (PL7, K5). It is one folder with a manifest that says what runs at the hub, what runs on workers and what it adds to the site (PL3). Plugins add, never replace (PL1), and none hooks into routing, auth or the journal (PL2).

What is built so far: the manifest, installing from a folder, hot loading, the library a plugin's hub code uses, the worker part for Python workers, pushed to them and hot loaded there, the site section (pages, settings sections, Vue components and shell files), and core's own caps as plugins installed by default.

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
| `name` | string | yes | Lowercase letters and digits, with hyphens between them (`notes`, `lg-tv`). It names the plugin on this hub. Installing a plugin with a name that is already installed replaces it. `core` is taken: it owns the `blob` and `config` record types. |
| `version` | string | yes | The plugin's version, free text (`1.0.0`). Shown by `plugin.list`. |
| `hub` | object | no | What runs at the hub. See below. |
| `worker` | object | no | What runs on workers, by worker form. See below. |
| `site` | object | no | What it adds to the site: pages, settings sections, Vue components. See below. |
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
- `plugin.install {url}` takes the http or https URL of a zip or tar archive (plain, gzip, bzip2 or xz) of a plugin's folder: `plugin.json` at the archive's top, or in its one top-level folder, as a GitHub release asset or branch archive (`…/archive/refs/heads/main.zip`) has it (PL4; brief 61). The hub downloads it off its loop, with no time or size limit, unpacks it into a temporary folder (refusing anything outside it, and links), installs from there as from a folder, and removes the temporary folder. The Settings page takes a URL in the same field as a folder.
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
- **A stopped call** (brief 59; [protocol.md](protocol.md#call_stop-hub-to-worker-optional-brief-59)). Each call runs as a task of its own. When a caller stops it, the worker cancels that task: the handler, or whatever its result is still awaiting, gets `CancelledError`. A handler that starts something meant to outlive its return (a process whose output its result streams) ends it when the call's task ends: `asyncio.current_task()` in the handler is that task, so `asyncio.current_task().add_done_callback(...)` is the place, as `shell.exec` does.
- **A stream with a process behind it** (brief 59). `await stream.signalled()` gives the next signal a client sent, by name (`INT`, `TERM`, `KILL`, `HUP`, `QUIT`), or None once the stream is closed; `await stream.close(exit_code)` closes it saying how the process ended. A stream cap with no process ignores both.
- **A big result** (brief 54; [protocol.md](protocol.md#a-big-result-brief-54)). A plugin author has nothing to do: a result of any size arrives whole, and one over about a megabyte is sent in pieces, which the caller follows on a stream. The handler still holds the value it returns. To hold no more than a piece of it, as the built-in caps do, return any of these, from `rook_worker`, as the result itself or as a field of a result that is an object; the worker takes them as it sends:
  - `Text(pieces)`: a string, from an async iterable of `str`;
  - `Items(items)`: a list, from an async iterable of ordinary JSON values;
  - an awaitable (a task, a future): awaited when the sending reaches it, for what is known last, such as an exit code. In a field, one that gives `LEFT_OUT` leaves the field out.

  A field's place is the order the handler gave them, so put what is known last at the end. A result under about a megabyte is sent as one `result`, the same as an ordinary value, so callers see no difference.
- The worker runs the module's source inside its own process (not sandboxed, PL2), as a module with a name of its own each load, so a replaced version starts fresh.

A plugin's worker caps are called and journaled exactly as any worker cap (PL2). Nothing else is needed yet. The shared helper library (PL6) is where anything that plugin worker code repeats will go.

### `site`: what it adds to the site (built)

```json
"site": {
  "pages":      [{"segment": "Notes", "title": "Notes", "file": "site/notes.js"}],
  "settings":   [{"title": "Notes", "file": "site/settings.js"}],
  "components": [{"name": "note-card", "file": "site/note-card.js"}],
  "shell":      [{"file": "shell/dock.css"}, {"file": "shell/dock.js"}]
}
```

Each part is optional. Every `file` of `pages`, `settings` and `components` is an ES module in the plugin's folder whose default export is a Vue 3 component, as core's pages are: no build step, Vue is the page's global `Vue` (P1).

- `pages` are shown in the navigation under their `segment`, after core's pages, by the same mechanism core's pages use. A segment appears with the first page installed into it (P2). A page's name is its title in lowercase with hyphens between the words (`Notes` is `notes`), and the page is at `/plugins/<plugin>/<page>`.
- `settings` are sections added to the Settings page as tabs after core's Plugins section (P4).
- `components` are registered with the site by `name`, so any page, core's or a plugin's, can use them as `<note-card>`. A name is lowercase letters and digits with at least one hyphen, so it is never an HTML element's name.
- `shell` (house agent brief HA1, as 0.5's manifest `shell:`): files the shell loads on every page once a person is signed in, after the page itself. They load in the manifest's order, each plugin's after the plugins installed before it. A `.css` file loads as a stylesheet. A `.js` file loads as a classic script, not a module, so a plugin's shell files share one scope, as 0.5's did. A shell script:
  - reaches the shell through `window.rookShell`: `api`, `call`, `follow` and `toast` as a page imports them, `store`, `page` (the page this load shows: `id`, `label`, `path`) and `reloadNav()`;
  - hooks in by pushing an `async` function to `window.shellHooks.starting`, run once every shell file has loaded.

  The shell gives every page two places for it: `#work`, a pane at the head of the page area, empty and hidden until a shell part fills it (with the body class `working`, the page itself is hidden while the pane shows); and `#shell-dock`, a column beside the page. The house agent's chat dock (P5) arrives this way, in the built-in `agents` plugin.

The hub serves these files at `/plugin-files/<plugin>/<file>` to a logged-in session only, and serves no file the manifest does not name. They are read from the installed plugin on every page load, so installing or replacing a plugin shows on the next page load, with no restart (PL3).

What the hub refuses at install, saying what to fix (an install is all or nothing):

- an entry without its fields, or a `file` that is missing or leaves the plugin's folder (`bad_manifest`);
- a page, settings section or component name used twice in the plugin, or a component name without a hyphen (`bad_manifest`). A settings section's name is made from its title as a page's is;
- a shell file named twice, or one that ends neither in `.css` nor in `.js` (`bad_manifest`);
- a page name that core or another plugin already has (`page_taken`);
- a component name that core or another plugin already has (`component_taken`). Core's are `side-bar`, `login-view`, `pin-view`, `row-menu` and `toast-line`.

A page file looks like this. It calls a cap with the site's `call(worker, cap, args, why)`: the call goes out as the signed-in person, through `POST /api/call`, the same as any page's, and is journaled under them with the `why` (L1). It returns the cap's result, or throws an error that says what to try next. A call still running after 45 seconds returns `{running: true, id, worker, stream, follow}` instead and goes on by itself; `await answer.follow()` gives its result, or throws its error, when it ends (Bake 13 Nov). A plugin's caps at the hub are called with worker `"hub"`.

```js
import { call, viewHref } from '/site/shell.js';

export default {
  data() { return { notes: null, error: '' }; },
  computed: { href() { return viewHref(location.pathname, {}); } },
  async mounted() {
    try { this.notes = (await call('hub', 'note.search', {}, 'Listed on the Notes page.')).notes; }
    catch (e) { this.error = e.message; }
  },
  template: `<div>
    <header class="topbar"><div><div class="title-row"><h1>Notes</h1>
      <pin-view label="Notes" :href="href"></pin-view></div></div></header>
    <div class="wrap">
      <p v-if="error" class="callout">{{ error }}</p>
      <note-card v-for="n in notes || []" :key="n.note" :note="n"></note-card>
    </div></div>`,
};
```

`/site/shell.js` also gives a page `api` (any of the site's requests), `toast`, `viewHref` and `showView` (a view kept in the address, P6). A pin on a plugin's page names the plugin and the page, so it survives a reinstall and never leads to another plugin's page. If a new version no longer has the page, the pin says it is gone, and so does the page area when it is opened. A page that fails to load says which plugin's page failed, and the rest of the site works. A plugin's page runs in the site as core's pages do: there is no sandbox, and a plugin hooks into nothing in routing, auth or the journal (PL2).

## Core's own caps: plugins installed by default (PL7)

Core's own caps ship as plugins in the one format above, in `hub/rook_hub/defaults/`. Each has a worker part for Python workers and nothing at the hub or on the site, except `agents`, which is the house agent and has a site part (and, with brief HA2, a hub part).

| Plugin | Caps | From |
|---|---|---|
| `agents` | No caps yet (brief HA2 adds the house agent's). Its site part is the chat dock beside every page, ported from 0.5 (`cbe86a47:rook/plugins/agents/shell/`): the conversation with the house agent through `agent.chat.send` (`wait: false`) and `agent.chat.read`, Rookie (`rookie.js`, vendored unchanged; `rookie-map.js`), the work pane, the chat markup (`chat-widgets.js`: `[[choices: ...]]`, `[[open: ...]]`, `[[point: ...]]`, `[[say: ...]]`) and "Getting ready" while no engine answers. A hub where `agent.site` cannot be called shows no dock. | house agent briefs HA1 and HA2; P5 |
| `net` | `net.http`, `net.tcp`, `net.udp`: the network primitives every worker serves as a jump box for devices (D3). The functions are `rook_net`'s, which the hub also runs for a device on a direct route (D4). | D3 |
| `shell` | `shell.exec` (run a command and wait for it: `argv`, or `cmd` in `sh`, `bash` or `powershell`; gives `ok`, `code`, `stdout`, `stderr`, and `shell` when it was not `sh`), `shell.which` (a program's path on PATH, or null), `shell.env` (one variable by `name`, its value or `default`, or every variable whose name starts with `prefix`, as an object; values as they are), `shell.open` (a command, a login shell unless given, as a stream: on a terminal of `cols` by `rows`, resized by `stream_resize`, or on pipes with `pty` false; it closes when the command ends, with its exit code, and closing it ends the command; a signal sent to the stream goes to the command and everything it started). Stopping a `shell.exec` call ends its command the same way. Every command starts with INT, TERM, HUP and QUIT at their defaults, whatever the worker itself ignores (brief 65). | docs/inventory.md CAP-01, CAP-02 (0.5's), CAP-03, WRK4-070 |
| `file` | `file.read` (`path`, `encoding`: a codec or `base64`, `max_bytes`; gives `content`, `size`, `encoding`, and `truncated` when `max_bytes` cut it; anything that opens, a FIFO or a device too, read for as long as it takes), `file.write` (`path`, `content`, `encoding`, `append`, `create_parents`; gives `path`, `written`), `file.list` (`path`, `recursive`, `hidden`, `limit`; gives `entries` of `name`, `type`, `size`, `modified`, `mode`, and `path` when recursive), `file.search` (`pattern`, a regex, in the files under `path`, with `recursive`, `glob`, `ignore_case`, `limit`; gives `matches` of `path`, `line`, `text`; binary files skipped; the pattern runs in a child Python, so a slow one never stops the worker), `file.stat` (`path`; gives `exists`, and `type`, `size`, `modified`, `mode`). | docs/inventory.md CAP-04 (0.5's) |
| `worker` | `worker.ping`: no arguments; gives `pong: true` and `at`, the worker's clock. | docs/inventory.md CAP-05 (0.5's) |
| `battery` | `battery.status`: no arguments; gives `percent` and `status`, from upower or else `/sys/class/power_supply/BAT*`, on Linux. A machine with no battery answers so, with what to try. | docs/inventory.md CAP-07 (0.5's), H4 |

None of these caps has a timeout, a size cap or a list of what may run (Build rules; Bake, 14 and 17 November). `max_bytes` and `limit` are there for a caller who wants to bound a call, and have no default: with nothing passed, a cap gives everything. A call with an argument the cap does not take is refused, saying which it takes. The shell caps are built for Linux and macOS (the same POSIX calls; only Linux has been run); on Windows each but `shell.env` answers that it is not built there yet (W5). `battery.status` is built for Linux only. The file caps use the standard library only; only Linux has been run.

What a hub does at start, every start (Bake, 17 November: "How else are you going to load new plugins?"). It first loads every installed plugin, then goes through the folders in `defaults/` in name order (`battery`, `file`, `net`, `shell`, `worker`):

- **Not installed** (no plugin of that name in the data directory: a new data directory, or one made by a core that did not ship it): it installs it, as any plugin is installed.
- **Installed at an older version** than the one shipped: it installs the shipped one in its place, as an install of the same name replaces a plugin. Versions are compared as dotted numbers, number by number, a missing place counting as 0 (`0.1` is `0.1.0`). A version that is not dotted numbers is never counted older, so it is left.
- **Installed at the same or a newer version**: it leaves it. That covers a plugin a person installed under one of those names at a version at least the shipped one: the person's plugin stays. A person's plugin under one of those names at an older version is replaced, as the rule says.
- **One that fails to install** (for example a cap it adds is already served by a plugin a person installed): the hub logs that it was not installed and why, keeps what was installed before, and carries on.

These installs are not journaled: no person or agent made them. The hub keeps no other note of the defaults: it compares the shipped version with the installed one each time, so a second start with nothing new installs nothing. Rook has no uninstall, so a default plugin is removed only by hand: one whose folder was removed from the data directory is still installed as far as the hub knows, so a start logs that it could not load it and carries on, until a core ships a newer version of it, which is then installed.

Workers get the worker parts as they get any plugin's, so a worker opted out of plugins (P4) serves none of these caps, and a worker's knock offers only its own caps: the default plugins arrive after approval. A worker linked to the hub when it stopped needs no restart of its own: it links again when the hub is back, and is sent the new and updated worker parts as on any install.

### `skill` (kept, not served)

`skill` names a file in the folder (PL5). The hub keeps it and `plugin.list` reports it. How it reaches an agent is not decided yet.

## Not built, and open

- Uninstalling, disabling or enabling a plugin, and what happens to its records and record types.
- A plugin's hub code calling caps on workers (other than a driver's three network steps).
- Serving a plugin's skill to agents (PL5).
- Pushing worker parts to forms other than `python`, such as `esp32` (PL3).
- The files and health families of PL7, and the shell caps 0.4 and 0.5 do not agree on: see the pull request for brief 39.
- The registry (PL4: LATER).
