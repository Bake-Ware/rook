"""The plugin library (PL6): what every plugin's hub code repeats, in one place.

A plugin's hub module defines `setup(plugin)`. The hub calls it when it loads
the plugin, with a `Plugin`: through it the plugin declares its caps and its
record types. Each cap's handler is then called as `handler(call, args)` with a
`Call`, which says who is calling (user and token) and is the plugin's only way
to records, blobs and the event bus.

    from rook_hub.library import Error

    def setup(plugin):
        plugin.record_type("note")

        @plugin.cap("note.create", "Make a note.", {"type": "object", ...})
        def create(call, args):
            return call.create("note", args["title"], body={"text": args["text"]})

Through a `Call` a plugin reaches only records of the types it owns, plus blobs
(K2, K3). Nothing here touches routing, auth or the journal (PL2): a plugin's
cap is journaled like every hub cap, because it is called the same way.

A plugin may also declare a driver for a kind of device (D2, PL1). A driver's
handlers are `async`, because each network step goes to a worker and back:

    def setup(plugin):
        lamp = plugin.driver("lamp")

        @lamp.cap("lamp.on", "Turn the lamp on.", {"type": "object"})
        async def on(device, args):
            return await device.http(device.config["url"] + "/on", method="POST")

        @lamp.status
        async def status(device):
            return (await device.http(device.config["url"] + "/state"))["body"]

A handler gets a `Device`: its config (with secret:<name> resolved, D7) and the
three network primitives (D3). It never names a worker; the hub picks the route (D4).
"""

import asyncio
import base64
import json
import uuid

from . import records
from .errors import HubError

# A plugin raises Error(code, message, next_step) to refuse a call with an error
# that says what to try next (C3).
Error = HubError


class Plugin:
    """What a plugin declares when it loads: its caps and its record types, and (house agent,
    brief HA2) its settings, its skills, the skills it carries on an agent's turn while a
    condition holds, and what runs once it is loaded."""

    def __init__(self, name, folder=None, data=None):
        self.name = name
        self.folder = folder      # the installed copy of the plugin's folder (pathlib.Path)
        self._data = data         # the plugin's own data folder at the hub (brief MEM5), made on first use
        self.caps = {}            # cap name -> (handler, {"description": ..., "schema": ...})
        self.record_types = []
        self.drivers = {}         # driver name -> Driver
        self.settings = {}        # setting name -> {"doc", "where": "hub" | "person", "default"}
        self.skills = {}          # skill name -> its file (a pathlib.Path inside the folder)
        self.carried = {}         # skill name -> when(call): the lead-in while it is carried, else None
        self.loaders = []         # fn(call), run each time the plugin is loaded, as the system owner

    def cap(self, name, description, schema):
        """Declare a cap served by the hub, called with worker "hub" (W3). Use as a
        decorator on `handler(call, args)`, which returns any JSON value, or on an
        `async` handler, which the hub awaits while it goes on serving."""
        def register(handler):
            self.caps[name] = (handler, {"description": description, "schema": schema})
            return handler
        return register

    @property
    def data(self):
        """A folder of the plugin's own in the hub's data directory (`<data>/plugin-data/<plugin>`, brief
        MEM5), as WORKER.data is on a worker: made on first use, owner-only, kept across restarts and
        installs, and carried by backup.run (a SQLite file in it is copied consistently)."""
        return _made(self._data)

    def setting(self, name, doc, where="hub", default=None):
        """Declare a setting the plugin keeps at the hub (brief HA2; 0.5's plugin settings).
        `where`: "hub" (one value for the hub) or "person" (each user's own). A setting never
        set reads as `default`, None unless given: 0.5's "not chosen yet". A handler reads
        and writes it through `call.settings`; agents and the site through setting.list and
        setting.set with `plugin`."""
        self.settings[name] = {"doc": doc, "where": where, "default": default}

    def skill(self, name, file):
        """Declare a skill: a markdown file in the plugin's folder that teaches an agent something
        (PL5), with an optional front matter of `description:` and `when:` lines. agent.skill
        reads it, and an agent's prompt lists it by its description."""
        self.skills[name] = self.folder / file if self.folder is not None else file

    def carry(self, name):
        """Decorate `when(call)` (sync or async): while it returns text, every turn of the house
        agent carries skill `name` in full, after that text (0.5's setup brief, which first-run
        setup uses). `call` is as the person whose message the turn answers. None: not carried."""
        def register(when):
            self.carried[name] = when
            return when
        return register

    def on_load(self, fn):
        """Decorate `fn(call)`, run once each time the plugin is loaded (at every hub start, and on
        every install or replace), after its caps are served. `call` is as the hub's system owner.
        It runs while the hub starts, so it is not async; one that raises is logged."""
        self.loaders.append(fn)
        return fn

    def record_type(self, type):
        """Declare a record type this plugin owns (K2)."""
        self.record_types.append(type)

    def driver(self, name):
        """Declare a driver: what the hub uses to speak to one kind of device (D2)."""
        self.drivers[name] = Driver(name)
        return self.drivers[name]


def _made(folder):
    for f in reversed([folder, *folder.parents][:2]):     # plugin-data/ and the plugin's own: owner-only, as the rest
        f.mkdir(mode=0o700, exist_ok=True)
    return folder


class Driver:
    """The caps a kind of device has, and optionally how to read its status (D1, D6)."""

    def __init__(self, name):
        self.name = name
        self.caps = {}            # cap name -> (async handler(device, args), {"description": ..., "schema": ...})
        self.status_handler = None

    def cap(self, name, description, schema):
        """Declare a cap of this kind of device (W3), on `async handler(device, args)`."""
        def register(handler):
            self.caps[name] = (handler, {"description": description, "schema": schema})
            return handler
        return register

    def status(self, handler):
        """Declare `async handler(device)`, which reads the device's status; the hub polls it (D6)."""
        self.status_handler = handler
        return handler


class Device:
    """One device, as its driver sees it during one call or poll."""

    def __init__(self, guid, moniker, name, config, send):
        self.guid, self.moniker, self.name = guid, moniker, name
        self.config = config                     # secret:<name> already resolved (D7, X2)
        self._send = send                        # the hub's route: send(cap, args) -> the primitive's answer

    async def http(self, url, method="GET", headers=None, body=None, wait=None):
        """One HTTP request (net.http): {status, headers, body or body_base64}."""
        args = {"url": url, "method": method, "headers": headers or {}, **_bytes("body", body)}
        return await self._send("net.http", dict(args, **({"wait": wait} if wait is not None else {})))

    async def tcp(self, host, port, data, wait):
        """One TCP send (net.tcp): {data or data_base64}, what came back within `wait` seconds."""
        return await self._send("net.tcp", {"host": host, "port": port, "wait": wait, **_bytes("data", data)})

    async def udp(self, host, port, data, wait):
        """One UDP send (net.udp): {data or data_base64}, the first reply within `wait` seconds, or data null."""
        return await self._send("net.udp", {"host": host, "port": port, "wait": wait, **_bytes("data", data)})


def _bytes(name, value):
    if value is None:
        return {}
    if isinstance(value, str):
        return {name: value}
    return {name + "_base64": base64.b64encode(value).decode("ascii")}


class Call:
    """One call of a plugin cap: who is calling, and the plugin's records, blobs and events."""

    def __init__(self, hub, plugin, actor, user, token):
        self._hub, self.plugin = hub, plugin
        self._actor = actor                      # as the hub got it: {"user": GUID or None, "token": JWT or None}
        self.user, self.token = user, token      # the caller's user GUID, and token GUID or None

    def _owned(self, type):
        if type == records.BLOB:
            return
        row = self._hub._db.execute("SELECT plugin FROM record_types WHERE type = ?", (type,)).fetchone()
        if row is None or row["plugin"] != self.plugin:
            owner = f"it belongs to plugin {row['plugin']!r}" if row else "no plugin has declared it"
            raise HubError("type_not_owned", f"Plugin {self.plugin!r} does not own record type {type!r}: {owner}.",
                           f"Reach {type} records through the caps of the plugin that owns them, or declare a "
                           f"record type of your own in setup().")

    def _check(self, record):
        """The record if this plugin may reach it, else HubError."""
        with self._hub._lock:
            self._owned(records._row(self._hub, record)["type"])
        return record

    def _visible(self, found):
        with self._hub._lock:
            owned = {row[0] for row in self._hub._db.execute(
                "SELECT type FROM record_types WHERE plugin = ?", (self.plugin,))} | {records.BLOB}
        return [r for r in found if r["type"] in owned]

    def has_users(self):
        """Whether any person is a user of this hub yet (not the system owner). Brief HA3: the agents
        plugin's first load on a data directory tells a new hub (none: it starts in setup) from one made
        before setup existed (it has people: setup counts as done)."""
        with self._hub._lock:
            return self._hub.auth.has_users()

    @property
    def data(self):
        """The plugin's own data folder at the hub (Plugin.data; brief MEM5)."""
        return _made(self._hub.state_dir / "plugin-data" / self.plugin)

    def acting(self):
        """How the caller came in (brief MEM5: who said it): {"token": {"moniker", "purpose"} of the token they
        came with (purpose "session" for a person's own site session, "agent" for an agent's), or None for a
        person acting directly; "job": the moniker of the job whose run this call is, or None}."""
        token = job = None
        with self._hub._lock:
            if self.token is not None:
                row = self._hub._db.execute("SELECT moniker, purpose FROM tokens WHERE guid = ?", (self.token,)).fetchone()
                token = {"moniker": row["moniker"], "purpose": row["purpose"]} if row else None
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            running = next((r for r in self._hub._running.values() if task is not None and r["task"] is task), None)
            if running and running.get("job"):
                row = self._hub._db.execute("SELECT moniker FROM jobs WHERE guid = ?", (running["job"],)).fetchone()
                job = row["moniker"] if row else running["job"]
        return {"token": token, "job": job}

    def install_facts(self):
        """What the installer wrote about this hub in `<data>/install.json` (brief HA4: its folder, units,
        port, AI engine), as a dict; None for a hub no installer made, or a file that is not a JSON
        object. Brief HA3: agent.setup gives it to the house agent as `install`, as 0.5's setup.json did."""
        try:
            facts = json.loads((self._hub.state_dir / "install.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return facts if isinstance(facts, dict) else None

    # ------------------------------------------------------------ records

    def create(self, type, title, description="", body=None, *, owner=None):
        """A record of a type this plugin owns; its creator is the caller (K1)."""
        with self._hub._lock:
            self._owned(type)
        return records.create(self._hub, type, title, description, body, owner=owner, **self._actor)

    def get(self, record):
        return records.get(self._hub, self._check(record))

    def update(self, record, *, title=None, description=None, body=None):
        return records.update(self._hub, self._check(record), title=title, description=description, body=body)

    def delete(self, record):
        records.delete(self._hub, self._check(record))

    def search(self, type, text=None, limit=50):
        """Records of one type this plugin owns (or blobs), optionally holding every word of `text`."""
        with self._hub._lock:
            self._owned(type)
        return records.search(self._hub, type=type, text=text, limit=limit)

    def link(self, source, target):
        records.link(self._hub, self._check(source), self._check(target))

    def unlink(self, source, target):
        records.unlink(self._hub, self._check(source), self._check(target))

    def links(self, record):
        """`to` and `from` links, keeping only records this plugin may reach."""
        found = records.links(self._hub, self._check(record))
        return {"to": self._visible(found["to"]), "from": self._visible(found["from"])}

    def store_blob(self, data, mime, title="", description="", *, owner=None):
        return records.store_blob(self._hub, data, mime, title, description, owner=owner, **self._actor)

    def read_blob(self, record):
        return records.read_blob(self._hub, record)

    # ------------------------------------------------------------- events

    def emit(self, name, payload=None):
        """Put an event on the bus (PL1, J2), as the caller."""
        self._hub.events.emit(name, payload or {}, user=self.user, token=self.token)

    # ----------------------------------------------- house agent (brief HA2)

    @property
    def settings(self):
        """This plugin's settings (Plugin.setting): `settings[name]` or `settings.get(name)`
        reads one, `settings.set(name, value)` writes it. A where="person" setting is the
        caller's own."""
        return Settings(self._hub, self.plugin, self.user)

    def as_caller(self, user, token=None):
        """The same plugin's Call as someone else: `user` a user GUID acting directly, or
        `token` a JWT (then `user` is ignored)."""
        actor = {"user": None, "token": token} if token is not None else {"user": user, "token": None}
        with self._hub._lock, self._hub._db:
            got_user, got_token = self._hub.auth.actor(**actor)
        return Call(self._hub, self.plugin, actor, got_user, got_token)

    async def call(self, worker, cap, args, why):
        """Call `cap` on `worker` (or "hub") as the caller, as any call is made (L1: journaled
        under them with `why`), and return its result; a result that came in pieces comes
        joined. An error reply raises HubError with that error. There is no timeout."""
        return self._result(await self._hub.call(worker, cap, args, why, **self._actor))

    async def start(self, worker, cap, args, why):
        """Start a call as `call` makes it, and give it a stream at once (brief 59), so whoever
        closes that stream stops the call. Returns (the stream's moniker or None if the call
        ended before it began, an awaitable of the result as `call` gives it)."""
        call_id = uuid.uuid4().hex
        task = asyncio.ensure_future(self._hub.call(worker, cap, args, why, call_id=call_id, **self._actor))
        await asyncio.sleep(0)               # the call begins, and is running until it ends
        running = self._hub._running.get(call_id)
        stream = self._hub._follow(call_id, running)["moniker"] if running and running["worker"] else None

        async def result():
            return self._result(await task)
        return stream, result()

    def _result(self, reply):
        if reply["type"] == "error":
            raise HubError(reply["code"], reply["message"], reply["next"])
        if reply["type"] == "stream_open":
            return {name: reply[name] for name in ("stream", "title", "description")}
        return self._hub.joined(reply)

    def later(self, key, fn):
        """Run `await fn()` on the hub, after every earlier `later` of this plugin with the same
        `key` has ended: one at a time, in the order asked. It goes on after the cap that asked
        returns, until it ends or the hub stops. Returns an asyncio.Task of its result."""
        return self._hub.plugins.later(self.plugin, key, fn)

    def open_stream(self, title, description):
        """Open a stream at the hub that this plugin writes (S1 to S3: a title and a description,
        free text). Clients read it as any stream; its input comes only through the plugin's
        own caps. It is kept until closed (S4). Returns its record (guid, moniker, ...)."""
        return self._hub.streams.open_for_plugin(self.plugin, title, description, (self.user, self.token))

    def append(self, stream, data, *, by_caller=False):
        """Add `data` (str or bytes) to a stream this plugin opened, as its next entry. With
        `by_caller`, it is recorded as input from the caller (direction in); otherwise as the
        plugin's own output (out). Returns the entry's position (0, 1, 2 ...)."""
        data = data.encode("utf-8") if isinstance(data, str) else data
        return self._hub.streams.plugin_append(self.plugin, stream, data, (self.user, self.token) if by_caller
                                               else (None, None))

    async def read(self, stream, position=0, wait=0, limit=None):
        """Entries of a stream from `position` on, as the stream tool reads them raw (bytes as
        base64 in `data`), waiting up to `wait` seconds for one, however long that is; `limit`
        None: all of them."""
        return await self._hub.streams.read_all(stream, position, wait, limit)

    def busy(self, key):
        """Whether anything this plugin asked of `later` with `key` is still waiting or running."""
        return (self.plugin, key) in self._hub.plugins._queues

    def person(self, who=None):
        """A user: the caller (None), or one named by GUID, moniker or name. {guid, moniker, name}."""
        auth = self._hub.auth
        if who is None:
            return {k: v for k, v in auth.get_user(self.user).items() if k in ("guid", "moniker", "name")}
        with self._hub._lock:
            row = self._hub._db.execute("SELECT guid FROM users WHERE guid = ? OR moniker = ? OR name = ?",
                                        (who, who, who)).fetchone()
        if row is None:
            raise HubError("unknown_user", f"No user is called {who!r}.",
                           "List the users with users.list and use a name, moniker or GUID from there.")
        return {k: v for k, v in auth.get_user(row["guid"]).items() if k in ("guid", "moniker", "name")}

    def workers(self):
        """Every worker as the workers tool lists it: guid, moniker, name, online and the caps it announces."""
        return [{"guid": w["guid"], "moniker": w["moniker"], "name": w["name"], "online": w["online"],
                 "caps": [c["name"] for c in w["caps"]]} for w in self._hub.list_workers()]

    def skills(self):
        """Every skill a loaded plugin declares: name -> (plugin, file)."""
        return self._hub.plugins.skills()

    async def carried(self):
        """The skills carried on this caller's turn now (Plugin.carry): [(name, lead-in, file)],
        each condition asked as the caller, in its own plugin. One that raises is left out and
        logged."""
        return await self._hub.plugins.carried(self._actor, self.user, self.token)


class Settings:
    """A plugin's settings as one caller sees them (Plugin.setting, Call.settings). Kept at the
    hub in table plugin_settings, a value as JSON; a where="person" setting per user."""

    def __init__(self, hub, plugin, user):
        self._hub, self._plugin, self._user = hub, plugin, user

    def _declared(self, name):
        declared = self._hub.plugins.declared_settings(self._plugin)
        if name not in declared:
            raise HubError("unknown_setting", f"Plugin {self._plugin} has no setting {name!r}.",
                           f"Its settings are {', '.join(declared) or 'none'}; setting.list shows them.")
        return declared[name]

    def _scope(self, name):
        if self._declared(name)["where"] == "hub":
            return ""
        if self._user is None:
            raise HubError("no_person", f"Setting {name} of plugin {self._plugin} is each person's own; "
                                        "nobody is calling.", "Set it as the person it belongs to.")
        return self._user

    def get(self, name, default=None):
        """The value, or the declared default (else `default`) when it was never set."""
        scope = self._scope(name)
        with self._hub._lock:
            row = self._hub._db.execute("SELECT value FROM plugin_settings WHERE plugin = ? AND scope = ? AND name = ?",
                                        (self._plugin, scope, name)).fetchone()
        if row is None:
            declared = self._declared(name)["default"]
            return declared if declared is not None else default
        return json.loads(row["value"])

    __getitem__ = get

    def set(self, name, value):
        """Keep `value` (any JSON value; None forgets it, so it reads as never set)."""
        scope = self._scope(name)
        with self._hub._lock, self._hub._db:
            if value is None:
                self._hub._db.execute("DELETE FROM plugin_settings WHERE plugin = ? AND scope = ? AND name = ?",
                                      (self._plugin, scope, name))
            else:
                self._hub._db.execute(
                    "INSERT INTO plugin_settings (plugin, scope, name, value, set_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (plugin, scope, name) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
                    (self._plugin, scope, name, json.dumps(value), self._hub.clock()))
        return value

    def chosen(self, name):
        """Whether it was ever set (0.5: blank means "not chosen yet")."""
        scope = self._scope(name)
        with self._hub._lock:
            return self._hub._db.execute("SELECT 1 FROM plugin_settings WHERE plugin = ? AND scope = ? AND name = ?",
                                         (self._plugin, scope, name)).fetchone() is not None


SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_settings (   -- plugins' settings (Plugin.setting; brief HA2)
    plugin TEXT NOT NULL,
    scope  TEXT NOT NULL,          -- '' for the hub's value, else the user GUID whose own it is
    name   TEXT NOT NULL,
    value  TEXT NOT NULL,          -- JSON
    set_at REAL NOT NULL,
    PRIMARY KEY (plugin, scope, name)
);
"""
