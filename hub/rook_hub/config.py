"""Worker config (CF1 to CF3, K5): one record per worker at the hub, sent to the worker.

Each worker has one record of type `config` (CF1, K1, K5). Its body is
{"worker": <the worker's GUID>, "values": {...}}: the values set for that worker,
kept exactly as written, so a `secret:<name>` in them stays a reference (X2). Only
what someone set is kept. Core declares the type itself, as it declares `blob`
(records.py), and serves its two typed caps, `config.get` and `config.set` (K2),
which reach the record through the plugin library (library.Call), as a plugin's
caps reach its own records. The record is made when the worker is approved, or
at the hub's start for a worker approved before there were config records; its
owner is the worker's owner.

Delivery (CF2, CF3): the hub sends the worker a `config` message with its values,
each `secret:<name>` replaced by the secret's value (the worker's scope, then its
band's, then the hub's; X1), and a `version`, when the worker links and whenever
`config.set` changes them; at a link, a worker that holds no config and should
have none is sent nothing, as for plugins (PL3). The worker applies them and
reports the version it applied in its heartbeats (`config_version`). The values with secrets put in are
only ever sent: never stored, journaled or shown (X2, L4). When they cannot be sent
because a secret cannot be put in, nothing is sent and the worker keeps what it
has: `config.set` says why in its reply, and at a link the hub journals it (kind
`config`).
"""

import asyncio
import hashlib
import json

from rook_protocol import encode

from . import records
from .errors import HubError, error
from .library import Call

TYPE = "config"   # the record type core declares for worker config (CF1, K2)

SCHEMA = "INSERT OR IGNORE INTO record_types (type, plugin) VALUES ('config', 'core');"

_sending = set()  # the sends under way, so none is collected before it is done


def _find(hub, guid):
    """The GUID of worker `guid`'s config record, or None. The lock is held."""
    row = hub._db.execute("SELECT guid FROM records WHERE type = ? AND json_extract(body, '$.worker') = ?",
                          (TYPE, guid)).fetchone()
    return row["guid"] if row else None


def make(hub, guid, actor):
    """Make worker `guid`'s config record, with no values, unless it has one (CF1). `actor`
    is (user, token): its creator, and its owner, who is the worker's owner (W6): the user who
    approved it, or the owner, for a record made at start. The lock and the transaction are held."""
    if _find(hub, guid) is None:
        moniker = hub._db.execute("SELECT moniker FROM workers WHERE guid = ?", (guid,)).fetchone()[0]
        records._create(hub, TYPE, f"Config of {moniker}", "", records._json({"worker": guid, "values": {}}),
                        None, actor)


def make_missing(hub):
    """At start: a config record for every worker without one, made by its owner."""
    with hub._lock, hub._db:
        for row in hub._db.execute("SELECT guid, owner FROM workers").fetchall():
            make(hub, row["guid"], (row["owner"], None))


def version(values):
    """A short name for one set of values as written: the references, never a secret's value."""
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _record(hub, guid):
    """The GUID of worker `guid`'s config record, made if it is missing."""
    with hub._lock, hub._db:
        make(hub, guid, (hub._db.execute("SELECT owner FROM workers WHERE guid = ?", (guid,)).fetchone()[0], None))
        return _find(hub, guid)


def _values(hub, guid):
    return records.get(hub, _record(hub, guid))["body"]["values"]


def deliver(hub, guid):
    """Send worker `guid` its config now, if it is linked (CF2, CF3). Returns None once the
    message is handed to its link, else the `error` that kept it from being sent."""
    link = hub._links.get(guid)
    if link is None:
        moniker = hub.get_worker(guid)["moniker"]
        return error("worker_offline", f"{moniker} is not connected.",
                     f"Nothing to do: {moniker} gets its config when it links.")
    values = _values(hub, guid)
    try:
        message = encode({"type": "config", "values": hub.vault.substitute(values, guid), "version": version(values)})
    except HubError as e:
        return e.message
    task = asyncio.get_running_loop().create_task(_send(link, message))
    _sending.add(task)
    task.add_done_callback(_sending.discard)
    return None


async def _send(link, message):
    try:
        await link.send(message)
    except EOFError:
        pass     # the link closed; the worker gets its config again when it links


def connected(hub, guid, reported):
    """A worker's link is up: send it its config, unless it reports holding none (`reported`,
    the config_version of its first heartbeat, is None) and should have none, as for plugins
    (PL3). What keeps the config from being sent is journaled."""
    if reported is None and not _values(hub, guid):
        return
    problem = deliver(hub, guid)
    if problem is not None:
        with hub._lock, hub._db:
            hub._journal("config", (None, None), guid, {"not_sent": problem})


def _view(hub, record):
    worker = hub.get_worker(record["body"]["worker"])
    values = record["body"]["values"]
    return {"worker": worker["moniker"], "record": record["moniker"], "values": values,
            "version": version(values), "applied": worker["config_version"]}


# ------------------------------------------------------------------- hub caps

def _call(hub, actor):
    """The plugin library, as core, for the caller (K2)."""
    with hub._lock, hub._db:
        user, token = hub.auth.actor(**actor)
    return Call(hub, records.CORE, actor, user, token)


def _worker(args):
    worker = args.get("worker")
    if not isinstance(worker, str) or not worker:
        raise HubError("bad_message", "args.worker is required: a worker's moniker or GUID.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    return worker


def get(hub, args, actor):
    guid = hub.get_worker(_worker(args))["guid"]
    call = _call(hub, actor)
    return {"config": _view(hub, call.get(_record(hub, guid)))}


def set_values(hub, args, actor):
    guid = hub.get_worker(_worker(args))["guid"]
    changes, unset = args.get("set", {}), args.get("unset", [])
    if not isinstance(changes, dict) or not isinstance(unset, list) or not all(isinstance(k, str) for k in unset):
        raise HubError("bad_message", "args.set must be an object and args.unset a list of names.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    call = _call(hub, actor)
    record = call.get(_record(hub, guid))
    values = dict(record["body"]["values"], **changes)
    for name in unset:
        values.pop(name, None)
    record = call.update(record["guid"], body={"worker": guid, "values": values})
    problem = deliver(hub, guid)
    return {"config": _view(hub, record), "sent": problem is None, **({"not_sent": problem} if problem else {})}


CAPS = {
    "config.get": (get, {
        "description": "Read a worker's config: the values set for it, as written (a secret stays secret:<name>), "
                       "their version, and the version the worker last said it applied.",
        "schema": {"type": "object", "required": ["worker"], "additionalProperties": False, "properties": {
            "worker": {"type": "string", "description": "The worker's moniker or GUID."}}},
    }),
    "config.set": (set_values, {
        "description": "Set or unset values in a worker's config; the hub sends it the new config at once, with "
                       "each value that is exactly secret:<name> replaced by that secret.",
        "schema": {"type": "object", "required": ["worker"], "additionalProperties": False, "properties": {
            "worker": {"type": "string", "description": "The worker's moniker or GUID."},
            "set": {"type": "object", "description": "Names and the values to give them; a value may be "
                                                     "secret:<name> to use a secret from the vault."},
            "unset": {"type": "array", "items": {"type": "string"},
                      "description": "Names to take out of the config."}}},
    }),
}
