"""Caps the hub itself serves, called like any worker's caps with worker `hub`.

Only what the plan requires an agent to be able to do: approve a knocking
worker (R2), attach a knock to a known identity (W7), change a worker's owner
(W6), revoke a worker's key (R5), mint a join token (R3), mint, list and revoke
tokens and list users (T5, T6; 0.5's token.* and users.list), manage jobs and emit
events (J7, J2; jobs.py), keep and list secrets (X3; vault.py), redact the
journal (L5; scrub.py), install and list plugins (PL4; plugins.py), add a
device (D1; devices.py), read and set a worker's config (CF1 to CF3;
config.py), and roll up the heartbeat history (H5; history.py). Installed plugins add caps of their own, served the same way (PL1).
"""

import datetime

from . import config, devices, history, jobs, plugins, scrub, vault
from .errors import HubError

HUB = "hub"  # the worker name that addresses the hub's own caps

# Hub caps whose arguments are stored, not used: their secret:<name> references are kept
# as written and resolved when the stored call runs (X2: a job stores the reference;
# a device's config keeps it and its driver gets the value each time, D7; a worker's config
# keeps it and the worker gets the value each time its config is sent, CF3).
KEEP_REFERENCES = {"job.create", "device.add", "config.set"}


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


def reassign(hub, args, actor):
    record = hub.reassign(_text(args, "worker"), _text(args, "owner"), **actor)
    owner = hub.auth.get_user(record["owner"])
    return {"worker": _worker(record), "owner": {"moniker": owner["moniker"], "name": owner["name"]}}


def revoke(hub, args, actor):
    record = hub.revoke(_text(args, "worker"), **actor)
    return {"revoked_from": _worker(record)}


def join_token(hub, args, actor):
    return minted(hub, "join", actor)


# What a token of each purpose is for, as the minting reply says it (C1, C3).
USES = {
    "agent": "Give it to an agent as its MCP token: \"Authorization: Bearer <token>\" on POST /mcp. "
             "It acts as you until it is revoked.",
    "join": "Give it to one new worker at install: python -m rook_worker <hub url> --code <token>. "
            "Its knock is approved at once, owned by you. It works once.",
    "webhook": "POST anything to path with header; the body becomes a webhook event. A job whose match is "
               "match runs on it.",
}


def minted(hub, purpose, actor):
    """Mint a token for `purpose` as the actor (T5) and say how to use it. Webhook tokens carry 0.5's
    job.webhook shape where it applies: the path and the header, and the match a job uses (J2)."""
    jwt = hub.auth.mint(purpose, **actor)
    record = hub.auth.verify(jwt)
    reply = {"token": jwt, "moniker": record["moniker"], "purpose": purpose, "scopes": record["scopes"],
             "expires_at": _iso(record["expires_at"]), "use": USES[purpose]}
    if purpose == "webhook":
        reply.update(path="/webhook", header="Authorization: Bearer <token>",
                     match={"event": "webhook", "payload": {"token_moniker": record["moniker"]}})
    return reply


def _iso(at):
    return None if at is None else datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat(timespec="seconds")


def token_create(hub, args, actor):
    purpose = args.get("purpose", "agent")
    if purpose not in USES:
        raise HubError("bad_message", f"purpose must be one of {', '.join(USES)}; it is {purpose!r}.",
                       "Mint an agent token for an agent, a webhook token for a webhook, a join token for a new worker.")
    return minted(hub, purpose, actor)


def token_list(hub, args, actor):
    tokens = hub.auth.list_tokens()
    monikers = {t["guid"]: t["moniker"] for t in tokens}
    names = {u["guid"]: u["name"] for u in hub.auth.list_users()}
    return {"tokens": [{"moniker": t["moniker"], "guid": t["guid"], "purpose": t["purpose"], "scopes": t["scopes"],
                        "owner": {"moniker": t["owner_moniker"], "name": names[t["owner"]]},
                        "minted_by": monikers.get(t["minted_by"]),
                        **{name: _iso(t[name]) for name in ("minted_at", "expires_at", "last_used", "spent_at",
                                                            "revoked_at")}} for t in tokens]}


def token_revoke(hub, args, actor):
    return {"revoked": hub.auth.revoke_token(_text(args, "id"), **actor)}


def users_list(hub, args, actor):
    return {"users": [{"moniker": u["moniker"], "guid": u["guid"], "name": u["name"],
                       "created_at": _iso(u["created_at"]), "revoked_at": _iso(u["revoked_at"])}
                      for u in hub.auth.list_users()]}


_NOTHING = {"type": "object", "additionalProperties": False, "properties": {}}


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
    "worker.reassign": (reassign, {
        "description": "Make another user a worker's owner. Owner is a fact: it allows and refuses nothing.",
        "schema": {"type": "object", "required": ["worker", "owner"], "additionalProperties": False,
                   "properties": {
                       "worker": {"type": "string", "description": "The worker's moniker or GUID."},
                       "owner": {"type": "string", "description": "The new owner: a user's moniker or GUID."}}},
    }),
    "worker.revoke": (revoke, {
        "description": "Revoke a worker's key: it is told at once and stops; attach a new key to bring it back.",
        "schema": {"type": "object", "required": ["worker"], "additionalProperties": False, "properties": {
            "worker": {"type": "string", "description": "The worker's moniker or GUID."}}},
    }),
    "worker.join-token": (join_token, {
        "description": "Mint a one-use join token: a new worker that knocks with it is approved at once.",
        "schema": _NOTHING,
    }),
    "token.create": (token_create, {
        "description": "Mint a token owned by the person your token belongs to (T5): agent (the default) for an "
                       "agent's MCP access, webhook for POST /webhook, join for a new worker. Returned once.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "purpose": {"type": "string", "enum": list(USES), "description": "agent, webhook or join."}}},
    }),
    "token.list": (token_list, {
        "description": "Every token (never the token itself): purpose, scopes, owner, who minted it, when it was "
                       "made, ends, was last used, spent or revoked.",
        "schema": _NOTHING,
    }),
    "token.revoke": (token_revoke, {
        "description": "Revoke a token: it stops working at once.",
        "schema": {"type": "object", "required": ["id"], "additionalProperties": False, "properties": {
            "id": {"type": "string", "description": "The token's moniker or GUID, from token.list."}}},
    }),
    "users.list": (users_list, {
        "description": "Every user of this hub: name, moniker, GUID, when made and whether revoked.",
        "schema": _NOTHING,
    }),
}


CAPS.update(jobs.CAPS)  # create, list, pause, un-pause and delete jobs; emit an event (J7, J2)
CAPS.update(vault.caps())  # create and list secrets, never their values (X3)
CAPS.update(scrub.caps())  # take a value out of the journal (L5)
CAPS.update(plugins.CAPS)  # install and list plugins (PL4)
CAPS.update(devices.CAPS)  # add a device (D1)
CAPS.update(config.CAPS)  # read and set a worker's config (CF1, CF2, CF3)
CAPS.update(history.CAPS)  # roll heartbeats older than 12 hours into minutes (H5, Bake after the stress test)


def descriptors(hub):
    """The hub's caps, its plugins' included, as a worker announces caps (W3)."""
    return [dict(meta, name=name, returns="result") for name, (_, meta) in CAPS.items()] + hub.plugins.descriptors()


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
    if cap in hub.plugins.caps:
        return hub.plugins.call(cap, args, actor)
    if cap not in CAPS:
        raise HubError("unknown_cap", f"The hub does not serve {cap}.",
                       f"The hub serves {', '.join([*CAPS, *hub.plugins.caps])}; list them with the caps tool.")
    return CAPS[cap][0](hub, args, actor)
