"""The MCP door (C2): how an agent reaches the hub.

MCP's Streamable HTTP transport at `/mcp`: the client POSTs one JSON-RPC
message, the hub answers with one JSON response. The hub never pushes
messages, so there is no event stream and no session.

Every request carries `Authorization: Bearer <token>`, a hub token with the
`client` scope (T1, T4). Anything else is refused with what to try next (C3).
Each request is a use of the token (T6); calls are journaled with the token
and its user (L1).

Five tools, generic on purpose (C2): workers, caps, call, journal, stream. Caps are
discovered through them, never registered as tools.
"""

import json

from aiohttp import web

from . import bands, caps, devices, journal
from .errors import HubError

PATH = "/mcp"
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # MCP revisions with initialize

INSTRUCTIONS = (
    "Rook gives you real machines. Each machine runs a worker that serves caps "
    "(capabilities such as shell.exec). Use workers to see the machines, caps to see what "
    "they can do, and call to do it, always with a one-sentence why. journal shows what "
    "has happened. The hub's own caps (approving new machines, minting join tokens) are "
    "called with worker \"hub\".")

TOOLS = [
    {"name": "workers",
     "description": (
         "List the machines (workers) on this Rook hub, online or not, and the machines knocking to join. "
         "Each worker shows its moniker (use it to address the worker in call), owner, whether it is "
         "online, what it runs on (facts), its health, its gateway MAC, its band (the named network it "
         "is on) or roaming when it is on a network nobody has named, and the names of the caps it "
         "last announced. A knocking machine cannot be used until it is approved: call worker \"hub\", "
         "cap \"worker.approve\", args {\"key\": its key}. If likely_rekey_of names a known worker that "
         "lost its key, use cap \"worker.attach\" instead. Devices (a TV, a lamp: anything with an API "
         "that cannot run a worker) are listed as workers with device true, their band, their route "
         "and whether they were reachable at the last status poll; call their caps by their moniker."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "caps",
     "description": (
         "Show what workers can do: each cap's name, one-line description, argument schema, and whether "
         "it returns a result or opens a stream. Narrow with worker (a moniker or GUID, or \"hub\" for "
         "the hub's own caps) and with match (part of a cap name, such as \"shell.\" or \"exec\"). "
         "Offline workers are included and marked."),
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "worker": {"type": "string", "description": "Moniker or GUID of one worker, or \"hub\"."},
         "match": {"type": "string", "description": "Only caps whose name contains this."}}}},
    {"name": "call",
     "description": (
         "Run one cap on one worker and wait for its reply. worker: a moniker or GUID from workers, or "
         "\"hub\" for the hub's own caps. cap: a cap name from caps. args: arguments matching that cap's "
         "schema. why: one plain sentence saying why you are doing this; the journal keeps it with your "
         "token and user. The reply is the cap's result, or an error that says what to try next."),
     "inputSchema": {"type": "object", "required": ["worker", "cap", "why"], "additionalProperties": False,
                     "properties": {
                         "worker": {"type": "string"},
                         "cap": {"type": "string"},
                         "args": {"type": "object"},
                         "why": {"type": "string"}}}},
    {"name": "journal",
     "description": (
         "Read the hub's journal, newest first: every call (with its why and result), auth event "
         "(approvals, tokens, logins), change to a band, event on the hub's bus, job run, and closed stream. "
         "Narrow with worker, cap, user (moniker or GUID), kind (\"call\", \"auth\", \"band\", \"event\", "
         "\"job\" or \"stream\"), "
         "job (moniker or GUID), run (a run ID; every run in one chain of jobs shares it), since and until "
         "(ISO 8601 times) and text (found in the why, cap or result). limit is how many entries come back "
         "(default 50)."),
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "worker": {"type": "string"}, "cap": {"type": "string"}, "user": {"type": "string"},
         "kind": {"type": "string", "enum": ["call", "auth", "band", "event", "job", "stream"]},
         "job": {"type": "string"}, "run": {"type": "string"},
         "since": {"type": "string"}, "until": {"type": "string"}, "text": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1}}}},
    {"name": "stream",
     "description": (
         "Work with streams: things that stay open on a worker, such as a terminal or a running process. "
         "Calling a cap whose returns is \"stream\" opens one, and the reply names it. action \"list\": every "
         "stream with its title, description, worker, who opened it and whether it is open (open: true for "
         "open ones only). action \"read\": what passed through the stream from position `from` (0 is the "
         "start): output from the worker as \"out\", input as \"in\" with who sent it. The reply's `next` "
         "is where to read from next time. With `wait` (seconds, up to 40) a read with nothing new waits for "
         "more. action \"write\": send `text` as input (end it with \"\\n\" to finish a line). action "
         "\"close\": close it, with a `why`. Several agents can read one stream at once. A stream stays open "
         "until someone closes it, so close it when its work is done."),
     "inputSchema": {"type": "object", "required": ["action"], "additionalProperties": False, "properties": {
         "action": {"type": "string", "enum": ["list", "read", "write", "close"]},
         "stream": {"type": "string", "description": "The stream's moniker or GUID."},
         "from": {"type": "integer", "minimum": 0}, "wait": {"type": "number", "minimum": 0, "maximum": 40},
         "limit": {"type": "integer", "minimum": 1}, "text": {"type": "string"}, "why": {"type": "string"},
         "open": {"type": "boolean"}}}},
]


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _owner(hub, guid):
    user = hub.auth.get_user(guid)
    return {"name": user["name"], "moniker": user["moniker"]}


async def workers(hub, token, args):
    band_of, opted_out = bands.membership(hub), devices.opted_out(hub)
    found = [{
        "moniker": w["moniker"], "guid": w["guid"], "name": w["name"], "owner": _owner(hub, w["owner"]),
        "online": w["online"], "last_heartbeat": journal.iso(w["last_heartbeat"]),
        "facts": w["facts"], "health": w["health"],
        "gateway_mac": (w["band"] or {}).get("gateway_mac"),
        "band": band_of.get(w["guid"]), "roaming": band_of.get(w["guid"]) is None,
        "caps": [c["name"] for c in w["caps"]], "serves_devices": w["guid"] not in opted_out,
        **({"streams": w["streams"]} if w["streams"] is not None else {}),
    } for w in hub.list_workers()]
    found += [dict(d, owner=_owner(hub, d["owner"]), caps=[c["name"] for c in d["caps"]])
              for d in devices.list_devices(hub)]                          # a device is a worker with caps (D1)
    knocking = [{
        "key": k["key"], "name": k["name"], "macs": k["macs"],
        "caps": [{"name": c["name"], "description": c["description"]} for c in k["caps"]],
        "first_seen": journal.iso(k["first_at"]), "last_seen": journal.iso(k["last_at"]),
        "likely_rekey_of": [{"moniker": r["moniker"], "matched": r["matched"]} for r in k["likely_rekey_of"]],
    } for k in hub.list_pending()]
    return _dumps({"workers": found, "knocking": knocking})


async def list_caps(hub, token, args):
    worker, match = args.get("worker"), args.get("match") or ""
    groups = [] if worker not in (None, caps.HUB) else [("hub", True, caps.descriptors(hub))]
    if worker != caps.HUB:
        device = devices.find(hub, worker) if worker is not None else None
        chosen = [] if device else [hub.get_worker(worker)] if worker is not None else hub.list_workers()
        groups += [(w["moniker"], w["online"], w["caps"]) for w in chosen]
        groups += [(d["moniker"], d["reachable"] is not False, d["caps"]) for d in devices.list_devices(hub)
                   if worker is None or (device is not None and d["guid"] == device["guid"])]              # devices too (D1)
    return _dumps({"caps": [
        {"worker": name, "online": online, "caps": [
            {"name": c["name"], "description": c["description"], "returns": c["returns"], "schema": c["schema"]}
            for c in announced if match in c["name"]]}
        for name, online, announced in groups]})


async def call(hub, token, args):
    for name in ("worker", "cap", "why"):
        if not isinstance(args.get(name), str) or not args[name]:
            raise HubError("bad_message", f"{name} is required.",
                           "Give worker, cap and why as strings, and args as an object.")
    reply = await hub.call(args["worker"], args["cap"], args.get("args") or {}, args["why"], token=token)
    if reply["type"] == "error":
        raise HubError(reply["code"], reply["message"], reply["next"])
    if reply["type"] == "stream_open":
        return _dumps(dict({name: reply[name] for name in ("stream", "title", "description")},
                           next="Read it with the stream tool, from position 0."))
    return _dumps(reply["result"])


async def read_journal(hub, token, args):
    filters = {name: args.get(name) for name in ("worker", "cap", "user", "kind", "since", "until", "text", "job", "run")}
    return _dumps({"entries": journal.read(hub, **filters, limit=args.get("limit") or 50)})


async def stream(hub, token, args):
    action, name = args.get("action"), args.get("stream")
    if action == "list":
        return _dumps({"streams": hub.streams.list(args.get("open"))})
    if not isinstance(name, str) or not name:
        raise HubError("bad_message", "stream is required for this action.",
                       "Give the stream's moniker or GUID; action \"list\" shows them.")
    if action == "read":
        return _dumps(await hub.streams.read(name, args.get("from", 0), args.get("wait", 0), args.get("limit", 200)))
    if action == "write":
        if not isinstance(args.get("text"), str):
            raise HubError("bad_message", "text is required to write.", "Give the input as text.")
        return _dumps(await hub.streams.write(name, args["text"], token=token))
    if action == "close":
        if not isinstance(args.get("why"), str) or not args["why"]:
            raise HubError("bad_message", "why is required to close a stream.",
                           "Say in one sentence why it is done; the journal keeps it.")
        return _dumps(await hub.streams.close(name, args["why"], token=token))
    raise HubError("bad_message", f"action {action!r} is not one of list, read, write, close.",
                   "Use action list, read, write or close.")


HANDLERS = {"workers": workers, "caps": list_caps, "call": call, "journal": read_journal, "stream": stream}


def _rpc(message_id, status=200, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": message_id}
    body.update({"error": error} if error is not None else {"result": result})
    return web.json_response(body, status=status)


def _authenticate(hub, request):
    """The request's token, if it is a good client token; else HubError. Recording the use (T6)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise HubError("token_missing", "This door needs a token.",
                       "Connect with the header \"Authorization: Bearer <token>\", using an agent token "
                       "minted on this hub.")
    token = header[7:].strip()
    with hub._lock, hub._db:
        hub.auth.use(token, "client")
    return token


async def _tool(hub, token, params):
    handler = HANDLERS[params["name"]]
    arguments = params.get("arguments") or {}
    try:
        if not isinstance(arguments, dict):
            raise HubError("bad_message", "Tool arguments must be an object.", "Send the arguments as an object.")
        text, failed = await handler(hub, token, arguments), False
    except HubError as e:
        text, failed = f"{e.message['code']}: {e.message['message']} Next: {e.message['next']}", True
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def add_routes(app, hub):
    async def post(request):
        try:
            message = json.loads(await request.content.read())   # no size limit: arguments carry files
        except ValueError:
            return _rpc(None, 400, error={"code": -32700, "message": "The body is not JSON."})
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc(None, 400, error={"code": -32600, "message": "Send one JSON-RPC 2.0 message per request."})
        message_id = message.get("id")
        try:
            token = _authenticate(hub, request)
        except HubError as e:
            status = 403 if e.message["code"] == "token_scope" else 401
            return _rpc(message_id, status, error={
                "code": -32001, "message": f"{e.message['message']} {e.message['next']}", "data": e.message})
        method = message.get("method")
        if method is None or message_id is None:  # a notification, or a reply to us: nothing to answer
            return web.Response(status=202)
        params = message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _rpc(message_id, result={
                "protocolVersion": asked if asked in VERSIONS else VERSIONS[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rook-hub", "version": "0.1.0"},
                "instructions": INSTRUCTIONS})
        if method == "ping":
            return _rpc(message_id, result={})
        if method == "tools/list":
            return _rpc(message_id, result={"tools": TOOLS})
        if method == "tools/call":
            if params.get("name") not in HANDLERS:
                return _rpc(message_id, error={"code": -32602, "message": (
                    f"No tool is called {params.get('name')!r}. The tools are {', '.join(HANDLERS)}.")})
            return _rpc(message_id, result=await _tool(hub, token, params))
        return _rpc(message_id, error={"code": -32601, "message": f"This door does not answer {method}."})

    async def no_stream(request):
        return web.Response(status=405, headers={"Allow": "POST"}, text="This door answers POST only.")

    app.router.add_post(PATH, post)
    app.router.add_get(PATH, no_stream)
    app.router.add_delete(PATH, no_stream)
