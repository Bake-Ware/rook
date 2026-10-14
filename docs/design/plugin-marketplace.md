# Plugin marketplace (design; preview)

Status: **stub.** The catalog format, the hub setting that lists catalogs,
the Marketplace tab on Manage > Plugins and the `plugin.marketplace.*` caps
exist. Installing from a catalog does not: the Install button says "Coming
soon" and `plugin.marketplace.install` always refuses with a not-implemented
error. This document fixes the format so catalogs can be written and reviewed
now, describes the path third-party plugins take today, and lists what must
hold before installing is turned on.

## 1. Third-party plugins today (no marketplace needed)

Plugins already load from installed Python distributions
(`rook.core.plugin.iter_entry_points`, docs/design/plugins.md §12):

1. Write a package whose module exports `PLUGIN` (a `rook.core.plugin.Plugin`
   subclass) and declare it:

   ```toml
   # pyproject.toml
   [project.entry-points."rook.plugins"]
   weather = "rook_weather:PLUGIN"     # or just "rook_weather"
   ```

2. `pip install` it into the environment of the process that should run it:
   the hub's MCP virtualenv for a hub plugin (`PLACEMENT = place("is_hub")`),
   a worker's virtualenv for a worker plugin. A worker started from the
   single-file bundle (`band-worker.pyz`) has no distribution metadata and
   only sees built-ins; run it from a virtualenv with the package installed.
3. Restart that process. The plugin shows up on Manage > Plugins with origin
   `entry-point`, where it can be switched on and off like any other.

Placement still decides where it loads, its caps declare risk tiers like
built-ins (undeclared counts as `exec`), and `CORE_API` must cover this core.

## 2. Catalog format (`rook-plugin-catalog/1`)

A catalog is one JSON document served over HTTPS:

```json
{
  "format": "rook-plugin-catalog/1",
  "name": "Example catalog",
  "example": false,
  "plugins": [
    {
      "id": "com.example.weather",
      "name": "Weather",
      "version": "1.0.0",
      "description": "Current conditions and a short forecast.",
      "author": "Example Author",
      "homepage": "https://example.com/rook-weather",
      "license": "MIT",
      "namespace": "weather",
      "placement": "is_hub",
      "caps": [
        {"cap": "weather.now", "risk": "read", "description": "Current conditions."}
      ],
      "package": {"kind": "pip", "requirement": "rook-weather==1.0.0"},
      "signature": null,
      "min_core_api": "1.0",
      "entry_point": "weather = rook_weather:PLUGIN"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `format` | exactly `rook-plugin-catalog/1`, else the document is rejected |
| `id` | lower case, dotted, reverse-DNS style (`com.example.weather`); unique in the catalog |
| `name`, `version`, `description`, `author` | required text (`version` a version string) |
| `homepage` | `https://` URL |
| `license` | SPDX expression (optional) |
| `namespace` | the plugin's cap namespace; every listed cap must be in it |
| `placement` | a placement expression (§4.2 of plugins.md), validated with the core parser; default `not is_hub` |
| `caps` | at least one `{cap, risk, description}`; `risk` is read, write, exec or admin. The risk summary shown is derived from these |
| `package.kind` | `pip`: `requirement` pins a version (`name==1.2.3`, `>=`, `~=`); `wheel`: `url` (`https://`, `.whl`) and `sha256` (64 lower-case hex) |
| `package.sha256` | required for `wheel`, allowed for `pip` (the file pip downloads) |
| `signature` | object or null. Placeholder: `{"scheme", "key_id", "value"}`; not verified yet |
| `min_core_api` | lowest `CORE_API_VERSION` it needs; the hub marks entries it cannot run |
| `entry_point` | the entry point the package declares, for review |

Entries that break a rule are dropped with the reason (shown on the page);
the rest of the catalog still lists. A catalog is at most 1 MB.

Catalog URLs are the hub setting **`core.plugins.catalogs`** (list,
`ROOK_PLUGIN_CATALOGS`, Settings > Hub > Plugins). With none set, the page
shows a bundled example catalog (`rook/hub/plugin_catalog_example.json`)
whose three entries are fictional and labelled as examples. Catalogs are
fetched by the MCP process over HTTPS only, cached for five minutes.

Agents read the same list with `plugin.marketplace.list` on worker `rook`
(read; its `refresh` refetches only for band owners and operator tokens, at
most once a minute, so a read-only caller cannot make the hub fetch);
`plugin.marketplace.install` (admin, destructive) is the stub.

## 3. Installing (not implemented): the plan

`plugin.marketplace.install(id, target)` would:

1. resolve the entry in a configured catalog and check `min_core_api`,
   placement against the target (the hub or named workers) and the
   signature (§4);
2. download the wheel (or let pip resolve the pinned requirement with
   `--require-hashes`), verify `sha256`;
3. install into the target's plugin environment (a dedicated virtualenv, not
   the core's), through the target's own updater path so a failed install
   rolls back like an OTA update does;
4. restart or `host.enable` the new module, and record who installed what in
   the journal and the settings history.

Workers on the single-file bundle need an installed-plugins directory added
to `sys.path` before entry-point discovery; Android cannot install packages
at runtime (Chaquopy) and stays built-ins only.

## 4. Security considerations (must hold before install ships)

* **Signing.** Catalog entries and packages are signed by a publisher key;
  the hub keeps an allowlist of trusted publisher keys (operator managed,
  like the OTA root key) and refuses unsigned or unknown-key entries by
  default. `sha256` pins the bytes; the signature binds id, version, caps,
  risk and hash together so a catalog host cannot swap any of them.
* **Declared vs real capabilities.** After install, the caps the plugin
  actually registers are compared with the catalog's `caps` and risk; any
  extra cap or higher tier disables the plugin and reports it.
* **Sandboxing.** A plugin runs in the worker or hub process with that
  process's rights. Until there is process isolation (a plugin subprocess
  speaking the cap protocol, or OS sandboxing), install must be treated as
  granting the plugin full control of the target, and the UI must say so.
  The permission tiers (permissions.md) still gate who may *call* its caps.
* **Review.** A curated catalog should only list reviewed source, built
  reproducibly from a tagged commit, with a changelog per version. Updates
  are opt-in per plugin, never automatic.
* **Least placement.** Install on the hub only when the plugin needs to run
  there; hub plugins see the vault and settings services.
* **Supply chain.** Pinned requirements only; no dependency resolution from
  arbitrary indexes; `--require-hashes` for every dependency.
* **Removal.** Uninstall must unload, remove the package and drop its stored
  settings and vault entries on request.
