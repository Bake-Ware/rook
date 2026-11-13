"""Bands (B1-B6): a band is a physical network, in one sentence a router.

A band is a set of gateway MAC addresses that a user has named (B2). A worker
is in the band that holds the gateway MAC of its latest heartbeat, so
membership follows the worker on its own (B1) and is one band at a time (B6).
A worker whose gateway MAC is in no band, or that reported none (cellular), is
roaming (B3). The hub never makes a band or guesses one: it lists the gateway
MACs nobody has named, so a person can be asked to add one (B2).

Matching is exact on the gateway MAC and nothing else; MACs are compared in
lowercase, as the hub keeps every MAC. A gateway belongs to at most one band,
which is what keeps a worker in one band (B6). Bands carry no permissions:
nothing anywhere checks a band before allowing something (B4).
"""

import json
import uuid

from .errors import HubError

SCHEMA = """
CREATE TABLE IF NOT EXISTS bands (
    guid       TEXT PRIMARY KEY,
    moniker    TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS band_gateways (
    mac      TEXT PRIMARY KEY,           -- lowercase; one band per gateway (B6)
    band     TEXT NOT NULL REFERENCES bands(guid),
    added_at REAL NOT NULL
);
"""


def _text(value, what, example):
    if not isinstance(value, str) or not value:
        raise HubError("bad_message", f"{what} is required and must be a string.", f"Give {what} like {example}.")
    return value


def _band(hub, band):
    """A band row by GUID or moniker (G1). The lock is held."""
    row = hub._db.execute("SELECT * FROM bands WHERE guid = ? OR moniker = ?", (band, band)).fetchone()
    if row is None:
        raise HubError("unknown_band", f"No band is called {band!r}.",
                       "List the bands and use a GUID or moniker from there.")
    return row


def _record(hub, row):
    gateways = [r[0] for r in hub._db.execute(
        "SELECT mac FROM band_gateways WHERE band = ? ORDER BY added_at, mac", (row["guid"],))]
    return {"guid": row["guid"], "moniker": row["moniker"], "name": row["name"],
            "created_at": row["created_at"], "gateways": gateways}


def _summary(band):
    return None if band is None else {"guid": band["guid"], "moniker": band["moniker"], "name": band["name"]}


def _change(hub, worker_row, before, after):
    """Emit band.change (J2) for one worker, from band `before` to band `after` (None: roaming)."""
    hub.events.emit("band.change", {"worker": worker_row["guid"], "worker_moniker": worker_row["moniker"],
                                    "name": worker_row["name"], "from": _summary(before), "to": _summary(after)},
                    worker=worker_row["guid"])


def moved(hub, worker_row, mac):
    """A heartbeat reports gateway `mac` for the worker whose row (before this heartbeat)
    is `worker_row`: emit band.change if that puts it in another band. The lock is held."""
    known = _known(hub)
    before, after = known.get(_gateway(worker_row)), known.get(mac)
    if (before or {}).get("guid") != (after or {}).get("guid"):
        _change(hub, worker_row, before, after)


def _joined(hub, mac):
    """Workers on gateway `mac` have just gone from roaming into its band: band.change. The lock is held."""
    band = _known(hub)[mac]
    for row in hub._db.execute("SELECT guid, moniker, name, state FROM workers").fetchall():
        if _gateway(row) == mac:
            _change(hub, row, None, band)


def _add_gateway(hub, guid, mac):
    """Put `mac` in band `guid`, unless another band already holds it. The lock is held."""
    taken = hub._db.execute("SELECT b.moniker, b.name FROM band_gateways g JOIN bands b ON b.guid = g.band "
                            "WHERE g.mac = ?", (mac,)).fetchone()
    if taken is not None:
        raise HubError("gateway_taken", f"Gateway {mac} is already in band {taken['name']} ({taken['moniker']}).",
                       "A gateway belongs to one band: use that band, or name a gateway no band holds.")
    hub._db.execute("INSERT INTO band_gateways (mac, band, added_at) VALUES (?, ?, ?)", (mac, guid, hub.clock()))


def create(hub, name, gateway_mac, *, user=None, token=None):
    """Name a band from one gateway MAC (B2). It gets a GUID and a moniker (G1).
    The actor is a user acting directly or a client token (T4); journaled (L1)."""
    name = _text(name, "name", "\"Home\"")
    mac = _text(gateway_mac, "gateway_mac", "02:00:00:00:00:01").lower()
    with hub._lock, hub._db:
        actor = hub.auth.actor(user, token)
        guid = str(uuid.uuid4())
        hub._db.execute("INSERT INTO bands (guid, moniker, name, created_at) VALUES (?, ?, ?, ?)",
                        (guid, hub._new_moniker(guid), name, hub.clock()))
        _add_gateway(hub, guid, mac)
        hub._journal("band", actor, None, {"band_created": guid, "name": name, "gateway_mac": mac})
        _joined(hub, mac)
        return _record(hub, _band(hub, guid))


def add_gateway(hub, band, gateway_mac, *, user=None, token=None):
    """Add another gateway MAC to a band (B2). Journaled (L1)."""
    mac = _text(gateway_mac, "gateway_mac", "02:00:00:00:00:01").lower()
    with hub._lock, hub._db:
        actor = hub.auth.actor(user, token)
        row = _band(hub, band)
        _add_gateway(hub, row["guid"], mac)
        hub._journal("band", actor, None, {"gateway_added": mac, "band": row["guid"]})
        _joined(hub, mac)
        return _record(hub, row)


def rename(hub, band, name, *, user=None, token=None):
    """Rename a band. Its GUID, moniker and gateways stay (G1). Journaled (L1)."""
    name = _text(name, "name", "\"Home\"")
    with hub._lock, hub._db:
        actor = hub.auth.actor(user, token)
        row = _band(hub, band)
        hub._db.execute("UPDATE bands SET name = ? WHERE guid = ?", (name, row["guid"]))
        hub._journal("band", actor, None, {"band_renamed": row["guid"], "from": row["name"], "to": name})
        return _record(hub, _band(hub, row["guid"]))


def get(hub, band):
    with hub._lock:
        return _record(hub, _band(hub, band))


def list_bands(hub):
    with hub._lock:
        return [_record(hub, row) for row in hub._db.execute("SELECT * FROM bands ORDER BY created_at")]


# ------------------------------------------------------------- membership

def _known(hub):
    """{gateway MAC: (guid, moniker, name) of its band}. The lock is held."""
    return {row["mac"]: {"guid": row["guid"], "moniker": row["moniker"], "name": row["name"]}
            for row in hub._db.execute("SELECT g.mac, b.guid, b.moniker, b.name FROM band_gateways g "
                                       "JOIN bands b ON b.guid = g.band")}


def _gateway(worker_row):
    """The gateway MAC in a worker's latest heartbeat, lowercase, or None."""
    state = json.loads(worker_row["state"]) if worker_row["state"] else {}
    mac = (state.get("band") or {}).get("gateway_mac")
    return mac.lower() if mac else None


def membership(hub):
    """{worker GUID: its band (guid, moniker, name), or None when it is roaming} for
    every worker, from its latest heartbeat (B1, B3, B6)."""
    with hub._lock:
        known = _known(hub)
        return {row["guid"]: known.get(_gateway(row)) for row in hub._db.execute("SELECT guid, state FROM workers")}


def of_worker(hub, worker):
    """One worker's band (guid, moniker, name), or None when it is roaming."""
    with hub._lock:
        return _known(hub).get(_gateway(hub._resolve(worker)))


def unrecognised(hub):
    """The gateway MACs workers report in their latest heartbeat that no band holds,
    each with the workers on it, so a person can be asked to add it to a band (B2)."""
    with hub._lock:
        known, found = _known(hub), {}
        for row in hub._db.execute("SELECT guid, moniker, name, state FROM workers ORDER BY approved_at"):
            mac = _gateway(row)
            if mac is not None and mac not in known:
                found.setdefault(mac, []).append({"guid": row["guid"], "moniker": row["moniker"], "name": row["name"]})
        return [{"gateway_mac": mac, "workers": workers} for mac, workers in found.items()]
