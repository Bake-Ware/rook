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

import base64

from . import records
from .errors import HubError

# A plugin raises Error(code, message, next_step) to refuse a call with an error
# that says what to try next (C3).
Error = HubError


class Plugin:
    """What a plugin declares when it loads: its caps and its record types."""

    def __init__(self, name):
        self.name = name
        self.caps = {}            # cap name -> (handler, {"description": ..., "schema": ...})
        self.record_types = []
        self.drivers = {}         # driver name -> Driver

    def cap(self, name, description, schema):
        """Declare a cap served by the hub, called with worker "hub" (W3). Use as a
        decorator on `handler(call, args)`, which returns any JSON value."""
        def register(handler):
            self.caps[name] = (handler, {"description": description, "schema": schema})
            return handler
        return register

    def record_type(self, type):
        """Declare a record type this plugin owns (K2)."""
        self.record_types.append(type)

    def driver(self, name):
        """Declare a driver: what the hub uses to speak to one kind of device (D2)."""
        self.drivers[name] = Driver(name)
        return self.drivers[name]


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
