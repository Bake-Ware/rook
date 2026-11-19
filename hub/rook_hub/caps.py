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

from . import backup, config, devices, history, jobs, plugins, scrub, vault
from .errors import HubError

HUB = "hub"  # the worker name that addresses the hub's own caps

# Hub caps whose arguments are stored, not used: their secret:<name> references are kept
# as written and resolved when the stored call runs (X2: a job stores the reference;
# a device's config keeps it and its driver gets the value each time, D7; a worker's config
# keeps it and the worker gets the value each time its config is sent, CF3).
KEEP_REFERENCES = {"job.create", "job.update", "device.add", "config.set"}


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


def interval(hub, args, actor):
    seconds = args.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise HubError("bad_message", "args.seconds is required: a whole number of seconds, 1 or more.",
                       "Give seconds, such as 60.")
    with hub._lock:
        hub.auth.actor(**actor)
    record = hub.get_worker(_text(args, "worker"))
    sent = hub.set_interval(record["guid"], seconds)
    return {"worker": _worker(record), "interval": seconds, "sent": sent,
            "next": "It heartbeats on this interval from now on." if sent else
                    f"{record['moniker']} is not connected: it gets this interval when it links."}


# The Settings page's values, for an agent (0.5's setting.list and setting.set; P4). Today the page
# holds one: the hub's name ("Hubs have names"). Plugins keep their own settings behind their own caps.
SETTINGS = {"name": "The hub's name, shown on the site."}


def setting_list(hub, args, actor):
    """The hub's own settings, then every loaded plugin's (brief HA2; 0.5's setting.list with
    `plugin`): a where="person" setting shows the caller's own value."""
    from .library import Call
    with hub._lock:
        user, token = hub.auth.actor(**actor)
    values = {"name": hub.name()}
    wanted = args.get("plugin")
    found = [] if wanted else [{"section": "hub", "key": key, "value": values[key], "description": description}
                               for key, description in SETTINGS.items()]
    for plugin in sorted(hub.plugins.declared):
        if (wanted and plugin != wanted) or plugin not in hub.plugins.loaded:
            continue
        settings = Call(hub, plugin, actor, user, token).settings
        for key, spec in hub.plugins.declared_settings(plugin).items():
            mine = spec["where"] == "hub" or user is not None
            found.append({"section": plugin, "plugin": plugin, "key": key, "where": spec["where"],
                          "value": settings.get(key) if mine else spec["default"],
                          "chosen": settings.chosen(key) if mine else False, "description": spec["doc"]})
    if wanted and not found:
        raise HubError("unknown_plugin", f"No loaded plugin {wanted!r} has settings.",
                       "List the settings with setting.list and no plugin to see whose there are.")
    return {"settings": found}


def setting_set(hub, args, actor):
    key, plugin = _text(args, "key"), args.get("plugin")
    if plugin is not None:              # a plugin's setting (brief HA2); a where="person" one is the caller's own
        from .library import Call
        if plugin not in hub.plugins.loaded:
            raise HubError("unknown_plugin", f"No loaded plugin is called {plugin!r}.",
                           "List the settings with setting.list; each plugin's are under its name.")
        with hub._lock:
            user, token = hub.auth.actor(**actor)
        settings = Call(hub, plugin, actor, user, token).settings
        spec = settings._declared(key)
        return {"section": plugin, "plugin": plugin, "key": key, "where": spec["where"],
                "value": settings.set(key, args.get("value"))}
    if key not in SETTINGS:
        raise HubError("unknown_setting", f"The hub has no setting {key!r}.",
                       f"The settings are {', '.join(SETTINGS)}, and each plugin's under its name; "
                       "setting.list shows them with their values.")
    return {"section": "hub", "key": key, "value": hub.set_name(args.get("value"), **actor)}


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
    "worker.interval": (interval, {
        "description": "Set how often a worker heartbeats, in seconds (H2). A linked worker follows it at once; "
                       "it is offline after missing three.",
        "schema": {"type": "object", "required": ["worker", "seconds"], "additionalProperties": False,
                   "properties": {
                       "worker": {"type": "string", "description": "The worker's moniker or GUID."},
                       "seconds": {"type": "integer", "minimum": 1, "description": "Seconds between heartbeats."}}},
    }),
    "setting.list": (setting_list, {
        "description": "List the hub's settings, as the Settings page holds them, and each plugin's, with their values.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "plugin": {"type": "string", "description": "Only this plugin's settings (its name)."}}},
    }),
    "setting.set": (setting_set, {
        "description": "Change one of the hub's settings, as on the Settings page, or a plugin's; setting.list names them.",
        "schema": {"type": "object", "required": ["key", "value"], "additionalProperties": False, "properties": {
            "key": {"type": "string", "description": "The setting, such as name."},
            "plugin": {"type": "string", "description": "The plugin whose setting it is; leave out for the hub's own."},
            "value": {"description": "Its new value; null forgets a plugin's setting."}}},
    }),
}


CAPS.update(jobs.CAPS)  # create, list, change, pause, un-pause, run, stop and delete jobs; emit an event (J7, J2; brief 59)
CAPS.update(vault.caps())  # create, replace, delete and list secrets, never their values (X3)
CAPS.update(scrub.caps())  # take a value out of the journal (L5)
CAPS.update(plugins.CAPS)  # install and list plugins (PL4)
CAPS.update(devices.CAPS)  # add a device (D1)
CAPS.update(config.CAPS)  # read and set a worker's config (CF1, CF2, CF3)
CAPS.update(backup.CAPS)  # back up the hub, list and verify backups (F3, X4; 0.5's backup.*)
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
