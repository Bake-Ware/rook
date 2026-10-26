"""A minimal MCP client over streamable HTTP, stdlib only.

`rook ask` and the other hub commands use it with an API token from the
dashboard's Tokens page: the hub's MCP server runs the call as that token
(its permissions, its journal rows), and, unlike a band call, it may run hub
caps above the band's ``read`` ceiling, such as ``home.ask``.

One call is one short session: ``initialize``, ``notifications/initialized``,
``tools/call``, then a best-effort ``DELETE`` of the session. Replies may be
plain JSON or a server-sent event stream; both are read.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

PROTOCOL = "2025-06-18"
MAX_REPLY = 16 * 1024 * 1024


class McpError(Exception):
    """The MCP endpoint refused or failed the request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the request, bearer token
    included, to wherever the hub (or something in between) points, another
    host too, and turn the POST into a GET. The redirect surfaces as an
    HTTPError and the user is told the address to use instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _messages(body: bytes, content_type: str) -> list[dict]:
    if "text/event-stream" in content_type:
        out, data = [], []
        for line in body.decode("utf-8", "replace").splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                try:
                    out.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
        return out
    if not body.strip():
        return []
    value = json.loads(body)
    return value if isinstance(value, list) else [value]


class McpClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session: str | None = None

    def _post(self, payload: dict, timeout: float | None = None,
              method: str = "POST") -> list[dict]:
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json",
                   "MCP-Protocol-Version": PROTOCOL,
                   "User-Agent": "rook-cli"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        data = json.dumps(payload).encode() if method == "POST" else None
        req = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=timeout or self.timeout) as r:
                if r.headers.get("Mcp-Session-Id"):
                    self.session = r.headers["Mcp-Session-Id"]
                body = r.read(MAX_REPLY + 1)
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise McpError("the hub refused the API token (create a new one on the "
                               "dashboard's Tokens page, then run rook login --token)") from None
            if 300 <= e.code < 400:
                where = e.headers.get("Location", "") if e.headers else ""
                raise McpError(f"the hub's MCP endpoint redirected to {where or 'another address'}; "
                               "the token was not sent there. Use that address: rook login "
                               "--token --mcp-url <address>") from None
            if e.code == 404:
                raise McpError(f"nothing answers MCP at {self.url} (HTTP 404). On a hub whose MCP "
                               "server runs separately (usually port 8765), give its address: "
                               "rook login --token --mcp-url http://<hub address>:8765/mcp") from None
            raise McpError(f"the hub's MCP endpoint answered HTTP {e.code}") from None
        if len(body) > MAX_REPLY:
            raise McpError("reply too large")
        return _messages(body, ctype)

    def _rpc(self, rid: int, method: str, params: dict, timeout: float | None = None) -> Any:
        for msg in self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                              timeout):
            if msg.get("id") == rid:
                if "error" in msg:
                    raise McpError(str((msg["error"] or {}).get("message") or msg["error"]))
                return msg.get("result")
        raise McpError(f"no answer to {method}")

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> str:
        """The tool's text result. Raises McpError on refusal or a tool error."""
        self._rpc(1, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "rook-cli", "version": "1"}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = self._rpc(2, "tools/call", {"name": name, "arguments": arguments},
                               timeout) or {}
        finally:
            self.close()
        text = "".join(c.get("text", "") for c in result.get("content") or []
                       if isinstance(c, dict) and c.get("type") == "text")
        if result.get("isError"):
            raise McpError(text or f"{name} failed")
        return text

    def close(self) -> None:
        if not self.session:
            return
        try:
            self._post({}, timeout=5, method="DELETE")
        except Exception:
            pass
        self.session = None


def rook_call(url: str, token: str, cap: str, args: dict, worker: str = "rook",
              timeout: float = 30.0, why: str = "") -> dict:
    """``rook_call`` through MCP; returns its reply ``{"ok", "result"|"error"}``."""
    text = McpClient(url, token, timeout=timeout + 10).call_tool(
        "rook_call", {"cap": cap, "why": why or f"rook command on this machine: {cap}",
                      "args": args, "worker": worker, "timeout": timeout},
        timeout=timeout + 10)
    try:
        reply = json.loads(text)
    except ValueError:
        return {"ok": False, "error": text[:500]}
    return reply if isinstance(reply, dict) else {"ok": True, "result": reply}
