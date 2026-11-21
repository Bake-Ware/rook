"""Plugins, the hub side (PL1 to PL4, PL6, K2). The format is in docs/plugins.md.

A plugin is a folder with a manifest, `plugin.json`. The hub installs it from a
folder on its own disk, or from a URL (`plugin.install`, PL4): a URL names a zip or tar
archive (gzip, bzip2 or xz, or none) holding the folder, either at the archive's top or as
its one top-level folder, as a GitHub release or branch archive has it. The hub downloads it
off its loop, as long as that takes, and unpacks it into a new temporary folder, which goes
once the install ends. Then, as for a folder, it checks the manifest, copies the
folder into `<state dir>/plugins/`, imports the plugin's hub module and calls
its `setup(plugin)` (library.py). Installing loads it at once, with no restart,
and installing a plugin whose name is already installed replaces its code at
once. When the hub starts it loads every installed plugin (PL3).

Core's own caps ship as plugins installed by default (PL7): the folders under
`defaults/` (`battery`: battery.status; `file`: files; `net`: the network primitives, D3;
`shell`: commands, the environment and terminals; `worker`: worker.ping).
At every start the hub installs each one its data directory lacks, and updates each
one whose installed version is older than the shipped one, as any plugin is installed
(install_defaults). Otherwise they are installed plugins like any other: loaded at
every start, replaced by an install of the same name, pushed to workers and withdrawn
by the opt-out.

A plugin adds, never replaces (PL1): a cap name or a record type that the hub or
another plugin already has is refused, and then nothing of that plugin is kept.
An install is all or nothing. A plugin is a hub object with a GUID and a moniker
(G1). Its site section (the last block of this file) adds pages, settings
sections and Vue components to the site (PL1, P1).

A plugin's worker part (PL3) is a module per worker form. The hub pushes the
parts to each connected worker as one `plugins` message listing every part that
worker should have: when it connects, and again when a plugin is installed or
replaced or its opt-out changes, to each worker whose list changed. A worker
opted out of plugins (P4) gets an empty list. Only the form `python` is pushed
to in this slice; other forms' entries are kept. What a worker failed to load
it reports in its heartbeat; the hub journals it and `plugin.list` shows it.

Plugin code runs inside the hub, unsandboxed (PL3). Each load imports the hub
module under a name of its own, so reinstalling one plugin cannot disturb another.
"""

import asyncio
import importlib.util
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
import inspect
import json
import logging
import pathlib
import re
import shutil
import sys
import uuid

from rook_protocol import ProtocolError, encode, validate

from . import records
from .errors import HubError
from .library import Call, Plugin
from .journal import iso

log = logging.getLogger(__name__)  # rook_hub.plugins: where a plugin that fails to load at start is reported

MANIFEST = "plugin.json"
DEFAULTS = pathlib.Path(__file__).parent / "defaults"   # core's own caps as plugins (PL7)
PUSHED = "python"   # the worker form the hub pushes worker parts to (PL3); others are kept, not pushed
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\Z")
_VERSION = re.compile(r"[0-9]+(\.[0-9]+)*\Z")

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
CREATE TABLE IF NOT EXISTS plugin_optouts (  -- workers that receive no plugin worker parts (P4); kept at the hub (W4)
    worker TEXT PRIMARY KEY REFERENCES workers(guid)
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
    part = manifest.get("worker", {}).get(PUSHED)
    if part is not None:
        module = part.get("module") if isinstance(part, dict) else None
        where = (folder / module).resolve() if isinstance(module, str) and module else None
        if where is None or not where.is_relative_to(folder.resolve()) or not where.is_file():
            raise HubError("bad_manifest", f"{path}: worker.{PUSHED}.module must name a Python file inside the "
                                          f"plugin's folder; it is {module!r}.", fix)
        try:
            where.read_text("utf-8")
        except UnicodeDecodeError:
            raise HubError("bad_manifest", f"{path}: worker.{PUSHED}.module {module!r} is not UTF-8 text.", fix) from None
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
        self.loaded = set()  # the plugins loaded at the hub: only theirs are pushed to workers
        self._pushed = {}    # worker guid -> (link, parts): what that link was last sent (PL3)
        self._sending = set()
        self.declared = {}   # plugin name -> its library.Plugin as loaded: settings, skills, carried skills
        self._queues = {}    # (plugin, key) -> the last task asked of Call.later with that key
        self._later = set()  # every such task until it ends

    def _load(self, name, folder, manifest):
        """Import the plugin's hub module and run its setup(). Returns the declared Plugin."""
        from . import owner_only     # __init__.py imports this module first
        declared = Plugin(name, folder, self.hub.state_dir / "plugin-data" / name)
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
        finally:
            owner_only(folder)          # the __pycache__ Python wrote as it imported, owner-only like the rest
        for cap, (handler, meta) in declared.caps.items():
            try:
                validate({"type": "call", "id": "-", "cap": cap, "args": {}})
            except ProtocolError as e:
                raise HubError("bad_plugin", f"Plugin {name} declares cap {cap!r}: {e}.",
                               "Name caps as dot-separated segments, such as note.create, "
                               "with hyphens only inside a segment (W3).") from None
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
        _check_declared(name, folder, declared)
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
        self.declared[declared.name] = declared
        self.loaded.add(declared.name)

    def _loaded(self, declared):
        """Run what the plugin asked to run once it is loaded (Plugin.on_load), as the system owner."""
        system = self.hub.auth.system
        for fn in declared.loaders:
            try:
                fn(Call(self.hub, declared.name, {"user": system, "token": None}, system, None))
            except Exception:
                log.exception("Plugin %s: what it runs once loaded failed", declared.name)

    # ----------------------------------------------------------- install

    def install(self, source, actor):
        """Install, or replace, the plugin in folder `source` (PL4). All or nothing. `actor`
        is who installs it; None only for the hub's own default plugins (install_defaults)."""
        source = pathlib.Path(source).expanduser().resolve()
        manifest = read_manifest(source)
        name = manifest["name"]
        if actor is not None:
            with self.hub._lock, self.hub._db:
                self.hub.auth.actor(**actor)
        self.root.mkdir(exist_ok=True, mode=0o700)
        folder = self.root / f"{name}.{uuid.uuid4().hex[:8]}"
        module_name = None
        from . import owner_only     # __init__.py imports this module first
        try:
            shutil.copytree(source, folder, ignore=shutil.ignore_patterns("__pycache__"))
            owner_only(folder)          # the copy kept the source's modes; the hub's data is owner-only
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
        self._loaded(declared)
        if old is not None and old["folder"] != folder.name:
            shutil.rmtree(self.root / old["folder"], ignore_errors=True)   # the replaced code; one copy per plugin
        self.push()
        return self.get(name)

    def install_defaults(self):
        """At every start, install core's own plugins (PL7) in name order: each one not installed,
        and each one whose installed version is older than the shipped one. One that fails is
        logged and skipped, as load_all does, and what was installed before stays."""
        for folder in sorted(DEFAULTS.iterdir()):
            if not (folder / MANIFEST).is_file():
                continue
            shipped = read_manifest(folder)
            with self.hub._lock:
                row = self.hub._db.execute("SELECT version FROM plugins WHERE name = ?",
                                           (shipped["name"],)).fetchone()
            if row is not None and not older(row["version"], shipped["version"]):
                continue
            try:
                self.install(folder, None)
            except HubError as e:
                log.error("Plugin %s %s was not installed: %s %s", shipped["name"], shipped["version"],
                          e.message["message"], e.message["next"])

    def load_all(self):
        """Load every installed plugin, as the hub starts. One that fails is logged and skipped."""
        with self.hub._lock:
            rows = self.hub._db.execute("SELECT * FROM plugins ORDER BY installed_at, rowid").fetchall()
        for row in rows:
            try:
                declared, module_name = self._load(row["name"], self.root / row["folder"], json.loads(row["manifest"]))
                self._clashes(declared)
                self._serve(declared, module_name)
                self._loaded(declared)
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
        loaded, failed = [], []
        for w in self.hub.list_workers():
            for part in w["plugins"] or []:
                if part["name"] == row["name"]:
                    (failed if "error" in part else loaded).append(
                        {"worker": w["moniker"], **{k: v for k, v in part.items() if k != "name"}})
        return {"guid": row["guid"], "moniker": row["moniker"], "name": row["name"], "version": row["version"],
                "installed_at": iso(row["installed_at"]),
                "caps": sorted(cap for cap, entry in self.caps.items() if entry[0] == row["name"]),
                "record_types": types,
                "drivers": sorted(d for d, entry in self.drivers.items() if entry[0] == row["name"]),
                "worker": "worker" in manifest, "site": "site" in manifest, "skill": "skill" in manifest,
                "loaded_on": loaded, "failed_on": failed}

    def list(self):
        with self.hub._lock:
            names = [r[0] for r in self.hub._db.execute("SELECT name FROM plugins ORDER BY installed_at, rowid")]
        return [self.get(name) for name in names]

    # ------------------------------------------------------------ serving

    # ------------------------------------------------- worker parts (PL3)

    def worker_parts(self, guid):
        """The worker parts `guid` should have now: each plugin loaded at the hub with an entry
        for the pushed form, in install order, or none when the worker is opted out (P4)."""
        with self.hub._lock:
            if self.hub._db.execute("SELECT 1 FROM plugin_optouts WHERE worker = ?", (guid,)).fetchone():
                return []
            rows = self.hub._db.execute("SELECT name, version, folder, manifest FROM plugins "
                                        "ORDER BY installed_at, rowid").fetchall()
        parts = []
        for row in rows:
            part = json.loads(row["manifest"]).get("worker", {}).get(PUSHED)
            if part is not None and row["name"] in self.loaded:
                source = (self.root / row["folder"] / part["module"]).read_text("utf-8")
                parts.append({"name": row["name"], "version": row["version"], "source": source})
        return parts

    def push(self, worker=None):
        """Send each connected worker of the pushed form (or only `worker`, a GUID) its worker
        parts, if they differ from what its link was last sent (PL3). The worker loads them."""
        for guid, link in list(self.hub._links.items()):
            if worker is not None and guid != worker:
                continue
            if (self.hub.get_worker(guid)["facts"] or {}).get("form") != PUSHED:
                continue
            parts = self.worker_parts(guid)
            if self._pushed.get(guid) == (link, parts):
                continue
            self._pushed[guid] = (link, parts)
            task = asyncio.get_running_loop().create_task(self._send(link, parts))
            self._sending.add(task)
            task.add_done_callback(self._sending.discard)

    def connected(self, guid, link, reported):
        """A worker's link is up: send it its worker parts, unless it reports holding none
        (`reported`, from its heartbeat) and should have none."""
        self._pushed[guid] = (link, None if reported else [])
        self.push(guid)

    async def _send(self, link, parts):
        try:
            await link.send(encode({"type": "plugins", "plugins": parts}))
        except EOFError:
            pass     # the link closed; the worker gets its list again when it connects

    def set_receives(self, worker, receives, *, user=None, token=None):
        """Whether `worker` receives plugins' worker parts; the default is yes (P4). Journaled (L1).
        An opted-out worker is sent an empty list at once, and drops what it had."""
        with self.hub._lock, self.hub._db:
            actor = self.hub.auth.actor(user, token)
            guid = self.hub._resolve(worker)["guid"]
            if receives:
                self.hub._db.execute("DELETE FROM plugin_optouts WHERE worker = ?", (guid,))
            else:
                self.hub._db.execute("INSERT OR IGNORE INTO plugin_optouts (worker) VALUES (?)", (guid,))
            self.hub._journal("auth", actor, guid, {"receives_plugins": receives})
        self.push(guid)
        return receives

    def opted_out(self):
        """The GUIDs of the workers that receive no plugin worker parts."""
        with self.hub._lock:
            return {row[0] for row in self.hub._db.execute("SELECT worker FROM plugin_optouts")}

    def descriptors(self):
        return [dict(meta, name=cap, returns="result") for cap, (_, _, meta) in sorted(self.caps.items())]

    def call(self, cap, args, actor):
        """Run a plugin's cap. A handler that raises becomes an error naming the plugin (C3)."""
        name, handler, _ = self.caps[cap]
        with self.hub._lock, self.hub._db:
            user, token = self.hub.auth.actor(**actor)
        try:
            result = handler(Call(self.hub, name, actor, user, token), args)
            if inspect.isawaitable(result):     # an async handler: awaited by Hub.call, the hub serving meanwhile
                return self._finish(name, cap, result)
            json.dumps(result)
            return result
        except HubError:
            raise
        except Exception as e:
            log.exception("Plugin %s failed in %s", name, cap)
            raise HubError("plugin_failed", f"Plugin {name} failed in {cap}: {type(e).__name__}: {e}",
                           f"Check the arguments against {cap}'s schema (caps tool); if they are right, "
                           f"plugin {name} needs fixing and installing again.") from None

    async def _finish(self, name, cap, pending):
        """An async handler's result, awaited, as `call` gives a plain one: JSON, or plugin_failed."""
        try:
            result = await pending
            json.dumps(result)
            return result
        except (HubError, asyncio.CancelledError):
            raise
        except Exception as e:
            log.exception("Plugin %s failed in %s", name, cap)
            raise HubError("plugin_failed", f"Plugin {name} failed in {cap}: {type(e).__name__}: {e}",
                           f"Check the arguments against {cap}'s schema (caps tool); if they are right, "
                           f"plugin {name} needs fixing and installing again.") from None

    # ------------------------------------------------ house agent (brief HA2)

    def declared_settings(self, plugin):
        """Plugin `plugin`'s settings as it declared them (Plugin.setting): name -> declaration."""
        declared = self.declared.get(plugin)
        return declared.settings if declared is not None else {}

    def skills(self):
        """Every skill a loaded plugin declares (Plugin.skill): name -> (plugin, file). On a name
        two plugins declare, the one loaded first keeps it."""
        found = {}
        for name, declared in self.declared.items():
            if name in self.loaded:
                for skill, path in declared.skills.items():
                    found.setdefault(skill, (name, path))
        return found

    async def carried(self, actor, user, token):
        """The skills carried now on a turn for this caller (Plugin.carry): [(skill, lead-in, file)]."""
        found = []
        for name, declared in list(self.declared.items()):
            if name not in self.loaded:
                continue
            for skill, when in declared.carried.items():
                try:
                    lead = when(Call(self.hub, name, actor, user, token))
                    if inspect.isawaitable(lead):
                        lead = await lead
                except Exception:
                    log.exception("Plugin %s: the condition carrying skill %s failed", name, skill)
                    continue
                path = declared.skills.get(skill) or self.skills().get(skill, (None, None))[1]
                if lead and path is not None:
                    found.append((skill, str(lead), path))
        return found

    def later(self, plugin, key, fn):
        """Call.later: run `await fn()` after the earlier ones of `plugin` with `key`, one at a time."""
        before = self._queues.get((plugin, key))

        async def run():
            if before is not None and not before.done():
                await asyncio.wait({before})
            return await fn()

        task = asyncio.get_running_loop().create_task(run())
        self._queues[(plugin, key)] = task
        self._later.add(task)

        def ended(task):
            self._later.discard(task)
            if self._queues.get((plugin, key)) is task:
                del self._queues[(plugin, key)]
            if not task.cancelled():
                task.exception()          # retrieved: whoever asked has it, or it was logged by the plugin
        task.add_done_callback(ended)
        return task

    async def stop(self):
        """The hub is stopping: end what plugins left running (Call.later)."""
        tasks = list(self._later)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _check_declared(name, folder, declared):
    """A plugin's settings, skills and carried skills as the library takes them; else HubError."""
    fix = "Declare them as docs/plugins.md shows, then install again."
    for setting, spec in declared.settings.items():
        if not isinstance(setting, str) or not setting or spec["where"] not in ("hub", "person"):
            raise HubError("bad_plugin", f"Plugin {name} declares setting {setting!r} with where {spec['where']!r}: "
                                         "a setting needs a name and where \"hub\" or \"person\".", fix)
        json.dumps(spec["default"])
    root = folder.resolve()
    for skill, path in declared.skills.items():
        where = (folder / path).resolve() if not isinstance(path, pathlib.Path) else path.resolve()
        if not isinstance(skill, str) or not skill or not where.is_relative_to(root) or not where.is_file():
            raise HubError("bad_plugin", f"Plugin {name} declares skill {skill!r} as {path}: it must be a file "
                                         "inside the plugin's folder.", fix)
        declared.skills[skill] = where
    for skill, when in declared.carried.items():
        if not callable(when):
            raise HubError("bad_plugin", f"Plugin {name} carries skill {skill!r} with no condition to call.", fix)
    for fn in declared.loaders:
        if not callable(fn) or inspect.iscoroutinefunction(fn):
            raise HubError("bad_plugin", f"Plugin {name}'s on_load function must be a plain (not async) function.", fix)


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


def older(installed, shipped):
    """Whether version `installed` is older than `shipped`: both dotted numbers such as "0.1.0",
    compared number by number, missing places counting as 0. A version that is not dotted
    numbers is never older: what to do with one is not decided (brief 45, Needs Bake)."""
    if not (_VERSION.match(installed) and _VERSION.match(shipped)):
        return False
    a, b = ([int(n) for n in v.split(".")] for v in (installed, shipped))
    width = max(len(a), len(b))
    return a + [0] * (width - len(a)) < b + [0] * (width - len(b))


# ------------------------------------------------------------ hub caps (PL4)

def install(hub, args, actor):
    path, url = args.get("path"), args.get("url")
    if (path is None) == (url is None) or not isinstance(path or url, str) or not (path or url):
        raise HubError("bad_message", "Give args.path or args.url, one of them, as a string.",
                       "Give the path of the plugin's folder on the hub's disk, or the URL of an archive of it.")
    if url is not None:
        return _install_from(hub, url, actor)
    return {"installed": hub.plugins.install(path, actor)}


async def _install_from(hub, url, actor):
    if not url.startswith(("http://", "https://")):
        raise HubError("bad_message", f"{url} is not an http or https URL.",
                       "Give the URL of a zip or tar archive of the plugin's folder.")
    with hub._lock:
        hub.auth.actor(**actor)
    work = pathlib.Path(tempfile.mkdtemp(prefix="rook-plugin-"))
    try:
        folder = await asyncio.to_thread(fetch, url, work)
        return {"installed": hub.plugins.install(folder, actor), "from": url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fetch(url, work):
    """Download the archive at `url` into the folder `work`, unpack it there and return the plugin's
    folder in it. No timeout and no size limit: it takes as long as it takes (brief 61)."""
    archive = work / "download"
    request = urllib.request.Request(url, headers={"User-Agent": "rook-hub"})
    try:
        with urllib.request.urlopen(request) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out)
    except urllib.error.HTTPError as e:
        raise HubError("plugin_download_failed", f"{url} answered HTTP {e.code} {e.reason}.",
                       "Check the URL in a browser; give one that downloads the archive itself.") from None
    except (OSError, ValueError) as e:
        raise HubError("plugin_download_failed", f"{url} could not be downloaded: {getattr(e, 'reason', e)}.",
                       "Check the URL and that the hub's machine can reach it, then install again.") from None
    unpacked = work / "unpacked"
    unpacked.mkdir()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                _check_member(url, name)
            z.extractall(unpacked)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                _check_member(url, member.name)
                if not (member.isfile() or member.isdir()):
                    raise HubError("plugin_archive_bad", f"The archive at {url} holds {member.name}, a link or "
                                   "device, which a plugin folder never holds.", "Give an archive of plain files.")
            if hasattr(tarfile, "data_filter"):     # Python 3.11.4 and later
                tar.extractall(unpacked, filter="data")
            else:
                tar.extractall(unpacked)            # its members were checked above: plain files and folders inside it
    else:
        raise HubError("plugin_archive_bad", f"What {url} gives is not a zip or tar archive.",
                       "Give the URL of a .zip or .tar.gz of the plugin's folder (a release asset, or a branch archive).")
    if (unpacked / MANIFEST).is_file():
        return unpacked
    inside = [p for p in unpacked.iterdir() if not p.name.startswith(".")]
    if len(inside) == 1 and (inside[0] / MANIFEST).is_file():
        return inside[0]
    raise HubError("plugin_archive_bad", f"The archive at {url} has no {MANIFEST} at its top or in its one top folder.",
                   f"Give an archive of the plugin's folder, the one that holds {MANIFEST}.")


def _check_member(url, name):
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise HubError("plugin_archive_bad", f"The archive at {url} holds {name}, outside its own folder.",
                       "Give an archive of the plugin's folder only.")


def list_plugins(hub, args, actor):
    return {"plugins": hub.plugins.list()}


def receives(hub, args, actor):
    worker, receive = args.get("worker"), args.get("receive")
    if not isinstance(worker, str) or not worker or not isinstance(receive, bool):
        raise HubError("bad_message", "args.worker (a moniker or GUID) and args.receive (true or false) are required.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    moniker = hub.get_worker(worker)["moniker"]
    return {"worker": moniker, "receives_plugins": hub.plugins.set_receives(worker, receive, **actor)}


CAPS = {
    "plugin.install": (install, {
        "description": "Install a plugin from a folder on the hub's disk or from a URL, or replace it if one by "
                       "that name is installed; it loads at once. Give path or url.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string", "description": "The plugin's folder on the hub's disk, holding plugin.json."},
            "url": {"type": "string", "description": "An http or https URL of a zip or tar archive of the plugin's "
                                                     "folder (plugin.json at its top or in its one top folder)."}}},
    }),
    "plugin.list": (list_plugins, {
        "description": "List the installed plugins: version, the caps and record types each added, whether "
                       "it has a worker section, a site section and a skill, and which workers loaded its worker "
                       "part or failed to.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }),
    "worker.plugins": (receives, {
        "description": "Opt a worker out of plugins' worker parts, or back in; it drops or loads them at once.",
        "schema": {"type": "object", "required": ["worker", "receive"], "additionalProperties": False, "properties": {
            "worker": {"type": "string", "description": "The worker's moniker or GUID."},
            "receive": {"type": "boolean", "description": "false to opt it out, true to opt it back in."}}},
    }),
}


# ------------------------------------------------------------ the site section (PL1, P1, P2, P6)
#
# "site": {"pages": [{"segment", "title", "file"}], "settings": [{"title", "file"}],
#          "components": [{"name", "file"}], "shell": [{"file"}]}   (docs/plugins.md; Bake 13 Nov)
# Each page, settings or component file is an ES module in the plugin's folder whose default
# export is a Vue component. A shell file (house agent brief HA1, as 0.5's manifest `shell:`)
# is a stylesheet (.css) or a classic script (.js) the shell loads on every page, in order.
# A page's name is its title in lowercase, words joined by hyphens: the plugin's page `notes`
# is at /plugins/<plugin>/notes, so a pin names the plugin and the page (Bake 13 Nov).

SITE_FIELDS = {"pages": ("segment", "title", "file"), "settings": ("title", "file"), "components": ("name", "file"),
               "shell": ("file",)}
SHELL_KINDS = {".css": "style", ".js": "script"}         # what a shell file may be, by its ending
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
            if part == "shell" and target.suffix not in SHELL_KINDS:
                raise HubError("bad_manifest", f"{where}: file {entry['file']!r} must end in .css (a stylesheet) "
                                              "or .js (a script).", fix)
            name = {"components": entry.get("name"), "shell": site_file(folder, entry["file"])}.get(part) \
                or page_name(entry["title"])
            if not name or name in names:
                raise HubError("bad_manifest", f"{where}: {'name' if part == 'components' else 'file' if part == 'shell' else 'title'} "
                                              f"{entry.get('title') or entry.get('name') or entry.get('file')!r} is empty or used twice in this plugin.", fix)
            names.add(name)


def site_file(folder, file):
    """A manifest's file name as the site serves it: its path in the folder, with forward slashes."""
    return (folder / file).resolve().relative_to(folder.resolve()).as_posix()


def site_parts(hub):
    """Every installed plugin's site section, in install order: [(plugin name, installed folder, site)]."""
    with hub._lock:
        rows = hub._db.execute("SELECT name, folder, manifest FROM plugins ORDER BY installed_at, rowid").fetchall()
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
