"""Plugins, the hub side (PL1 to PL4, PL6, K2). The format is in docs/plugins.md.

A plugin is a folder with a manifest, `plugin.json`. The hub installs it from a
folder on its own disk (`plugin.install`): it checks the manifest, copies the
folder into `<state dir>/plugins/`, imports the plugin's hub module and calls
its `setup(plugin)` (library.py). Installing loads it at once, with no restart,
and installing a plugin whose name is already installed replaces its code at
once. When the hub starts it loads every installed plugin (PL3).

A plugin adds, never replaces (PL1): a cap name or a record type that the hub or
another plugin already has is refused, and then nothing of that plugin is kept.
An install is all or nothing. A plugin is a hub object with a GUID and a moniker
(G1). Its worker section is kept and reported, nothing more, until the slice
that acts on it. Its site section (the last block of this file) adds pages,
settings sections and Vue components to the site (PL1, P1).

Plugin code runs inside the hub, unsandboxed (PL3). Each load imports the hub
module under a name of its own, so reinstalling one plugin cannot disturb another.
"""

import importlib.util
import inspect
import json
import logging
import pathlib
import re
import shutil
import sys
import uuid

from rook_protocol import ProtocolError, validate

from . import records
from .errors import HubError
from .library import Call, Plugin
from .journal import iso

log = logging.getLogger(__name__)  # rook_hub.plugins: where a plugin that fails to load at start is reported

MANIFEST = "plugin.json"
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
    guid         TEXT PRIMARY KEY,
    moniker      TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL UNIQUE,
    version      TEXT NOT NULL,
    folder       TEXT NOT NULL,          -- its copy's name in <state dir>/plugins/, so the data directory can move (F3)
    manifest     TEXT NOT NULL,          -- plugin.json as installed
    installed_at REAL NOT NULL
);
"""


def read_manifest(folder):
    """The folder's manifest, checked (docs/plugins.md), or HubError saying what to fix."""
    path = folder / MANIFEST
    fix = f"Fix {path} as docs/plugins.md describes, then install again."
    if not folder.is_dir():
        raise HubError("bad_plugin", f"{folder} is not a folder on the hub's disk.",
                       "Give the path of a plugin's folder on the hub's disk.")
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise HubError("bad_manifest", f"{folder} has no {MANIFEST}.", fix) from None
    except (ValueError, UnicodeDecodeError) as e:
        raise HubError("bad_manifest", f"{path} is not JSON: {e}.", fix) from None
    if not isinstance(manifest, dict):
        raise HubError("bad_manifest", f"{path} must be a JSON object.", fix)
    name, version = manifest.get("name"), manifest.get("version")
    if not isinstance(name, str) or not _NAME.match(name) or name == records.CORE:
        raise HubError("bad_manifest", f"{path}: name must be lowercase letters and digits, hyphens between them, "
                                      f"and not {records.CORE!r}; it is {name!r}.", fix)
    if not isinstance(version, str) or not version:
        raise HubError("bad_manifest", f"{path}: version must be a string such as \"1.0.0\".", fix)
    for section in ("hub", "worker", "site"):
        if section in manifest and not isinstance(manifest[section], dict):
            raise HubError("bad_manifest", f"{path}: {section} must be an object.", fix)
    module = manifest.get("hub", {}).get("module")
    if "hub" in manifest and (not isinstance(module, str) or not (folder / module).is_file()):
        raise HubError("bad_manifest", f"{path}: hub.module must name the plugin's Python file in the folder; "
                                      f"it is {module!r}.", fix)
    skill = manifest.get("skill")
    if skill is not None and (not isinstance(skill, str) or not (folder / skill).is_file()):
        raise HubError("bad_manifest", f"{path}: skill must name a file in the folder; it is {skill!r}.", fix)
    read_site(folder, manifest, path, fix)
    return manifest


class Plugins:
    """The plugins installed on one hub, and the caps they serve."""

    def __init__(self, hub):
        self.hub = hub
        self.root = hub.state_dir / "plugins"
        self.caps = {}       # cap name -> (plugin name, handler, {"description", "schema"})
        self.drivers = {}    # driver name -> (plugin name, library.Driver) (D2)
        self._modules = {}   # plugin name -> the module name its hub code is loaded under
        self._loads = 0

    def _load(self, name, folder, manifest):
        """Import the plugin's hub module and run its setup(). Returns the declared Plugin."""
        declared = Plugin(name)
        if "hub" not in manifest:
            return declared, None
        path = folder / manifest["hub"]["module"]
        self._loads += 1
        module_name = f"rook_plugin_{name.replace('-', '_')}_{self._loads}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            if not callable(getattr(module, "setup", None)):
                raise HubError("bad_plugin", f"{path} has no setup(plugin) function.",
                               "Define setup(plugin) in the hub module; docs/plugins.md shows one.")
            module.setup(declared)
        except HubError:
            sys.modules.pop(module_name, None)
            raise
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise HubError("plugin_load_failed", f"Plugin {name}'s hub code failed to load: {type(e).__name__}: {e}",
                           f"Fix {path.name} so that importing it and calling setup() succeed, then install again.") from None
        for cap, (handler, meta) in declared.caps.items():
            try:
                validate({"type": "call", "id": "-", "cap": cap, "args": {}})
            except ProtocolError as e:
                raise HubError("bad_plugin", f"Plugin {name} declares cap {cap!r}: {e}.",
                               "Name caps as dot-separated lowercase segments, such as note.create (W3).") from None
            if not callable(handler) or not isinstance(meta["description"], str) or not meta["description"] \
                    or "\n" in meta["description"] or not isinstance(meta["schema"], dict):
                raise HubError("bad_plugin", f"Plugin {name}'s cap {cap} needs a handler, a one-line description "
                                            "and a JSON Schema object.",
                               "Declare it as @plugin.cap(name, description, schema) on a function (W3).")
        for kind in declared.record_types:
            if not isinstance(kind, str) or not kind:
                raise HubError("bad_plugin", f"Plugin {name} declares a record type {kind!r}.",
                               "Give each record type a name, such as \"note\".")
        for driver in declared.drivers.values():
            _check_driver(name, driver)
        return declared, module_name

    def _clashes(self, declared):
        """Refuse names the hub or another plugin already has (PL1)."""
        from . import caps           # caps.py imports this module for its own caps
        for cap in declared.caps:
            owner = "the hub" if cap in caps.CAPS else self.caps.get(cap, (None,))[0]
            if owner is not None and owner != declared.name:
                raise HubError("cap_taken", f"Cap {cap} is already served by "
                                           f"{owner if owner == 'the hub' else 'plugin ' + owner}.",
                               "Plugins add, never replace: give the cap another name and install again.")
        for driver in declared.drivers:
            owner = self.drivers.get(driver, (None,))[0]
            if owner is not None and owner != declared.name:
                raise HubError("driver_taken", f"Driver {driver} is already declared by plugin {owner}.",
                               "Plugins add, never replace: give the driver another name and install again.")
        with self.hub._lock:
            for kind in declared.record_types:
                row = self.hub._db.execute("SELECT plugin FROM record_types WHERE type = ?", (kind,)).fetchone()
                if row is not None and row["plugin"] != declared.name:
                    raise HubError("type_taken", f"Record type {kind!r} belongs to "
                                                f"{'core' if row['plugin'] == records.CORE else 'plugin ' + row['plugin']}.",
                                   "Plugins add, never replace: declare a record type of your own and install again.")

    def _serve(self, declared, module_name):
        """Swap the plugin's caps in: its old caps go, its new ones come, in one step."""
        old = self._modules.pop(declared.name, None)
        if old is not None:
            sys.modules.pop(old, None)
        if module_name is not None:
            self._modules[declared.name] = module_name
        kept = {cap: entry for cap, entry in self.caps.items() if entry[0] != declared.name}
        kept.update({cap: (declared.name, handler, meta) for cap, (handler, meta) in declared.caps.items()})
        self.caps = kept
        drivers = {name: entry for name, entry in self.drivers.items() if entry[0] != declared.name}
        drivers.update({name: (declared.name, driver) for name, driver in declared.drivers.items()})
        self.drivers = drivers

    # ----------------------------------------------------------- install

    def install(self, source, actor):
        """Install, or replace, the plugin in folder `source` (PL4). All or nothing."""
        source = pathlib.Path(source).expanduser().resolve()
        manifest = read_manifest(source)
        name = manifest["name"]
        with self.hub._lock, self.hub._db:
            self.hub.auth.actor(**actor)
        self.root.mkdir(exist_ok=True)
        folder = self.root / f"{name}.{uuid.uuid4().hex[:8]}"
        module_name = None
        try:
            shutil.copytree(source, folder, ignore=shutil.ignore_patterns("__pycache__"))
            declared, module_name = self._load(name, folder, manifest)
            self._clashes(declared)
            site_clashes(self.hub, manifest)
            for kind in declared.record_types:
                records.declare_type(self.hub, kind, name)
            with self.hub._lock, self.hub._db:
                old = self.hub._db.execute("SELECT * FROM plugins WHERE name = ?", (name,)).fetchone()
                guid = old["guid"] if old else str(uuid.uuid4())
                if old is None:
                    self.hub._db.execute(
                        "INSERT INTO plugins (guid, moniker, name, version, folder, manifest, installed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (guid, self.hub._new_moniker(guid), name, manifest["version"],
                                                         folder.name, json.dumps(manifest), self.hub.clock()))
                else:
                    self.hub._db.execute("UPDATE plugins SET version = ?, folder = ?, manifest = ?, installed_at = ? "
                                         "WHERE guid = ?", (manifest["version"], folder.name, json.dumps(manifest),
                                                            self.hub.clock(), guid))
        except BaseException:
            if module_name is not None:
                sys.modules.pop(module_name, None)
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self._serve(declared, module_name)
        if old is not None and old["folder"] != folder.name:
            shutil.rmtree(self.root / old["folder"], ignore_errors=True)   # the replaced code; one copy per plugin
        return self.get(name)

    def load_all(self):
        """Load every installed plugin, as the hub starts. One that fails is logged and skipped."""
        with self.hub._lock:
            rows = self.hub._db.execute("SELECT * FROM plugins ORDER BY installed_at").fetchall()
        for row in rows:
            try:
                declared, module_name = self._load(row["name"], self.root / row["folder"], json.loads(row["manifest"]))
                self._clashes(declared)
                self._serve(declared, module_name)
            except HubError as e:
                log.error("Plugin %s was not loaded: %s %s", row["name"], e.message["message"], e.message["next"])

    # ------------------------------------------------------------ reading

    def get(self, name):
        with self.hub._lock:
            row = self.hub._db.execute("SELECT * FROM plugins WHERE name = ? OR guid = ? OR moniker = ?",
                                       (name, name, name)).fetchone()
            if row is None:
                raise HubError("unknown_plugin", f"No plugin is called {name!r}.",
                               "List the plugins with plugin.list and use a name, moniker or GUID from there.")
            types = [r[0] for r in self.hub._db.execute(
                "SELECT type FROM record_types WHERE plugin = ? ORDER BY type", (row["name"],))]
        manifest = json.loads(row["manifest"])
        return {"guid": row["guid"], "moniker": row["moniker"], "name": row["name"], "version": row["version"],
                "installed_at": iso(row["installed_at"]),
                "caps": sorted(cap for cap, entry in self.caps.items() if entry[0] == row["name"]),
                "record_types": types,
                "drivers": sorted(d for d, entry in self.drivers.items() if entry[0] == row["name"]),
                "worker": "worker" in manifest, "site": "site" in manifest, "skill": "skill" in manifest}

    def list(self):
        with self.hub._lock:
            names = [r[0] for r in self.hub._db.execute("SELECT name FROM plugins ORDER BY installed_at")]
        return [self.get(name) for name in names]

    # ------------------------------------------------------------ serving

    def descriptors(self):
        return [dict(meta, name=cap, returns="result") for cap, (_, _, meta) in sorted(self.caps.items())]

    def call(self, cap, args, actor):
        """Run a plugin's cap. A handler that raises becomes an error naming the plugin (C3)."""
        name, handler, _ = self.caps[cap]
        with self.hub._lock, self.hub._db:
            user, token = self.hub.auth.actor(**actor)
        try:
            result = handler(Call(self.hub, name, actor, user, token), args)
            json.dumps(result)
            return result
        except HubError:
            raise
        except Exception as e:
            log.exception("Plugin %s failed in %s", name, cap)
            raise HubError("plugin_failed", f"Plugin {name} failed in {cap}: {type(e).__name__}: {e}",
                           f"Check the arguments against {cap}'s schema (caps tool); if they are right, "
                           f"plugin {name} needs fixing and installing again.") from None


def _check_driver(plugin, driver):
    """A driver's name, caps and status function, as W3 and D2 need them; else HubError."""
    fix = "Declare drivers as docs/plugins.md shows: plugin.driver(name), async caps, an async status."
    if not isinstance(driver.name, str) or not _NAME.match(driver.name):
        raise HubError("bad_plugin", f"Plugin {plugin} declares a driver named {driver.name!r}: use lowercase "
                                     "letters and digits, hyphens between them.", fix)
    for cap, (handler, meta) in driver.caps.items():
        try:
            validate({"type": "call", "id": "-", "cap": cap, "args": {}})
        except ProtocolError as e:
            raise HubError("bad_plugin", f"Driver {driver.name} declares cap {cap!r}: {e}.", fix) from None
        if not inspect.iscoroutinefunction(handler) or not isinstance(meta["description"], str) \
                or not meta["description"] or "\n" in meta["description"] or not isinstance(meta["schema"], dict):
            raise HubError("bad_plugin", f"Driver {driver.name}'s cap {cap} needs an async handler, a one-line "
                                         "description and a JSON Schema object.", fix)
    if driver.status_handler is not None and not inspect.iscoroutinefunction(driver.status_handler):
        raise HubError("bad_plugin", f"Driver {driver.name}'s status function must be async.", fix)


# ------------------------------------------------------------ hub caps (PL4)

def install(hub, args, actor):
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise HubError("bad_message", "args.path is required and must be a string.",
                       "Give the path of the plugin's folder on the hub's disk.")
    return {"installed": hub.plugins.install(path, actor)}


def list_plugins(hub, args, actor):
    return {"plugins": hub.plugins.list()}


CAPS = {
    "plugin.install": (install, {
        "description": "Install a plugin from a folder on the hub's disk, or replace it if one by that name is "
                       "installed; it loads at once.",
        "schema": {"type": "object", "required": ["path"], "additionalProperties": False, "properties": {
            "path": {"type": "string", "description": "The plugin's folder, holding plugin.json."}}},
    }),
    "plugin.list": (list_plugins, {
        "description": "List the installed plugins: version, the caps and record types each added, and whether "
                       "it has a worker section, a site section and a skill.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }),
}


# ------------------------------------------------------------ the site section (PL1, P1, P2, P6)
#
# "site": {"pages": [{"segment", "title", "file"}], "settings": [{"title", "file"}],
#          "components": [{"name", "file"}]}   (docs/plugins.md; Bake 13 Nov)
# Each file is an ES module in the plugin's folder whose default export is a Vue component.
# A page's name is its title in lowercase, words joined by hyphens: the plugin's page `notes`
# is at /plugins/<plugin>/notes, so a pin names the plugin and the page (Bake 13 Nov).

SITE_FIELDS = {"pages": ("segment", "title", "file"), "settings": ("title", "file"), "components": ("name", "file")}
_COMPONENT = re.compile(r"[a-z0-9]+(-[a-z0-9]+)+\Z")    # a hyphen, so it is never an HTML element's name


def page_name(title):
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))


def read_site(folder, manifest, path, fix):
    """Check the site section, or HubError saying what to fix: each entry has its fields,
    each file is in the folder, and names are good and not repeated within the plugin."""
    site = manifest.get("site")
    if site is None:
        return
    root = folder.resolve()
    for part, fields in SITE_FIELDS.items():
        entries = site.get(part, [])
        if not isinstance(entries, list):
            raise HubError("bad_manifest", f"{path}: site.{part} must be a list.", fix)
        names = set()
        for i, entry in enumerate(entries):
            where = f"{path}: site.{part}[{i}]"
            if not isinstance(entry, dict) or not all(isinstance(entry.get(f), str) and entry[f].strip() for f in fields):
                raise HubError("bad_manifest", f"{where} must be an object with {', '.join(fields)}, each a string.", fix)
            target = (folder / entry["file"]).resolve()
            if not target.is_relative_to(root):
                raise HubError("bad_manifest", f"{where}: file {entry['file']!r} leaves the plugin's folder.",
                               f"Put the file inside {folder} and name it by its path there, such as site/page.js.")
            if not target.is_file():
                raise HubError("bad_manifest", f"{where}: file {entry['file']!r} is missing.",
                               f"Add the file to {folder}, or fix its path in {MANIFEST}.")
            if part == "components" and not _COMPONENT.match(entry["name"]):
                raise HubError("bad_manifest", f"{where}: name {entry['name']!r} must be lowercase letters and digits "
                                              "with at least one hyphen, such as note-card.", fix)
            name = entry["name"] if part == "components" else page_name(entry["title"])
            if part != "settings" and (not name or name in names):
                raise HubError("bad_manifest", f"{where}: {'title' if part == 'pages' else 'name'} "
                                              f"{entry.get('title', entry['name'])!r} is empty or used twice in this plugin.", fix)
            names.add(name)


def site_file(folder, file):
    """A manifest's file name as the site serves it: its path in the folder, with forward slashes."""
    return (folder / file).resolve().relative_to(folder.resolve()).as_posix()


def site_parts(hub):
    """Every installed plugin's site section, in install order: [(plugin name, installed folder, site)]."""
    with hub._lock:
        rows = hub._db.execute("SELECT name, folder, manifest FROM plugins ORDER BY installed_at").fetchall()
    return [(row["name"], hub.plugins.root / row["folder"], json.loads(row["manifest"]).get("site") or {})
            for row in rows]


def site_clashes(hub, manifest):
    """Refuse a page or component name that core or another plugin already has (PL1)."""
    from . import site           # site.py imports this module
    name, mine = manifest["name"], manifest.get("site") or {}
    pages = {p["id"]: "core" for segment in site.SEGMENTS.values() for p in segment}
    components = {c: "core" for c in site.COMPONENTS}
    for other, _, parts in site_parts(hub):
        if other != name:
            pages.update({page_name(p["title"]): f"plugin {other}" for p in parts.get("pages", [])})
            components.update({c["name"]: f"plugin {other}" for c in parts.get("components", [])})
    for page in mine.get("pages", []):
        if page_name(page["title"]) in pages:
            raise HubError("page_taken", f"{pages[page_name(page['title'])]} already has a page called "
                                         f"{page_name(page['title'])!r}.",
                           "Plugins add, never replace: give the page another title and install again.")
    for component in mine.get("components", []):
        if component["name"] in components:
            raise HubError("component_taken", f"{components[component['name']]} already has a component called "
                                              f"{component['name']!r}.",
                           "Plugins add, never replace: give the component another name and install again.")
