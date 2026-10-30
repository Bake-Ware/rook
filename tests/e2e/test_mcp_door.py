"""Castling 0.5 through the MCP door, as real processes: an agent token
(claude-code acting for bake) calls a cap on a real worker over MCP, sees
its hold and reads the journal; the stdio shim does the same as a child
process (see harness.py and docs/castling-e2e.md)."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from harness import HTTP_SECS, WHY


def mcp(band, token: str, method: str, params: dict | None = None, mid: int = 1) -> dict:
    req = urllib.request.Request(band.mcp_url, method="POST", data=json.dumps(
        {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=HTTP_SECS) as r:
        out = json.loads(r.read())
    assert out["id"] == mid and "result" in out, out
    return out["result"]


def tool(band, token: str, name: str, **args) -> dict:
    res = mcp(band, token, "tools/call", {"name": name, "arguments": args})
    out = json.loads(res["content"][0]["text"])
    assert res["isError"] is (out.get("ok") is False and not out.get("held")), res
    return out


def test_an_agent_calls_a_cap_through_the_mcp_door(band):
    token = band.hub_cli("token", "--person", "bake", "--agent", "claude-code")
    init = mcp(band, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "e2e", "version": "0"}})
    assert init["serverInfo"]["name"] == "rook" and "call" in init["instructions"]
    assert {t["name"] for t in mcp(band, token, "tools/list")["tools"]} >= {"call", "caps", "workers", "journal"}

    caps = tool(band, token, "caps", worker="e2e-worker", match="e2e.")["workers"]["e2e-worker"]
    assert caps["online"] and caps["caps"]["e2e.ping"].startswith("read: ")
    out = tool(band, token, "call", worker="e2e-worker", cap="e2e.ping", args={"text": "mcp"}, why=WHY)
    assert out["ok"] and out["result"]["text"] == "mcp", out
    assert (out["result"]["person"], out["result"]["agent"]) == ("person:bake", "agent:claude-code")
    assert band.worker_calls("e2e.ping") >= 1
    entry = tool(band, token, "journal", call=out["id"])["entries"][0]
    assert (entry["decision"], entry["agent"], entry["why"]) == ("allow", "agent:claude-code", WHY)
    assert tool(band, token, "call", worker="e2e-worker", cap="e2e.ping", args={})["error"] == "denied: why required"

    # a hard stop holds the agent's call until the person answers at their own door
    held = tool(band, token, "call", worker="e2e-worker", cap="e2e.wipe", why=WHY)
    assert held["held"] and [h["hold"] for h in tool(band, token, "holds")["holds"]] == [held["hold"]]
    assert band.owner.post(f"/holds/{held['hold']}", {"decision": "approve"})["result"] == {"wiped": True}
    kinds = [e["decision"] for e in tool(band, token, "journal", call=held["id"], kind="call")["entries"]]
    assert kinds == ["released", "held"]


def test_the_stdio_shim_reaches_the_door(band):
    token = band.hub_cli("token", "--person", "bake", "--agent", "claude-code")
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "call", "arguments": {
                 "worker": "e2e-worker", "cap": "e2e.ping", "args": {"text": "stdio"}, "why": WHY}}}]
    r = subprocess.run([sys.executable, "-m", "rook.core2.hub.doors.mcp_stdio", "--url", band.mcp_url],
                       input="".join(json.dumps(m) + "\n" for m in lines), capture_output=True, text=True,
                       env={**band.env, "ROOK_TOKEN": token}, timeout=HTTP_SECS)
    assert r.returncode == 0, r.stderr
    replies = {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}
    assert set(replies) == {1, 2}
    assert json.loads(replies[2]["result"]["content"][0]["text"])["result"]["text"] == "stdio"
