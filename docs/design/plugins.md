# Plugins: the contract between Rook core and everything else

Status: wave 1 of the beta refactor. The core plugin host, node facts, the hub
node (`rook`) and generated MCP tools are implemented (`rook/core`,
`rook/hub`). Knowledge and tasks move onto this API in wave 2. Signed role
grants and permissions enforcement are specified in
[permissions.md](permissions.md); the settings UI and storage are specified in
[settings.md](settings.md). This document owns the plugin contract and says
where it hands off to those two.

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
| Band transport (Telesthete hub relay, WS bridge) | Knowledge wiki, tasks/projects (wave 2) |
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

`Plugin.manifest()` returns the manifest as a dict (name, namespace, version,
core_api, placement, caps, settings schema, migrations, guidance slots, skill
flag, panel). The host adds `source` (`package:<pkg>` or `entry_point:<name>`)
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
        env=None, label="", help="", choices=())
```

- `type`: `str`, `int`, `float`, `bool`, `list`, `dict` (or `resource`).
  Strings from env are coerced (`"true"`, JSON for list/dict). A bad default is
  a plugin bug and raises at import time.
- `scope`: `hub`, `band`, `worker` or `user`: where a value lives and who may
  change it (settings.md).
- `secret=True`: the value lives in the **vault** under
  `plugin.<namespace>.<name>`, never in settings storage, and is masked in
  listings.
- `env`: an env var that overrides the stored value (for containers and
  emergencies).

Resolution in `plugin.settings[name]`: env override, then the vault (secret) or
the stored value, then the default. An invalid value from one source is logged
and skipped, so a typo degrades to the next source instead of killing the
plugin. Wave 1 reads stored values from `hub_plugin_settings.json` beside the
hub's other stores (read-only; settings.md's framework writes it).

## 8. Migrations, guidance, skill, panel

- **Migrations.** `MIGRATIONS = "migrations"` names a directory of
  `NNN_description.sql` files. `self.migrate(conn)` (from `start()`) applies
  pending files in numeric order, each in its own transaction, recording
  `(namespace, version)` in `_rook_migrations`, so plugins can share a
  database. A failing file rolls back and stops the run. Released files are
  never edited; fixes are new files. Each plugin gets a private
  `self.data_dir` (`<node state>/plugins/<namespace>`).
- **Guidance slots.** `GUIDANCE = {slot: default}`. A slot is usually a cap
  name, and its text becomes that cap's tip on `rook_call` replies (today's
  guidance mechanism, `rook/band_mcp/guidance.py`). Operators edit the text;
  the plugin ships only the default. Wiring plugin slots into the guidance
  store is wave 2; manifests list the slots now.
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
5. **`available()`**: backend/config present on this node.
6. **Wire**: default `VERSION`, settings view, resource caller, data dir.
7. **Register** caps (all or nothing).
8. Node-specific binding: `bind_worker(worker)` on workers, `bind_host(hub_node)`
   on the hub.
9. **`start()`** (async) when the node starts; `heartbeat()` on every announce
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

A remote announce claiming the name `rook` (any case) is listed as
`rook~<id8>`, so name resolution stays exact. Quarantine and impostor
journaling arrive with signed grants (permissions.md 4.6).

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
constantly, typically the ones replacing a hand-written tool when knowledge
and tasks move onto caps. Everything else stays one `rook_call` away. The
reference plugin's `hub.info` deliberately declares no tool.

### 10.4 Band calls are read-only by default

Anyone holding the band PSK can put a request on the band with any
`identity` string, so band-originated calls to the hub node may reach only
caps declared `risk="read"`. `ROOK_HUB_BAND_MAX_RISK` raises the ceiling;
undeclared risk counts as `exec`. MCP calls are token-attributed and are not
limited here. Per-principal policy replaces this rule when permissions
enforcement lands. Band calls to the hub are journaled with worker `rook`.

`ROOK_HUB_PLUGINS=0` turns the hub node off entirely.

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
  outside it. Wave 1 ships `1.0`.
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
| Role grants | verification hook (stub rejects all) | permissions wave 2 |
| `risk`/`tags`/`limit`/`fields`/`tool` | declared, `limit`/`fields` enforced, tiers announced | policy enforcement, byte caps |
| Hub node `rook` + band serving | done, band calls read-only | tickets, signed announces |
| Generated MCP tools | done (no built-in hub cap uses it yet; `tools/list` unchanged) | regenerate chat/vault/journal/knowledge/tasks tools from caps |
| Settings schema + resolution | done (env, stored file, vault, default) | settings UI, writes, history |
| Resources | parsed; `cap://` callable on the hub | placement-aware `any` |
| Migrations, data dir | done | |
| Guidance slots, skill fragments, panels | declared; skill fragments generated | guidance store + dashboard mounting |
| Knowledge, tasks as plugins | | wave 2 |
