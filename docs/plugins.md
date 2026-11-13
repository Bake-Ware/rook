# Plugins

A plugin is how anything above core reaches Rook: tasks, knowledge, device drivers, and later shell and files too (PL7, K5). It is one folder with a manifest that says what runs at the hub, what runs on workers and what it adds to the site (PL3). Plugins add, never replace (PL1), and none hooks into routing, auth or the journal (PL2).

What is built so far is the hub side: the manifest, installing from a folder, hot loading, and the library a plugin's hub code uses. The worker and site sections are written down below as proposals. The hub keeps them and reports that they are there, and does nothing else with them yet.

## The folder

```
notes/
  plugin.json     the manifest
  hub.py          the hub module (if the plugin runs at the hub)
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
| `worker` | object | no | What runs on workers. **Proposal**, see below. |
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

### Installing (built)

Agents install through MCP with the hub caps (PL4):

- `plugin.install {path}` takes a plugin's folder on the hub's disk. The hub checks the manifest, copies the folder to `<state dir>/plugins/` and loads it at once, with no restart. A plugin of the same name is replaced at once, and its records stay.
- `plugin.list` shows each plugin: name, GUID, moniker, version, the caps and record types it added, and whether it has a worker section, a site section and a skill.

When the hub starts it loads every installed plugin. One that fails to load is logged (logger `rook_hub.plugins`) and skipped, and the hub carries on.

### `worker`: what runs on workers (proposal, not built)

PL3 says the hub pushes worker parts "to the right worker forms", and U1 says cores are written per language. The proposal is to key the section by the worker form that a heartbeat reports in `facts.form`:

```json
"worker": {
  "python": {"module": "worker.py"},
  "esp32":  {"image": "esp32/plugin.bin"}
}
```

- Each key is a worker form. Its value names what the hub pushes to workers of that form: for a Python core, a module whose caps the worker then announces like its own (W5); for a form that cannot load code, whatever that form's core can take.
- A worker whose form has no entry gets nothing, and the plugin's hub part still works.
- The per-worker opt-out on the Workers row (P4) decides which workers receive it.

Open: how the hub delivers it (a new protocol message, or the update path of U2), and how a worker part is removed or replaced.

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
- A plugin's hub code calling caps on workers.
- Serving a plugin's skill to agents (PL5).
- Pushing worker parts (PL3) and site parts (PL1, P1).
- The registry (PL4: LATER).
