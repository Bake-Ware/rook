"""Knowledge and tasks as hub plugins on an isolated test hub (opt-in).

The MCP tools keep their shape; the same records are reachable as caps on
worker "rook", from the MCP (rook_call) and across the real relay from the
dashboard's band client, where only the read caps are allowed.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


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


@pytest.fixture
def kb_hub(hub):
    if not hub.knowledge:
        pytest.skip("test hub started with --no-knowledge")
    return hub


def test_task_caps_through_rook_call(kb_hub):
    word = "it" + uuid.uuid4().hex[:10]

    async def go(s):
        made = await kb_hub.acall(s, "rook_call", cap="task.write", worker="rook", args={
            "action": "create", "kind": "concept", "data": {"title": f"Concept {word}"},
            "request_id": uuid.uuid4().hex})
        got = await kb_hub.acall(s, "rook_concept", action="get", id=made["result"]["slug"])
        found = await kb_hub.acall(s, "rook_call", cap="task.read", worker="rook",
                                   args={"action": "search", "kind": "concept", "query": word})
        return made, got, found
    made, got, found = kb_hub.run(go)
    assert made["ok"], made
    assert got["ok"] and got["result"]["id"] == made["result"]["id"]
    assert made["result"]["id"] in [r["id"] for r in found["result"]["results"]]


def test_knowledge_reads_cross_the_band_and_writes_do_not(kb_hub):
    if not kb_hub.env.get("ROOK_IT_DASHBOARD_URL"):
        pytest.skip("test hub started without the dashboard")
    deadline = time.time() + 45   # the hub node re-announces every ~30 s
    while time.time() < deadline:
        rook = [w for w in _dashboard(kb_hub, "/api/band/workers") if w.get("name") == "rook"]
        if rook and "knowledge.read" in rook[0]["caps"]:
            break
        time.sleep(2)
    else:
        pytest.fail("the dashboard never saw knowledge.read on worker 'rook'")
    read = _dashboard(kb_hub, "/api/band/call", {"cap": "knowledge.read", "worker_id": "rook",
                                                 "args": {"action": "status"}})
    assert read["ok"], read
    assert "counts" in read["result"]
    write = _dashboard(kb_hub, "/api/band/call", {"cap": "knowledge.write", "worker_id": "rook", "args": {
        "action": "create", "data": {"title": "from the band"}, "request_id": uuid.uuid4().hex}})
    assert not write["ok"] and "not callable over the band" in write["error"]


def test_operator_knowledge_api_is_mounted(kb_hub):
    """The dashboard's Knowledge page proxies to this route on the MCP server."""
    base = kb_hub.url.rsplit("/mcp", 1)[0]
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(base + "/knowledge/account-api", timeout=15)
    assert err.value.code == 401
    assert "Sign in" in json.loads(err.value.read())["error"]
