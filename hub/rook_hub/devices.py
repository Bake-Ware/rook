"""Devices and delegates (D1 to D8).

A device is anything with an API that cannot host a worker: a TV, a lamp, a
printer. It is a hub object with a GUID and a moniker (G1), and agents use it as
a worker with caps (D1): the `workers` tool lists it, marked as a device; the
`caps` tool lists its caps; the `call` tool calls them by its moniker.

Its caps come from a driver, which a plugin declares (D2, PL1) and which runs at
the hub. A driver never opens a socket: it asks for one of the dumb network
primitives (D3), and the hub carries that step out by the device's route (D4):

- direct: the hub does it itself;
- pinned: through one named worker, always;
- floating: through any eligible worker on the device's band (D5): online, on
  that band, not opted out of serving devices (D8), and announcing the
  primitive. The worker that succeeded last time goes first, then the next
  eligible one; one retry, then an error naming the workers tried.

Each primitive a worker carries out is an ordinary call on that worker, journaled
with a why that names the device, as the user who added the device (L1). Device
config and credentials stay at the hub (D7); a config string that is exactly
`secret:<name>` is kept as written and resolved each time the driver reads the
config, in the device's own scope, then its band's, then the hub's (X1, X2).
Devices have no heartbeat: if the driver can read status, the hub polls it
through the same route at the device's interval and keeps the latest answer and
whether the device was reachable (D6).
"""

import asyncio
import json
import logging
import uuid

import rook_net

from . import bands
from .errors import HubError
from .journal import iso
from .library import Device

log = logging.getLogger(__name__)  # rook_hub.devices: where a driver that raised during a poll is reported

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    guid        TEXT PRIMARY KEY,
    moniker     TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    driver      TEXT NOT NULL,           -- the driver's name (a plugin declares it)
    config      TEXT NOT NULL,           -- JSON object, secret:<name> kept as written (D7)
    band        TEXT,                    -- band GUID; a floating route needs one
    route       TEXT NOT NULL,           -- direct, pinned or floating (D4)
    via         TEXT,                    -- the worker GUID of a pinned route
    interval    INTEGER,                 -- seconds between status polls; NULL: not polled (D6)
    owner       TEXT NOT NULL,           -- the user who added it: polls and primitives act as them
    created_at  REAL NOT NULL,
    last_worker TEXT,                    -- the worker that last carried a primitive out (D5)
    status      TEXT,                    -- the latest status answer, JSON (D6)
    reachable   INTEGER,                 -- whether the latest poll got through; NULL: never polled
    polled_at   REAL
);
CREATE TABLE IF NOT EXISTS device_optouts (  -- workers that do not serve devices (D8); kept at the hub (W4)
    worker TEXT PRIMARY KEY REFERENCES workers(guid)
);
"""

ROUTES = ("direct", "pinned", "floating")
PRIMITIVES = {name: function for name, (function, _, _) in rook_net.CAPS.items()}   # net.http, net.tcp, net.udp


# ------------------------------------------------------------- opt-out (D8)

def set_serves_devices(hub, worker, serves, *, user=None, token=None):
    """Whether `worker` may carry out primitives for devices. The default is yes (D8).
    The actor is a user acting directly or a client token or site session (T4); journaled (L1)."""
    with hub._lock, hub._db:
        actor = hub.auth.actor(user, token)
        guid = hub._resolve(worker)["guid"]
        if serves:
            hub._db.execute("DELETE FROM device_optouts WHERE worker = ?", (guid,))
        else:
            hub._db.execute("INSERT OR IGNORE INTO device_optouts (worker) VALUES (?)", (guid,))
        hub._journal("auth", actor, guid, {"serves_devices": serves})
    return serves


def opted_out(hub):
    """The GUIDs of the workers that do not serve devices."""
    with hub._lock:
        return {row[0] for row in hub._db.execute("SELECT worker FROM device_optouts")}


# ---------------------------------------------------------------- devices

def find(hub, device):
    """A device row by GUID or moniker (G1), or None."""
    with hub._lock:
        return hub._db.execute("SELECT * FROM devices WHERE guid = ? OR moniker = ?", (device, device)).fetchone()


def _driver(hub, row):
    entry = hub.plugins.drivers.get(row["driver"])
    if entry is None:
        raise HubError("driver_missing", f"Device {row['moniker']} needs driver {row['driver']}, which no installed "
                                         "plugin declares.", f"Install the plugin that declares driver {row['driver']}.")
    return entry


def _caps(hub, row):
    entry = hub.plugins.drivers.get(row["driver"])
    return [] if entry is None else [dict(meta, name=cap, returns="result")
                                     for cap, (_, meta) in sorted(entry[1].caps.items())]


def record(hub, row):
    """A device as agents see it: a worker with caps, marked as a device (D1)."""
    band = bands.get(hub, row["band"]) if row["band"] else None
    route = {"kind": row["route"]}
    if row["via"]:
        route["via"] = hub._moniker(row["via"])
    return {"moniker": row["moniker"], "guid": row["guid"], "name": row["name"], "device": True,
            "driver": row["driver"], "owner": row["owner"],
            "band": {name: band[name] for name in ("guid", "moniker", "name")} if band else None,
            "route": route, "interval": row["interval"],
            "reachable": None if row["reachable"] is None else bool(row["reachable"]),
            "status": json.loads(row["status"]) if row["status"] is not None else None,
            "polled_at": iso(row["polled_at"]), "caps": _caps(hub, row)}


def list_devices(hub):
    with hub._lock:
        rows = hub._db.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
    return [record(hub, row) for row in rows]


def add(hub, args, actor):
    """The hub cap device.add (D1, D4, D6, D7): a new device with its driver, config, band, route and interval."""
    def need(name, kind, what):
        value = args.get(name)
        if not isinstance(value, kind) or (kind is str and not value):
            raise HubError("bad_message", f"args.{name} is required and must be {what}.",
                           "Call again with the arguments in device.add's schema (see the caps tool).")
        return value
    name, driver, route = need("name", str, "a string"), need("driver", str, "a string"), need("route", str, "a string")
    config = args.get("config", {})
    interval = args.get("interval")
    if not isinstance(config, dict):
        raise HubError("bad_message", "args.config must be an object.", "Give the device's config as a JSON object.")
    if interval is not None and (not isinstance(interval, int) or isinstance(interval, bool) or interval < 1):
        raise HubError("bad_message", "args.interval must be a whole number of seconds, 1 or more.",
                       "Give how often to poll the device's status, or leave interval out.")
    if route not in ROUTES:
        raise HubError("bad_message", f"args.route must be direct, pinned or floating; it is {route!r}.",
                       "Use direct (the hub reaches it), pinned (through one worker) or floating (any worker on its band).")
    if driver not in hub.plugins.drivers:
        known = ", ".join(sorted(hub.plugins.drivers)) or "none yet"
        raise HubError("unknown_driver", f"No installed plugin declares driver {driver!r}.",
                       f"Install the plugin with that driver first. Drivers installed: {known}.")
    guid = str(uuid.uuid4())
    with hub._lock, hub._db:
        user, _ = hub.auth.actor(**actor)
        band = bands._band(hub, args["band"])["guid"] if args.get("band") else None
        via = None
        if route == "floating" and band is None:
            raise HubError("bad_message", "A floating route needs args.band: the band whose workers can reach it.",
                           "Name the device's band (moniker or GUID), or choose route direct or pinned.")
        if route == "pinned":
            if not isinstance(args.get("via"), str) or not args["via"]:
                raise HubError("bad_message", "A pinned route needs args.via: the worker it always goes through.",
                               "Name that worker (moniker or GUID), or choose route direct or floating.")
            via = hub._resolve(args["via"])["guid"]
        hub._db.execute(
            "INSERT INTO devices (guid, moniker, name, driver, config, band, route, via, interval, owner, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guid, hub._new_moniker(guid), name, driver, json.dumps(config), band, route, via, interval, user,
             hub.clock()))
        row = hub._db.execute("SELECT * FROM devices WHERE guid = ?", (guid,)).fetchone()
    return {"added": record(hub, row)}


CAPS = {
    "device.add": (add, {
        "description": "Add a device (a TV, lamp, printer: anything with an API that cannot run a worker); agents "
                       "then call its driver's caps on it by its moniker, like a worker.",
        "schema": {"type": "object", "required": ["name", "driver", "route"], "additionalProperties": False,
                   "properties": {
                       "name": {"type": "string"},
                       "driver": {"type": "string", "description": "A driver an installed plugin declares."},
                       "config": {"type": "object", "description": "What the driver needs (address, keys). A value "
                                  "that is exactly secret:<name> is looked up in the device's scope, its band's, "
                                  "then the hub's."},
                       "band": {"type": "string", "description": "The band it is on (moniker or GUID)."},
                       "route": {"type": "string", "enum": list(ROUTES), "description":
                                 "direct: the hub reaches it; pinned: always through the worker in via; "
                                 "floating: through any worker on its band that serves devices."},
                       "via": {"type": "string", "description": "For a pinned route: the worker (moniker or GUID)."},
                       "interval": {"type": "integer", "minimum": 1,
                                    "description": "Seconds between status polls, if the driver reads status."}}},
    }),
}


# ----------------------------------------------------------------- routes

def _eligible(hub, row, cap):
    """(workers to try, in order; what the others on the band are not) for a floating route (D5)."""
    out = opted_out(hub)
    with hub._lock:
        known = bands._known(hub)
        rows = hub._db.execute("SELECT * FROM workers ORDER BY approved_at").fetchall()
        on_band = [w for w in rows if (known.get(bands._gateway(w)) or {}).get("guid") == row["band"]]
        records = [hub._record(w) for w in on_band]
    eligible, others = [], []
    for w in records:
        if not w["online"] or w["guid"] not in hub._links:
            others.append(f"{w['moniker']} is offline")
        elif w["guid"] in out:
            others.append(f"{w['moniker']} does not serve devices")
        elif cap not in {c["name"] for c in w["caps"]}:
            others.append(f"{w['moniker']} does not announce {cap}")
        else:
            eligible.append(w)
    eligible.sort(key=lambda w: w["guid"] != row["last_worker"])   # the last that succeeded goes first
    return eligible, others


async def _primitive(hub, guid, cap, args, why):
    """Carry out one primitive for device `guid` by its route (D4, D5). Returns its answer, or raises HubError."""
    row = find(hub, guid)
    label = f"{row['name']} ({row['moniker']})"
    if row["route"] == "direct":
        try:
            return await asyncio.to_thread(PRIMITIVES[cap], args)
        except Exception as e:
            raise HubError("device_unreachable", f"The hub could not reach {label}: {type(e).__name__}: {e}",
                           "Check the device's address and that the hub can reach it, then call again.") from None
    if row["route"] == "pinned":
        moniker = hub._moniker(row["via"])
        if row["via"] in opted_out(hub):
            tried = [(moniker, f"{moniker} does not serve devices")]
        else:
            reply = await hub.call(row["via"], cap, args, why, user=row["owner"])
            if reply["type"] == "result":
                return reply["result"]
            tried = [(moniker, f"{moniker}: {reply['message']}")]
        raise HubError("device_unreachable", f"{label} is pinned to {moniker} and could not be reached: "
                                             f"{tried[0][1]}.",
                       f"Bring {moniker} online and let it serve devices, then call again.")
    band = bands.get(hub, row["band"])["name"]
    candidates, others = _eligible(hub, row, cap)
    failures = []
    for worker in candidates[:2]:                              # one retry (D5)
        reply = await hub.call(worker["guid"], cap, args, why, user=row["owner"])
        if reply["type"] == "result":
            with hub._lock, hub._db:
                hub._db.execute("UPDATE devices SET last_worker = ? WHERE guid = ?", (worker["guid"], guid))
            return reply["result"]
        failures.append(f"{worker['moniker']}: {reply['message']}")
    tried = "; ".join(failures) if failures else "no worker was eligible"
    raise HubError("device_unreachable", f"{label} could not be reached on band {band}. Tried: {tried}."
                                         + (f" Not eligible: {'; '.join(others)}." if others else ""),
                   f"Bring a worker on band {band} online and let it serve devices, then call again.")


def _device(hub, row, purpose):
    """The Device a driver's handler gets: resolved config, and primitives sent by the route."""
    config = hub.vault.substitute(json.loads(row["config"]), row["guid"], band=row["band"])
    why = f"Device {row['moniker']} ({row['name']}): {purpose}."

    async def send(cap, args):
        return await _primitive(hub, row["guid"], cap, args, why)
    return Device(row["guid"], row["moniker"], row["name"], config, send)


# ------------------------------------------------------------------ calls

async def call(hub, row, cap, args):
    """Run one of a device's caps through its driver (D1, D2). Returns the result, or raises HubError."""
    plugin, driver = _driver(hub, row)
    if cap not in driver.caps:
        raise HubError("unknown_cap", f"{row['moniker']} does not announce {cap}.",
                       f"List the caps {row['moniker']} announces and call one of those.")
    used = hub.vault.substitute(args, row["guid"], band=row["band"])
    try:
        result = await driver.caps[cap][0](_device(hub, row, cap), used)
        json.dumps(result)
        return result
    except HubError:
        raise
    except Exception as e:
        log.exception("Driver %s (plugin %s) failed in %s", row["driver"], plugin, cap)
        raise HubError("plugin_failed", f"Plugin {plugin} failed in {cap} for {row['moniker']}: "
                                        f"{type(e).__name__}: {e}",
                       f"Check the arguments against {cap}'s schema (caps tool); if they are right, "
                       f"plugin {plugin} needs fixing and installing again.") from None


# ----------------------------------------------------------------- status

def due(hub, now):
    """The devices whose status poll is due (D6), each marked as polled now so it starts once."""
    with hub._lock, hub._db:
        rows = hub._db.execute("SELECT * FROM devices WHERE interval IS NOT NULL AND "
                               "coalesce(polled_at, created_at) + interval <= ?", (now,)).fetchall()
        rows = [row for row in rows if (hub.plugins.drivers.get(row["driver"]) or (None, None))[1] is not None
                and hub.plugins.drivers[row["driver"]][1].status_handler is not None]
        for row in rows:
            hub._db.execute("UPDATE devices SET polled_at = ? WHERE guid = ?", (now, row["guid"]))
    return rows


async def poll(hub, row):
    """Read one device's status through its route and keep it, with whether it was reachable (D6)."""
    status, reachable = None, False
    try:
        _, driver = _driver(hub, row)
        status, reachable = await driver.status_handler(_device(hub, row, "status poll")), True
        json.dumps(status)
    except HubError:
        pass
    except Exception:
        log.exception("Driver %s failed reading the status of %s", row["driver"], row["moniker"])
        reachable = False
    with hub._lock, hub._db:
        if reachable:
            hub._db.execute("UPDATE devices SET status = ?, reachable = 1 WHERE guid = ?",
                            (json.dumps(status), row["guid"]))
        else:
            hub._db.execute("UPDATE devices SET reachable = 0 WHERE guid = ?", (row["guid"],))
