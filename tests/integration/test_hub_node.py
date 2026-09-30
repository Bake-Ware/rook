"""The hub node (worker "rook") end to end on an isolated test hub (opt-in).

The MCP server hosts the hub plugins. The dashboard is a separate process with
its own band client, so its view of "rook" and its calls to it cross the real
relay: announce out, request in, reply out.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path


def test_hub_caps_through_mcp(hub):
    roster = hub.call("rook_workers")
    assert "rook" in {w["name"] for w in roster}
    reply = hub.call("rook_call", cap="hub.info", worker="rook")
    assert reply["ok"], reply
    assert reply["result"]["roles"] == ["is_hub"]
    assert reply["result"]["workers"] >= len(hub.workers)


def _dashboard(hub, path: str, body: dict | None = None):
    secrets = Path(hub.env["ROOK_IT_DATA_DIR"]) / "secrets.env"
    pw = next(line.split("=", 1)[1] for line in secrets.read_text().splitlines()
              if line.startswith("ROOK_WEB_PASS="))
    req = urllib.request.Request(
        hub.env["ROOK_IT_DASHBOARD_URL"] + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode(),
                 "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_hub_node_is_served_over_the_band(hub):
    if not hub.env.get("ROOK_IT_DASHBOARD_URL"):
        import pytest
        pytest.skip("test hub started without the dashboard")
    deadline = time.time() + 45   # the hub node re-announces every ~30 s
    rows: list = []
    while time.time() < deadline:
        rows = [w for w in _dashboard(hub, "/api/band/workers") if w.get("name") == "rook"]
        if rows:
            break
        time.sleep(2)
    assert rows, "the dashboard never saw worker 'rook' on the band"
    assert "hub.info" in rows[0]["caps"]
    reply = _dashboard(hub, "/api/band/call", {"cap": "hub.info", "worker_id": "rook"})
    assert reply["ok"], reply
    assert reply["from"] == rows[0]["worker_id"]
    assert reply["result"]["name"] == "rook"
