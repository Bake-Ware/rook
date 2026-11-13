"""Caps the hub itself serves, called like any worker's caps with worker `hub`.

Only what the plan requires an agent to be able to do: approve a knocking
worker (R2), attach a knock to a known identity (W7), mint a join token (R3),
manage jobs and emit events (J7, J2; jobs.py), keep and list secrets (X3;
vault.py).
"""

import datetime

from . import jobs, vault
from .errors import HubError

HUB = "hub"  # the worker name that addresses the hub's own caps

# Hub caps whose arguments are stored, not used: their secret:<name> references are kept
# as written and resolved when the stored call runs (X2: a job stores the reference).
KEEP_REFERENCES = {"job.create"}


def _text(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise HubError("bad_message", f"args.{name} is required and must be a string.",
                       "Call again with the arguments in this cap's schema (see the caps tool).")
    return value


def _worker(record):
    return {"guid": record["guid"], "moniker": record["moniker"], "name": record["name"]}


def approve(hub, args, actor):
    record = hub.approve(_text(args, "key"), **actor)
    return {"approved": _worker(record), "owner": hub.auth.get_user(record["owner"])["moniker"]}


def attach(hub, args, actor):
    record = hub.attach(_text(args, "key"), _text(args, "worker"), **actor)
    return {"attached_to": _worker(record)}


def join_token(hub, args, actor):
    jwt = hub.auth.mint("join", **actor)
    record = hub.auth.verify(jwt)
    expires = record["expires_at"]
    return {"token": jwt, "moniker": record["moniker"],
            "expires_at": None if expires is None else
            datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).isoformat(timespec="seconds"),
            "use": "Give it to one new worker at install. Its knock is approved at once, owned by you. "
                   "It works once."}


CAPS = {
    "worker.approve": (approve, {
        "description": "Approve a knocking worker as a new machine on this hub; you become its owner.",
        "schema": {"type": "object", "required": ["key"], "additionalProperties": False, "properties": {
            "key": {"type": "string", "description": "The knocking worker's key, from the workers tool."}}},
    }),
    "worker.attach": (attach, {
        "description": "Attach a knocking worker's new key to a known worker that lost its key "
                       "(a likely rekey); the old key is revoked.",
        "schema": {"type": "object", "required": ["key", "worker"], "additionalProperties": False,
                   "properties": {
                       "key": {"type": "string", "description": "The knocking worker's key."},
                       "worker": {"type": "string", "description": "The known worker's moniker or GUID."}}},
    }),
    "worker.join-token": (join_token, {
        "description": "Mint a one-use join token: a new worker that knocks with it is approved at once.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }),
}


CAPS.update(jobs.CAPS)  # create, list, pause, un-pause and delete jobs; emit an event (J7, J2)
CAPS.update(vault.caps())  # create and list secrets, never their values (X3)


def descriptors():
    """The hub's caps as a worker announces caps (W3)."""
    return [dict(meta, name=name, returns="result") for name, (_, meta) in CAPS.items()]


def for_journal(reply):
    """A hub cap's reply as the journal keeps it: never a usable token. Any shape is accepted."""
    if not isinstance(reply, dict):
        return reply
    result = reply.get("result")
    if isinstance(result, dict) and "token" in result:
        return dict(reply, result=dict(result, token="(withheld from the journal)"))
    return reply


def run(hub, cap, args, actor):
    """Run one hub cap. Returns its result, or raises HubError."""
    if cap not in CAPS:
        raise HubError("unknown_cap", f"The hub does not serve {cap}.",
                       f"The hub serves {', '.join(CAPS)}; list them with the caps tool.")
    return CAPS[cap][0](hub, args, actor)
