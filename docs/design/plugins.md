# Plugins: the contract between Rook core and everything else

Status: wave 2 of the beta refactor. The core plugin host, node facts, the hub
node (`rook`) and generated MCP tools are implemented (`rook/core`,
`rook/hub`). Knowledge and tasks run on this API as hub plugins (section 15). Signed role
grants and permissions enforcement are specified in
[permissions.md](permissions.md); the settings UI and storage are specified in
[settings.md](settings.md). This document owns the plugin contract and says
where it hands off to those two. The persona plugin (a hub part and a worker
part sharing the namespace `persona`) is specified in [persona.md](persona.md).
The home agent (hub plugin `home`, the hub's own LLM) is in
[home-agent.md](home-agent.md).

## 1. Goals

- **One plugin API for the hub and workers.** A plugin doesn't know whether it
  runs on the hub or on a worker. It declares *where* it may run (placement)
  and the host on each node decides.
- **Caps are the single interface.** Everything a plugin offers is a
  capability (`namespace.action`) callable on the band. MCP tools are generated
  from caps, not written by hand.
- **Core stays small and portable.** The core is what every node needs to be on
  a band. Everything with a domain (knowledge, tasks, memory, voice, chat
  integrations) is a plugin.
- **Existing worker plugins keep working unchanged.** The original worker API
  (`Plugin`, `NAMESPACE`, `@capability`, `available()`, `heartbeat()`,
  `start()`/`stop()`, `bind_worker`) is a strict subset of this contract.
- **Old peers keep working.** Every wire addition is an optional announce key
  that build-167 peers ignore.

## 2. Core versus plugin

| Core (always present) | Plugin (optional, placed) |
|---|---|
| Band transport (Telesthete hub relay, WS bridge) | Knowledge wiki, tasks/projects/concepts (hub plugins `knowledge`, `task`) |
| Worker registry, announces, cap routing | Memory, persona/guidance content, work sessions |
| Identity and tokens, attribution | Voice, decision, Telegram/Discord bridges |
| Call journal | Hardware integrations (camera, HID, CEC, KVM, battery, ...) |
| Persistent chat rooms and presence | Shell/process/file caps on workers |
| Vault (secrets) | `hub.*` introspection (the reference hub plugin) |
| Thin MCP bridge (`rook_call`, rosters, tool generation) | |
| Plugin host, node facts, capability registry | |

Package layout:

| Package | Role | Ships in the worker bundle |
|---|---|---|
| `rook.core` | registry, plugin contract, plugin host, facts, migrations, call context. **Stdlib only.** | yes (`band-worker.pyz` and the Android app copy it) |
| `rook.worker` | worker node: transport, `Worker`, admin caps, built-in worker plugins (`rook.worker.plugins`) | yes |
| `rook.hub` | hub node (`HubNode`, worker `rook`), MCP tool generation, built-in hub plugins (`rook.hub.plugins`) | no |
| `rook.band_mcp` | MCP bridge, hub stores (journal, chat, vault, tokens, guidance) | no |

`rook.worker.plugin` and `rook.worker.registry` / `rook.worker.context` remain
as re-export shims, so `from ..plugin import Plugin, capability` in every
existing worker plugin resolves to the core classes.

In wave 1 chat rooms, the vault, the journal and guidance are still wired
directly into `rook/band_mcp/server.py` as core services. They become hub caps
(Appendix A.2 of permissions.md) as their MCP tools are regenerated from caps.

## 3. The manifest

A plugin is a `Plugin` subclass exported from its module as `PLUGIN` (a class
or an instance; `PLUGIN = None` opts out on this host). The manifest is class
attributes. Only `NAMESPACE` is required.

| Attribute | Default | Meaning |
|---|---|---|
| `NAMESPACE` | required | Cap prefix. Caps are `NAMESPACE.suffix` (or just `NAMESPACE`). |
| `NAME` | module stem | Human/plugin id in listings. |
| `VERSION` | host build | `<build>.<adjective>.<noun>`, the worker scheme (`167.snappy.quail`). Built-in plugins inherit their host's build; third-party plugins set their own. A malformed version is a warning, not a failure. |
| `CORE_API` | `">=1.0,<2"` | Range of `rook.core.plugin.CORE_API_VERSION` the plugin supports. Comma-separated `>= <= == != > <` clauses, or a bare major (`"1"` means `>=1.0,<2`). Incompatible: the plugin does not load (`failed`). |
| `PLACEMENT` | `place("not is_hub")` | Where it runs (section 4). |
| `SETTINGS` | `()` | `setting(...)` / `resource(...)` entries (sections 6, 7). |
| `MIGRATIONS` | `None` | Directory of `NNN_name.sql` files beside the module (section 8). |
| `GUIDANCE` | `{}` | Guidance slots: `{slot: default text}`. Operators edit them; agents see them as cap tips (section 8). |
| `SKILL` | `""` | Markdown fragment merged into the agent skill reference (section 8). |
| `PANEL` | `None` | Optional web panel `{"title", "path"}` (section 8). |
| `DEPENDS` | `()` | Namespaces of plugins that must be loaded on the same node first (core API 1.1). The host tries the plugin after the others; if a dependency never loads it is `unavailable`. `self.dependency(ns)` returns the loaded instance. |

`Plugin.manifest()` returns the manifest as a dict (name, namespace, version,
core_api, placement, caps, settings schema, migrations, guidance slots, skill
flag, panel, depends). The host adds `source` (`package:<pkg>` or `entry_point:<name>`)
and the load `state`.

```python
from rook.core.plugin import Plugin, capability, place, setting, resource

class Notes(Plugin):
    NAMESPACE = "notes"
    VERSION = "12.brisk.otter"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("max_len", int, default=4000, scope="band", env="ROOK_NOTES_MAX",
                label="Longest note (chars)"),
        setting("api_token", str, secret=True, label="Sync token"),
        resource("embedder", default="cap://any/embed.text", label="Embedding service"),
    )
    MIGRATIONS = "migrations"
    GUIDANCE = {"notes.add": "Keep notes factual; link evidence."}
    SKILL = "### notes\n`notes.add(text)` stores a note; `notes.search(q)` finds them.\n"

    async def start(self):
        import sqlite3
        self.db = sqlite3.connect(self.data_dir / "notes.db", check_same_thread=False)
        self.migrate(self.db)

    @capability("search", risk="read", limit=20, fields=["id", "title"], tool=True)
    def search(self, q: str) -> list[dict]:
        """Search notes by text."""
        ...

    @capability("add", risk="write")
    def add(self, text: str) -> dict:
        """Store a note."""
        ...

PLUGIN = Notes
```

## 4. Placement and node facts

### 4.1 Facts

Every node describes itself with two kinds of fact (permissions.md 4.8):

- **Roles** come only from grants signed by the root key (permissions.md
  section 4). v1 defines `is_hub`. A node cannot claim a role by announcing
  it. Grants ride in the announce as `grants`; `rook.core.facts.verify_role_grant`
  checks them. **Wave 1 ships a stub verifier that rejects every remote
  grant** (`set_role_verifier()` is the hook the permissions implementation
  fills). The hub's own node holds `is_hub` as local authority: it holds the key.
- **Hardware/platform facts** are self-reported and gate placement only. They
  never grant anything. Detected once at start-up (`rook.core.facts.detect_facts`):

  | Fact | Type | Source |
  |---|---|---|
  | `os` | `linux`, `windows`, `darwin`, `android` | `platform.system()`, Android env |
  | `arch` | `x86_64`, `aarch64`, ... | `platform.machine()` |
  | `py` | `"3.12"` | interpreter |
  | `cpus`, `mem_gb` | numbers | `os.cpu_count()`, `/proc/meminfo` |
  | `pty` | bool | POSIX |
  | `display` | bool | `DISPLAY`/`WAYLAND_DISPLAY` (Linux); true on Windows/macOS |
  | `camera` | bool | `/dev/video*` (Linux) |
  | `gpu` | `[{vendor, name, vram_gb}]` | `nvidia-smi` (one bounded call) |
  | `embedded` | bool | Android, a device tree (SBCs), or under 2 GB RAM |

  Operators add or correct facts with `ROOK_NODE_FACTS` (a JSON object merged
  over the detected ones, for example `{"camera": true, "gpu": [{"vendor": "amd", "vram_gb": 16}]}`).
  Any key that could pass for a role (`is_*`, `role`, `roles`, `grants`) is
  dropped, both when detecting and when parsing someone else's announce.

On the wire the worker announce gains `"facts": {...}` (false booleans
omitted, capped at 1 KB). Build-167 workers don't send it and receivers treat
it as `{}`: such a worker matches only placements that don't depend on facts.

### 4.2 Placement

```python
place(where=<expression | callable | None>, run="all" | "one")
```

`where` is a predicate over the node's facts. Expressions use a small,
safely parsed subset of Python (no `eval`; attribute access, subscripts,
arbitrary calls and comprehensions are rejected at declaration time):

| Form | Meaning |
|---|---|
| `is_hub` / `is_<role>` | the node holds that signed role |
| `is_embedded` | the `embedded` fact |
| `has('camera')` | the fact is truthy (for a list fact, any item) |
| `has('gpu', vram_gb >= 8)` | some item of the list fact satisfies every condition; names inside refer to the item's keys |
| `has('gpu', vendor='nvidia')` | keyword = exact match on an item key |
| `os == 'android'`, `arch in ('x86_64', 'aarch64')`, `mem_gb >= 4` | compare plain facts |
| `and`, `or`, `not`, parentheses | combine |
| `any` | everywhere |

A callable `where(facts: NodeFacts) -> bool` is also accepted (in-process
plugins only; it can't be shown in a manifest). An error while evaluating
counts as "does not match".

**Default placement is `not is_hub`**: workers only. Every existing worker
plugin therefore keeps loading on every worker (where `available()` agrees)
and none of them lands on the hub. Hub plugins say `place("is_hub")`.

`run`:

- `"all"`: every matching node runs it.
- `"one"`: exactly one matching node per band runs it (a singleton service).
  The hub decides, because it sees the roster. **Wave 1:** the hub host
  elects itself for its own `run="one"` plugins (one hub per band); a worker
  host has no elector yet and declines `run="one"` plugins (`not_placed`).
  Hub-driven election of a worker (announce a candidate, hub replies with the
  winner, failover when it goes stale) is a wave-2 item.

Placement decides where a plugin *loads*. `available()` then decides whether it
can *function* there (a backend, a config value). Both must pass.

## 5. Capabilities

```python
@capability(suffix="", *, risk=None, tags=(), limit=None, fields=None,
            tool=False, description=None)
```

A bare `@capability("x")` behaves exactly as before. The keyword metadata
(`CapMeta`) is additive:

| Key | Meaning | Enforced by |
|---|---|---|
| `risk` (alias `tier`) | `read` < `write` < `exec` < `admin`, defined in permissions.md section 2. Undeclared = `exec`. | permissions layer (wave 2); the hub node's band ceiling (section 10) today |
| `tags` | `sensitive`, `destructive`, `physical` (permissions.md 2.1) | policy selectors |
| `limit` | Default page size. If the handler takes a `limit` parameter the default is injected when the caller omits it; otherwise core trims list results (a top-level list, or the `items` list of an envelope dict, adding `truncated`/`total`) to the caller's `limit` or the default. | `CapabilityRegistry.call` |
| `fields` | `None`: no projection. `"*"`: projection supported, everything by default. `[...]`: the default keys. The caller passes `fields=[...]`, `"a,b"` or `"*"`. Unless the handler takes `fields` itself, core strips it and projects: a dict, each dict of a list, or each item of an envelope's `items`. | `CapabilityRegistry.call` |
| `tool` | Also expose as a dedicated MCP tool (hub-placed caps; section 10.3) | hub MCP bridge |
| `description` | Replaces the docstring's first paragraph in tools and rosters | describe / tool generation |

Enforcement lives in the one registry both node types use, so the same
contract holds on the hub and on every new worker. Output byte caps are part
of the token-envelope work and will hook in at the same point.

`caps.describe` returns, per cap, the docstring, parameters and, when
declared, `risk`, `tags`, `limit`, `fields`, `tool`. Announces carry a compact
`"tiers": {"cap": "r|w|x|a"}` map of *declared* tiers only (permissions.md
2.2), omitted when there are none; build-167 peers ignore it.

The dispatch context (`rook.core.context.current_identity()`) gives a handler
the caller identity from the envelope, on both node types.

## 6. Resources: connection strings

Heavy plugins scaffold on the hub and reach their dependencies through
operator-set connection strings rather than hard-wired hosts:

```python
resource("embedder", default="cap://any/embed.text", label="Embedding service")
...
res = self.resource("embedder")          # Resource(scheme, target, path)
vec = await res.call({"text": "hello"})  # cap:// only
```

| Scheme | Form | Notes |
|---|---|---|
| `cap` | `cap://<worker name or id \| any>/<cap>` | Called over the band by the host. `any`: the first live holder by name (smarter picking comes with placement-aware routing). Calls carry the identity `system:rook-hub`. |
| `http`, `https` | `https://host:port/path` | The plugin uses its own client. |
| `sqlite` | `sqlite:///abs/path.db` | |
| `file` | `file:///abs/path` | |

A resource is a setting of type `resource`: it has a scope, an env override and
a default, and is validated on parse.

## 7. Settings

One schema per plugin drives validation, env overrides, the settings UI and
its history (settings.md specifies the UI, storage and history):

```python
setting(name, type=str, default=None, *, scope="hub", secret=False,
        env=None, label="", help="", choices=(),
        apply="live", bootstrap=False, overridable=(), flag=None,
        group="", order=0, min=None, max=None, pattern=None,
        advanced=False, deprecated=None)
```

- `type`: `str`, `int`, `float`, `bool`, `list`, `dict`, `resource`, and the
  str-shaped `url`, `hostport`, `path` (with a light shape check). Strings
  from env are coerced (`"true"`; JSON or `a,b` for lists; JSON for dicts). A
  bad default is a plugin bug and raises at import time.
- `scope`: the key's home scope, `hub`, `band`, `worker` or `user`.
  `overridable` names lower scopes that may override it (a `band` key with
  `overridable=("worker",)`; a `hub` key with `overridable=("user",)`).
- `secret=True`: the value lives in the **vault** (hub scope:
  `plugin.<namespace>.<name>`), never in settings storage, and is masked in
  every listing and in history (fingerprints only).
- `env`: a variable name or a list of them (the first is the legacy name the
  UI shows). The canonical `ROOK_<NAMESPACE>_<NAME>` is always accepted by
  `plugin.settings` too.
- `apply`: how a change takes effect, shown in the UI: `live` (read on use;
  the hub refreshes `plugin.settings` when a value is saved), `reload`,
  `restart` (read only at start), `risky` (commit-confirmed worker restart).
  Declare `restart` for anything you read in `available()` or `start()`.
- `bootstrap=True`: needed before the settings store is reachable; set only
  by env or flag, shown read-only with its source.
- `group`, `order`, `advanced`, `label`, `help`: placement and text in the UI.
  `min`/`max`/`pattern`/`choices`: validation (client and hub).

Resolution in `plugin.settings[name]`: env override (legacy names, then the
canonical one), then the vault (secret) or the stored value, then the default.
An invalid value from one source is logged and skipped, so a typo degrades to
the next source instead of killing the plugin. Hub plugins read stored values
from the hub settings store (`settings.db`, written by the Settings page and
`settings.set`); the wave-1 `hub_plugin_settings.json` is still read beneath
it. Worker plugins declare their settings too (wake, memory, pikvm, cec,
dongle); the hub delivers per-worker values in a commit-confirmed config push
and secrets as `{{secret:…}}` references the worker fetches at use.

## 8. Migrations, guidance, skill, panel

- **Migrations.** `MIGRATIONS = "migrations"` names a directory of
  `NNN_description.sql` files. `self.migrate(conn)` (from `start()`) applies
  pending files in numeric order, each in its own transaction, recording
  `(namespace, version)` in `_rook_migrations`, so plugins can share a
  database. A failing file rolls back and stops the run. Released files are
  never edited; fixes are new files. Each plugin gets a private
  `self.data_dir` (`<node state>/plugins/<namespace>`).
- **Guidance slots.** `GUIDANCE = {slot: default}`. A slot is usually a cap
  name or prefix, and its text becomes that cap's tip on `rook_call` replies
  (today's guidance mechanism, `rook/band_mcp/guidance.py`). A slot may also
  name a guidance kind directly (`tool:<name>`, `cap:<prefix>`). Operators edit
  the text; the plugin ships only the default. On the hub the bridge registers
  every loaded plugin's slots with the guidance store at start-up
  (`HubNode.guidance_defaults()` then `Guidance.add_defaults`), so they show on
  the Agent instructions page and can be edited like core slots; core
  defaults win on a clash.
- **Skill fragment.** `SKILL` is markdown merged into the agent skill's tool
  reference by `tools/gen_skill_reference.py` (hub plugins today, under "Hub
  capabilities"). Keep it short: when to use the caps, the one or two args that
  matter, what's heavy.
- **Web panel.** `PANEL = {"title": ..., "path": "panel.html"}`: a static page
  beside the module that the dashboard mounts under the plugin's namespace and
  that talks to the plugin only through its caps. Declared now, mounted in
  wave 2.

## 9. Lifecycle

For each discovered candidate, the host (`rook.core.host.PluginHost`) runs:

1. **Filter**: operator-disabled modules are skipped (`disabled`); `enabled`
   limits to a list.
2. **Import** the module and read `PLUGIN`; construct it if it's a class.
3. **Manifest check**: `CORE_API` compatible, `VERSION` shape (warning only).
4. **Placement** over this node's facts; `run="one"` asks the elector.
5. **Dependencies**: a plugin whose `DEPENDS` are not loaded yet waits until
   the other candidates have been tried (then `unavailable` if still missing).
6. **Wire**: settings view, resource caller, data dir, dependencies. Since
   core API 1.1 this happens *before* `available()`, so an enable flag or a
   resource in the settings schema can gate loading.
7. **`available()`**: backend/config present on this node.
8. **Register** caps (all or nothing); default `VERSION`.
9. Node-specific binding: `bind_worker(worker)` on workers, `bind_host(hub_node)`
   on the hub.
10. **`start()`** (async) when the node starts; `heartbeat()` on every announce
   (~30 s; tiny, non-raising); **`stop()`** at shutdown.

`host.status[module]` records the outcome: `loaded`, `disabled`, `skipped`
(no/None `PLUGIN`), `not_placed`, `unavailable` or `failed` (with a reason),
plus `start_error` if `start()` raised. `host.manifests()` (and the hub's
`hub.plugins` cap) expose it.

## 10. The hub node: worker `rook`

### 10.1 On the band

The hub runs a `PluginHost` whose facts hold `is_hub`
(`rook/hub/node.py:HubNode`). It is attached to every band client of the MCP
bridge (`BandClient.attach_local`, also `MultiBandClient`, including bands
added later) and:

- announces itself on each band every ~30 s as a worker named **`rook`**
  (the reserved name, permissions.md 4.6), with `caps`, `plugins`, `facts`,
  `roles`, `tiers` and `core_api`. Its id persists in `hub_node_id` beside the
  hub stores (the permissions work replaces it with the op-key `kid`);
- answers band requests addressed to its id, or open requests for a cap it
  owns, with the same reply shape as a worker (`{id, from, ok, result|error}`);
- is never evicted from its own roster, and ignores its own announce echoed
  back.

A remote announce claiming the name `rook` (any case) without a valid
`is_hub` grant for the band, held by the announcer (op-key signed announce),
is listed as `rook~<id8>`, flagged `quarantined` and journaled once as
`audit.impostor` (permissions.md 4.6). The hub's own announce carries its
grant and signature, so band clients without a hub node (the dashboard) also
resolve `rook` to it.

### 10.2 From the MCP bridge

`rook_call(cap, worker="rook")` resolves `rook` through the normal roster and
the band client short-circuits the call in process: it never touches the band,
and attribution, journaling, secret substitution and guidance tips are those
of any `rook_call`. `rook_workers` lists `rook` like any worker; `rook_caps` shows
hub-only caps as held by `["rook"]` and does not count `rook` toward `"*"`/`all_but`
(so worker caps stay `"*"`).

### 10.3 Generated MCP tools

For every hub cap with `tool=True`, the bridge adds a tool named
`rook_<cap with dots as underscores>` (`notes.search` becomes
`rook_notes_search`). Its parameters mirror the handler signature (plus
`limit`/`fields` when core enforces them) and its description is the cap's
`description` or the first paragraph of its docstring; `guidance.apply` then
advertises it like every other tool (`descriptions.for_tool`,
`envelope.slim_tool`, operator tips). The tool is an alias of
`rook_call(cap=..., worker="rook")`, so it returns exactly what `rook_call`
returns. Existing tool names always win: a generated name that is already
taken is skipped with a warning.

Every connect pays for `tools/list` (budgeted in
`tests/test_token_envelope.py`), so `tool=True` is for caps agents use
constantly. Everything else stays one `rook_call` away. The reference
plugin's `hub.info` deliberately declares no tool.

**Plugin-shaped tools.** A tool whose shape predates caps and must not change
(the action-style `rook_knowledge`, `rook_task`, `rook_project`,
`rook_concept`) cannot be generated one-to-one from a cap. A hub plugin may
return such tools from `mcp_tools(invoke)` (`rook.hub.mcp_tools.register_plugin_tools`):
each is an async function (its `__name__` is the tool name, its docstring the
description) that routes to the plugin's caps through `invoke(cap, args)`.
`invoke` runs the cap in process through the registry (core's `limit`/`fields`
contract applies) with the MCP caller's identity, and returns the result or
raises the handler's exception, so the tool formats its own reply exactly as
before. Existing names win here too.

### 10.4 Band calls are read-only by default

Anyone holding the band PSK can put a request on the band with any
`identity` string, so band-originated calls to the hub node may reach only
caps declared `risk="read"`. `ROOK_HUB_BAND_MAX_RISK` raises the ceiling;
undeclared risk counts as `exec`. MCP calls are token-attributed and are not
limited here. Per-principal policy replaces this rule when permissions
enforcement lands. Band calls to the hub are journaled with worker `rook`.

`ROOK_HUB_PLUGINS=0` turns the hub node off entirely.

The wire-level contract (frames, messages, caps, placement, grants, chat
rooms) is specified for other implementations in `docs/spec/core-v1.md`,
with test vectors in `conformance/`.

## 11. Failure isolation

- A plugin that fails to import, construct, pass `available()`, or register
  (a cap collision with another plugin) is marked `failed` and every other
  plugin still loads. A collision leaves no half-registered caps behind.
  (The old worker loader crashed the worker on an import error.)
- `start()`/`stop()`/`heartbeat()` exceptions are logged and contained.
- A cap that raises returns `{ok: false, error: "<Type>: <msg>"}`; bad
  arguments return `bad args: ...`. The dispatch loop is never affected.
- On the hub, a failure to build the plugin host leaves the MCP bridge
  running without hub caps.
- All plugins share a process. Isolation is fault containment, not a security
  boundary: install only plugins you trust. (Out-of-process plugins, a
  subprocess speaking the band protocol, are a possible later extension; the
  cap contract doesn't change.)

## 12. Discovery

1. **Built-in package scan**: every non-underscore module of the node's
   plugin package (`rook.worker.plugins` on workers, `rook.hub.plugins` on
   the hub).
2. **Entry points**: installed distributions declare
   ```toml
   [project.entry-points."rook.plugins"]
   notes = "rook_notes.plugin:PLUGIN"   # or just "rook_notes.plugin"
   ```
   Both node types scan the group; placement decides where each plugin
   actually loads. A zipapp bundle has no distribution metadata, so bundled
   workers see only built-ins.

Runtime enable/disable (`worker.plugin.*`) persists per worker as before.

## 13. Versioning and compatibility

- **Core API.** `CORE_API_VERSION` is `major.minor`. Minor bumps only add
  (new optional manifest keys, new host services). A major bump may remove or
  change behaviour; plugins declare the range they support and are refused
  outside it. Wave 1 shipped `1.0`; `1.1` adds `DEPENDS` and wires settings before `available()`.
- **Plugin versions** follow the worker build scheme
  `<build>.<adjective>.<noun>`: the build number is monotonic and the
  adjective/noun pair names the commit.
- **Wire.** Everything added is optional: announce keys `facts`, `tiers`,
  `roles`, `core_api` and `grants` (later); a hub node that looks like one
  more worker. Build-167 workers ignore frames without `cap` (announces) and
  unknown announce keys; build-167 band clients and dashboards see `rook` as a
  worker and can call its read caps. No negotiation is needed. Anything that
  changes the meaning of an existing field would need a version check against
  the peer's announced `build`/`core_api`.
- **Worker bundles** now include `rook/core` (`build_band_worker.py`,
  `android/stage_worker.py`); `rook.core` must stay stdlib-only, and a test
  enforces that.

## 14. Wave status

| Item | Wave 1 (this change) | Later |
|---|---|---|
| Core plugin host shared by hub and worker | done | |
| Manifest, `core_api` check, versions | done | |
| Placement expressions + facts in announces | done | hub-driven `run="one"` election for workers |
| Role grants | verified (`roles_from_announce`: root signature, band scope, proof of possession) | done (permissions wave 2) |
| `risk`/`tags`/`limit`/`fields`/`tool` | declared, `limit`/`fields` enforced, tiers announced | policy enforcement, byte caps |
| Hub node `rook` + band serving | done, band calls read-only | tickets, signed announces |
| Generated MCP tools | done (no built-in hub cap uses it yet; `tools/list` unchanged) | regenerate chat/vault/journal tools from caps |
| Plugin-shaped MCP tools (`mcp_tools(invoke)`) | wave 2: knowledge/task tools, `tools/list` unchanged | |
| Settings schema + resolution | done (env aliases, store, vault, default); settings UI, writes, history, worker delivery (settings.md, implementation status) | typed worker settings map; user prefs to Android |
| Resources | parsed; `cap://` callable on the hub | placement-aware `any` |
| Migrations, data dir | done | |
| Guidance slots, skill fragments, panels | slots registered with the guidance store (wave 2); skill fragments generated | dashboard panel mounting |
| `DEPENDS`, settings wired before `available()` (core API 1.1) | wave 2 | |
| Knowledge, tasks as plugins | wave 2 (section 15) | settings UI writes; `handoff.*` caps with chat/journal |
| Chat rooms on the band (`chat.*` on `rook`, `rook/hub/plugins/rooms.py`) | wave 3 (core spec) | per-principal band identities (device-signed calls) |

## 15. Knowledge and tasks (hub plugins)

The shared wiki and the work tree (concept > project > task) are two hub
plugins on one store:

| Plugin | Module | Caps (worker `rook`) | MCP tools (unchanged) |
|---|---|---|---|
| `knowledge` | `rook/hub/plugins/knowledge/` (store, search, service, maintenance, web, migrations) | `knowledge.read` (R): search, get, list, context, status, bands, deck; `knowledge.write` (W): create, update, link, retract | `rook_knowledge` |
| `tasks` (namespace `task`, `DEPENDS = ("knowledge",)`) | `rook/hub/plugins/tasks.py` | `task.read` (R): deck, search, list, get, context, status; `task.write` (W): create, update, link, retract, claim, release; `kind` = task, project or concept | `rook_task`, `rook_project`, `rook_concept` |

The cap names follow permissions.md Appendix A.2. Each cap takes the tool's
arguments (`action`, `band`, `id`, `query`, `data`, `request_id`) and returns
the same result, with the lean MCP defaults for search/list (5/20 rows, a
small field set; `data.limit` / `data.fields` override). A read cap refuses a
write action, so the band ceiling (section 10.4) can't be bypassed through
`*.read`. The tools route each action to the read or write cap and keep their
reply shape (`{"ok": true, "result": ...}` or `{"ok": false, "error", "code"}`).
`rook/knowledge/*` remains as import aliases of the moved modules.

**Settings** (`knowledge` plugin; the tasks plugin loads with it):

| Setting | Type | Default | Env (legacy alias) |
|---|---|---|---|
| `enabled` | bool | `false` | `ROOK_KNOWLEDGE` |
| `db_path` | str | empty = `knowledge.db` beside the hub's other stores | `ROOK_KNOWLEDGE_DB` |
| `semantic` | bool | `true` (used only when `embedder` is set) | `ROOK_KNOWLEDGE_SEMANTIC` |
| `embedder` | resource | empty = keyword search only | `ROOK_EMBED_URL` |
| `embed_model` | str | `sentence-transformers/all-MiniLM-L6-v2` | `ROOK_EMBED_MODEL` |
| `hygiene_*` | | see [hygiene.md](hygiene.md) (live) | `ROOK_KNOWLEDGE_HYGIENE_*` |

`embedder` is `http(s)://…/embed` (the `services/knowledge-embeddings`
service, as before; an existing `ROOK_EMBED_URL` keeps working) or a band cap
such as `cap://any/embed.text`. Both are called with `{"texts": [...]}` and
return `{"model", "vectors"}` (a cap may return a bare list of vectors); the
model must match `embed_model`. An empty value means "not configured". The
settings are read when the hub starts. With `ROOK_HUB_PLUGINS=0` there is no
hub node, so knowledge and tasks are off too.

**Storage and migrations.** The store stays where it was (`db_path`
default), so an existing `knowledge.db` is used in place and a rollback to an
older release still finds it. Migration `001_schema.sql` (namespace
`knowledge` in `_rook_migrations`) is the layout the pre-plugin store created
(`PRAGMA user_version=2`), all `IF NOT EXISTS`. Before it runs on a database
that predates plugin migrations, `KnowledgeStore._upgrade_legacy` applies the
old in-code steps that need Python (adding and back-filling `slug`,
`actors.info`, the v1 state renames, dropping prototype tables), so every
record, link, claim, event, receipt, embedding and cursor is kept
(`tests/test_knowledge_plugin.py` checks a database written by the old code,
`tests/fixtures/knowledge_v2.db`). `user_version` stays 2 until a later
migration changes the layout.

**Attribution.** Writes record the caller's compound identity. Through the
MCP the bridge hands the plugin its attribution (`Knowledge.principal`), so
tool calls and `rook_call(worker="rook")` are attributed exactly as before. A
band call (only possible for writes when `ROOK_HUB_BAND_MAX_RISK` allows it)
records the self-stamped envelope identity with actor kind `band`.

**What stays in the bridge.** The bridge wires in what only it has: the
attributed caller, the band enrollment (for `bands`), the handoff store (for
`data.handoff`), the hygiene loop, auto-linking of calls/consoles/handoffs to
the caller's claimed task, and the operator Knowledge page API
(`/knowledge/account-api`, proxied by the dashboard). Background embedding
indexing runs in the plugin's `start()`.

**Handoffs stay in the bridge**, not in the tasks plugin. A handoff is a
*thread* record (`rook/band_mcp/sessions.py`): its `thread_id` is the same
namespace as the call journal's and chat rooms' thread ids, and
`rook_handoff_*` must keep working with knowledge off (the default). Tasks
only link to handoffs (link kind `handoff`), and `data.handoff` on
update/release saves one through the bridge. When chat rooms and the journal
become hub caps, handoffs move with them as `handoff.read` / `handoff.write`
(permissions.md A.2), not into `task.*`.

## 16. Chat integrations (hub plugins)

Telegram and Discord are the hub plugins `telegram` and `discord`, both built
on `rook/hub/integrations.py`, plus `notify` (`notify.send`, one cap over
every running integration). They bridge chat rooms to one chat or channel,
send notifications, and answer a fixed command set under the principal
`integration:<platform>`. The integration refuses anything the policy does
not allow, including a `would_deny` in audit mode. They are off by default,
use no platform library (aiohttp only), and keep the token in the vault
(`plugin.<platform>.token`). See [../integrations.md](../integrations.md).
