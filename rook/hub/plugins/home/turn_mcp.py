"""The per-turn tool server a Claude Code turn connects to: ``/home/turn-mcp``.

When the home agent runs on Claude Code (``home.backend = claude-code``), the
model runs on an agent host (worker plugin ``agenthost``), but its tools stay
at the hub: each turn registers the tools it may use here under a fresh random
bearer token, hands the URL and token to the agent host, and drops them when
the turn ends. The token is the whole credential: it reaches exactly that
turn's tools (the same ones the OpenAI-compatible backend would offer, run by
the same hub code as the same identity), and nothing else on the hub.

This is a small, stateless MCP server over streamable HTTP (JSON replies, no
SSE stream): ``initialize``, ``tools/list``, ``tools/call``, ``ping``;
notifications get 202.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.plugins.home.turn_mcp")

PATH = "/home/turn-mcp"
PROTOCOL = "2025-06-18"
#: A turn's token outlives its timeout by this much at most.
GRACE_S = 60.0
MAX_BODY = 1_000_000

Handler = Callable[[str, dict], Awaitable[str]]


@dataclass
class TurnTools:
    tools: list[dict]                 # OpenAI-style function schemas
    handler: Handler                  # (name, args) -> text result
    expires: float
    calls: int = 0
    used: list[str] = field(default_factory=list)


def mcp_tool(fn: dict) -> dict:
    """An OpenAI ``{"type": "function", "function": {...}}`` schema as an MCP tool."""
    f = fn.get("function") or fn
    return {"name": f["name"], "description": f.get("description") or "",
            "inputSchema": f.get("parameters") or {"type": "object", "properties": {}}}


class Registry:
    def __init__(self, clock=time.monotonic) -> None:
        self._turns: dict[str, TurnTools] = {}
        self.clock = clock

    def open(self, tools: list[dict], handler: Handler, timeout_s: float) -> tuple[str, TurnTools]:
        self._sweep()
        token = secrets.token_urlsafe(32)
        t = TurnTools(list(tools), handler, self.clock() + float(timeout_s) + GRACE_S)
        self._turns[token] = t
        return token, t

    def close(self, token: str) -> None:
        self._turns.pop(token, None)

    def get(self, token: str) -> TurnTools | None:
        self._sweep()
        for tok, t in self._turns.items():
            if hmac.compare_digest(tok.encode(), token.encode()):
                return t
        return None

    def _sweep(self) -> None:
        now = self.clock()
        for tok in [k for k, t in self._turns.items() if t.expires < now]:
            self._turns.pop(tok, None)

    def __len__(self) -> int:
        return len(self._turns)


def _rpc(mid: Any, result: Any = None, error: tuple[int, str] | None = None) -> dict:
    out: dict = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        out["error"] = {"code": error[0], "message": error[1]}
    else:
        out["result"] = result
    return out


async def handle(turn: TurnTools, msg: dict) -> dict | None:
    """One JSON-RPC message; None for a notification."""
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        return _rpc(mid, {"protocolVersion": PROTOCOL,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "rook-home", "version": "1"}})
    if method == "ping":
        return _rpc(mid, {})
    if method == "tools/list":
        return _rpc(mid, {"tools": [mcp_tool(t) for t in turn.tools]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in {(t.get("function") or t)["name"] for t in turn.tools}:
            return _rpc(mid, {"content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                              "isError": True})
        turn.calls += 1
        turn.used.append(name)
        try:
            text = await turn.handler(name, args)
            err = False
        except Exception as e:  # noqa: BLE001 - a tool error goes back to the model
            log.warning("home turn tool %s failed: %s", name, e)
            text, err = json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}), True
        return _rpc(mid, {"content": [{"type": "text", "text": text}], "isError": err})
    return _rpc(mid, error=(-32601, f"method not found: {method}"))


def routes(get_registry: Callable[[], "Registry | None"]):
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    async def endpoint(request: Request):
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        auth = request.headers.get("authorization", "")
        reg = get_registry()
        turn = reg.get(auth[7:]) if reg is not None and auth.startswith("Bearer ") else None
        if turn is None:
            return JSONResponse({"error": "unknown or finished turn"}, status_code=401)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            msg = json.loads(body)
        except ValueError:
            return JSONResponse(_rpc(None, error=(-32700, "parse error")), status_code=400)
        if isinstance(msg, list):
            out = [r for r in [await handle(turn, m) for m in msg if isinstance(m, dict)] if r]
            return JSONResponse(out) if out else Response(status_code=202)
        if not isinstance(msg, dict):
            return JSONResponse(_rpc(None, error=(-32600, "invalid request")), status_code=400)
        reply = await handle(turn, msg)
        return JSONResponse(reply) if reply is not None else Response(status_code=202)

    return [Route(PATH, endpoint, methods=["GET", "POST", "DELETE"])]
