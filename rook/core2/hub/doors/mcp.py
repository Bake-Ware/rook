"""The MCP door: agents (Claude Code, Codex, Hermes, local models) reach the
band through the Model Context Protocol, streamable HTTP transport
(castling-protocol: "agents use MCP unless we hit a wall").

    POST /mcp    one JSON-RPC message; a request gets an application/json
                 reply, a notification or response gets 202 and no body
    GET  /mcp    405: the door never starts calls of its own, so no SSE stream

Every request carries ``Authorization: Bearer <token>``. A token belongs to
an agent acting for a person (``python -m rook.core2.hub --config hub.yaml
token --person bake --agent claude-code``), so dispatch intersects what both
may do. Every tool ends at the hub's door API like the HTTP door's routes:
``call`` goes through dispatch (why, rights, gates, holds, ticket, journal),
the rest are the hub's read views. The door is stateless: no MCP session
ids; a caller's calls share their default Rook session unless they start one.

Options: ``bind: "127.0.0.1:8741"`` (port 0 picks one), ``path: /mcp``,
``origins: [...]``: browser origins allowed in; a request with any other
``Origin`` header is refused (DNS rebinding). Bind to localhost; put TLS in
front before exposing it. ``oauth: https://rook.example`` (the door's public
address) lets claude.ai's connectors in: the person signs in with their user
name and password and the connector gets a token for them acting through the
agent ``oauth_agent`` (default ``claude-web``, an agent in hub.yaml)
(:mod:`.mcp_oauth`).

Claude Code: ``claude mcp add --transport http rook http://127.0.0.1:8741/mcp
--header "Authorization: Bearer <token>"``. Agents that only speak stdio use
``python -m rook.core2.hub.doors.mcp_stdio`` (see there).
"""
from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from ... import CORE_VERSION
from .http import HttpDoor, _Error, _Handler
from .. import passwords
from ..identity import Caller
from .mcp_oauth import OAuthShim, _fields

log = logging.getLogger("rook.hub.door.mcp")

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # newest first

INSTRUCTIONS = """\
Rook: a band of machines (workers) that serve caps (capabilities, like shell.exec).
- Find: workers lists machines; caps lists what they serve (caps match="shell." for a family, \
match=<exact cap> for its arguments). Offline workers are listed too.
- Act: call worker=<name> cap=<cap> args={...} why="<one plain sentence>". The why is journaled \
and shown to the person if the call is held.
- A reply with held: tell the person the hold's name and stop; it runs once they approve \
(journal call=<id> shows how it went). Never work around a hold or a denial.
- Caps that answer {"stream": id} (terminals, processes, agent sessions): read and write it with stream. \
call worker=rook cap=stream.list (or stream.search query=...) finds streams and the worker each is on; \
chat rooms live on rook (call worker=rook cap=stream.open args={"title": ...} opens one).
- Every call is journaled under your token's agent and person; journal reads it back."""

_WHY = {"type": "string", "description": "One plain sentence: why you make this call. Journaled."}
_READ = {"readOnlyHint": True}
_WORKER = {"type": "string", "description": "Worker name or id (workers lists them; 'rook' is the hub), or an appliance's name (appliance.list)."}

TOOLS = [
    {"name": "call",
     "description": "Run one cap on one worker. Reply {id, ok, result | error}; held calls wait for a person.",
     "inputSchema": {"type": "object", "required": ["worker", "cap", "why"], "properties": {
         "worker": _WORKER,
         "cap": {"type": "string", "description": "The cap, e.g. shell.exec (caps lists them)."},
         "args": {"type": "object", "description": "The cap's arguments (caps match=<cap> describes them)."},
         "why": _WHY,
         "session": {"type": "string", "description": "A session from the session tool; blank: your default."},
         "appliance": {"type": "string", "description": "An appliance (TV, lamp...) by name; or just give its name as worker. "
                                                      "With both, worker pins one of its delegates."},
         "elevate": {"type": "boolean", "description": "Ask for administrator rights up front."}}}},
    {"name": "caps", "annotations": _READ,
     "description": "What workers serve, from their manifests (offline workers included): "
                    "one line per cap, or full detail (arguments, effects) for an exact cap or full=true.",
     "inputSchema": {"type": "object", "properties": {
         "worker": _WORKER,
         "match": {"type": "string", "description": "An exact cap, a prefix (shell.) or a glob (*.list)."},
         "full": {"type": "boolean", "description": "Full detail for every match."}}}},
    {"name": "workers", "annotations": _READ,
     "description": "The band's machines, online or not: name, id, plugins, description, problems.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "journal", "annotations": _READ,
     "description": "Journal entries you may read (newest first): who called what, why, and how it went.",
     "inputSchema": {"type": "object", "properties": {
         **{k: {"type": "string"} for k in ("call", "cap", "worker", "person", "agent", "project", "kind",
                                            "session", "chain")},
         "since": {"type": "number", "description": "Unix time."},
         "until": {"type": "number", "description": "Unix time."},
         "limit": {"type": "integer", "description": "Default 20, at most 500."}}}},
    {"name": "holds", "annotations": _READ,
     "description": "What waits on a person: held calls and questions agents asked (approval.ask); yours, or all for an owner. Only a person decides.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session",
     "description": "Start a session that narrows your calls to some caps and ties them to a project or "
                    "task in the journal (pass its id as call's session), or end one.",
     "inputSchema": {"type": "object", "properties": {
         "caps": {"type": "array", "items": {"type": "string"}, "description": "Cap patterns, e.g. [\"shell.*\"]."},
         "project": {"type": "string"}, "task": {"type": "string"},
         "end": {"type": "string", "description": "A session id to end instead."}}}},
    {"name": "stream",
     "description": "Read a stream (from cursor after; pass back next), or write input to it "
                    "(input={text} keystrokes or a message, {signal: INT}, {resize: [cols, rows]}). "
                    "On worker rook (chat rooms, agent and voice sessions): input={text} says it in a room.",
     "inputSchema": {"type": "object", "required": ["worker", "stream", "why"], "properties": {
         "worker": {"type": "string", "description": "The worker that opened it."},
         "stream": {"type": "string", "description": "The stream id (s_...)."},
         "why": _WHY,
         "input": {"type": "object", "description": "Write this instead of reading."},
         "after": {"type": "integer", "description": "Read events after this seq (0: from the start)."},
         "limit": {"type": "integer"},
         "wait": {"type": "number", "description": "Seconds to wait for new events (long poll)."}}}},
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "integer": int, "number": (int, float)}
_JOURNAL_DROP = ("hash", "prev", "args_hash", "ticket", "hub")


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class McpDoor(HttpDoor):
    name = "mcp"
    bind = "127.0.0.1:8741"

    def __init__(self, hub, options: dict):
        super().__init__(hub, options)
        self.path = "/" + str(options.get("path", "/mcp")).strip("/")
        self.origins = set(options.get("origins") or ())
        self.oauth_agent = str(options.get("oauth_agent") or "claude-web")
        self.oauth = OAuthShim(options["oauth"], self.path, hub.authenticate, self._mint, self._oauth_problem) \
            if options.get("oauth") else None

    def _oauth_problem(self) -> str | None:
        if self.oauth_agent not in self.hub.identities.agents:
            return (f"This hub has no agent {self.oauth_agent!r} for the connector yet: add "
                    f"'{self.oauth_agent}: {{grants: [\"*\"]}}' under agents: in hub.yaml and restart the hub.")
        return None

    def _mint(self, person: str) -> str:
        """The connector's token for a person who just signed in (on the hub's loop)."""
        problem = self._oauth_problem()
        if problem:
            raise ValueError(problem)
        log.info("connector signed in", extra={"fields": {"person": person, "agent": self.oauth_agent}})
        return self.hub.add_token(Caller(person, f"agent:{self.oauth_agent}"), label="claude.ai connector")

    def challenge(self) -> str:
        return self.oauth.challenge() if self.oauth else "Bearer"

    async def rpc(self, caller, method: str, params: dict) -> dict:
        """One JSON-RPC request to its result, or :class:`RpcError`."""
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "rook", "version": CORE_VERSION},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in _SCHEMAS:
                raise RpcError(-32602, f"unknown tool: {name}")
            if not isinstance(args, dict):
                raise RpcError(-32602, "arguments must be an object")
            bad = _check(_SCHEMAS[name], args)
            out = {"ok": False, "error": f"bad arguments: {bad}"} if bad else \
                await getattr(self, f"_{name}")(caller, args)
            return {"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"), default=str)}],
                    "isError": out.get("ok") is False and not out.get("held")}
        raise RpcError(-32601, f"method not found: {method}")

    # -- tools: each is the hub door API, trimmed for an agent's context -------

    async def _call(self, caller, a: dict) -> dict:
        body = {k: a[k] for k in ("worker", "cap", "args", "why", "session", "appliance", "elevate") if k in a}
        return await self.hub.call(caller, body, self.name)

    async def _caps(self, caller, a: dict) -> dict:
        match, full = a.get("match") or "", a.get("full") is True
        view = await self.hub.caps_view(caller, a.get("worker") or None)
        out = {}
        for wname, w in view["workers"].items():
            caps = {n: m for n, m in w["caps"].items() if _matches(n, match)}
            off = {n: why for n, why in w["unavailable"].items() if _matches(n, match)}
            if match and not (caps or off):
                continue
            entry: dict[str, Any] = {"online": w["online"]}
            entry["caps"] = {n: (m if full or n == match else _line(m)) for n, m in caps.items()}
            if off:
                entry["unavailable"] = off
            out[wname] = entry
        return {"ok": True, "workers": out}

    async def _workers(self, caller, a: dict) -> dict:
        view = await self.hub.workers_view(caller)
        return {"ok": True, "workers": [_trim(w) for w in view["workers"]]}

    async def _journal(self, caller, a: dict) -> dict:
        filters = {k: v for k, v in a.items() if v is not None}
        filters.setdefault("limit", 20)
        out = await self.hub.journal_view(caller, filters)
        if out.get("ok"):
            out["entries"] = [_trim({k: v for k, v in e.items() if k not in _JOURNAL_DROP})
                              for e in out["entries"]]
        return out

    async def _holds(self, caller, a: dict) -> dict:
        return await self.hub.holds_view(caller)

    async def _session(self, caller, a: dict) -> dict:
        if a.get("end"):
            return await self.hub.end_session(caller, a["end"])
        return await self.hub.start_session(caller, {k: a.get(k) for k in ("caps", "project", "task")})

    async def _stream(self, caller, a: dict) -> dict:
        # The worker's stream.read / stream.write (builtin/stream.yaml); stream.list on
        # the hub finds the stream and its worker.
        if "input" in a:      # the hub's own streams are rooms: text, not keystrokes
            kind = "text" if a.get("worker") == "rook" else "input"
            cap, args = "stream.write", {"stream": a.get("stream"), "type": kind, "body": a["input"]}
        else:
            cap = "stream.read"
            args = {"stream": a.get("stream"), **{k: a[k] for k in ("after", "limit", "wait") if k in a}}
        return await self.hub.call(caller, {"worker": a.get("worker"), "cap": cap, "args": args,
                                            "why": a.get("why")}, self.name)


class _McpHandler(_Handler):
    door: McpDoor

    def do_DELETE(self):
        self._handle("DELETE")

    def _route(self, method: str):
        url = urlsplit(self.path)
        oauth = self.door.oauth
        if oauth and oauth.owns(method, url.path.rstrip("/") or "/"):
            body = self._raw() if method == "POST" else b""
            auth = self.headers.get("Authorization", "")
            person = None
            if (method, url.path.rstrip("/")) == ("POST", "/authorize"):     # scrypt here, not on the loop
                f = _fields(body.decode("utf-8", "replace"))
                person = passwords.sign_in(self.door.hub, f.get("user"), f.get("password"))

            async def answer():      # on the hub's loop, like every token check (and one codes dict)
                return oauth.handle(method, url.path.rstrip("/"), url.query, body, auth, person=person)
            return self._run(answer())
        if url.path.rstrip("/") != self.door.path.rstrip("/"):
            raise _Error(404, "not found")
        if method != "POST":
            raise _Error(405, "POST JSON-RPC messages here; this door opens no stream")
        origin = self.headers.get("Origin")
        if origin and origin not in self.door.origins:
            raise _Error(403, f"origin {origin} not allowed")
        caller = self._run(self._auth())
        refused = self.door.hub.door_refused(caller, self.door.name)
        if refused:      # every tool, session starts and reads alike
            raise _Error(503, refused)
        try:
            msg = self._body()
        except _Error as e:
            if e.status != 400:
                raise
            raise _Error(400, e.message, _err(None, -32700, e.message)) from None
        if "method" not in msg or "id" not in msg:
            return 202, None              # a notification, or a response to nothing we asked
        mid, params = msg["id"], msg.get("params", {})
        if not isinstance(msg["method"], str) or not isinstance(params, dict):
            return 200, _err(mid, -32600, "invalid request")
        try:
            return 200, {"jsonrpc": "2.0", "id": mid,
                         "result": self._run(self.door.rpc(caller, msg["method"], params))}
        except RpcError as e:
            return 200, _err(mid, e.code, e.message)
        except _Error:
            raise
        except Exception:
            log.exception("mcp request failed")
            return 200, _err(mid, -32603, "internal error")


McpDoor.handler = _McpHandler


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _matches(name: str, match: str) -> bool:
    if not match:
        return True
    if any(c in match for c in "*?["):
        return fnmatch.fnmatchcase(name, match)
    return name == match or name.startswith(match)


def _check(schema: dict, args: dict) -> str | None:
    """What's wrong with a tool's argument names and types, or None. Missing
    ones are dispatch's to refuse (a call without a why is journaled)."""
    props = schema["properties"]
    for k, v in args.items():
        want = props.get(k, {}).get("type")
        if want is None:
            return f"unknown argument {k} (takes {', '.join(props)})"
        if v is not None and (not isinstance(v, _TYPES[want]) or (isinstance(v, bool) and want != "boolean")):
            return f"{k} must be a{'n' if want[0] in 'aio' else ''} {want}"
    return None


def _line(meta: dict) -> str:
    doc = str(meta.get("doc") or "").strip().splitlines()
    return f"{meta.get('risk', '?')}: {doc[0]}" if doc else str(meta.get("risk", "?"))


def _trim(d: dict) -> dict:
    """Leave out what says nothing (None, empty), not False or 0."""
    return {k: v for k, v in d.items() if v is not None and v != {} and v != [] and v != ""}
