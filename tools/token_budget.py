"""Measure what the MCP bridge costs an agent, in characters.

Builds a hub in-process (no network, no live band): a synthetic band of
worker-a … worker-z (26 workers) announcing the real worker plugin caps, a
throwaway store directory and a seeded knowledge wiki. Then it drives the MCP
over the ASGI transport exactly as a client would and prints the size of:

* ``initialize`` instructions and ``tools/list`` (paid on every connect),
* a ``rook_call`` shell.exec with no output (first and repeat call),
* ``rook_workers`` / ``rook_caps`` / ``caps.describe`` with default arguments,
* ``rook_knowledge`` search with default arguments.

"wire" is the JSON-RPC ``result`` object as sent (text content plus any
structured copy); "text" is the text payload an agent reads. Run from a
checkout::

    python tools/token_budget.py          # table
    python tools/token_budget.py --json   # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import tempfile
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

STATIC = "static-token-0123456789abcdef"


def _real_caps():
    from rook.worker.plugin import load_plugins
    from rook.worker.registry import CapabilityRegistry
    reg = CapabilityRegistry()
    plugins = load_plugins("rook.worker.plugins", reg, None)
    reg.register("caps.describe", lambda: None)
    return reg, [p.NAMESPACE for p in plugins]


class SyntheticBand:
    """26 workers with the real cap set (a few trimmed per worker)."""

    def __init__(self, n: int = 26):
        rng = random.Random(7)
        self.registry, plugins = _real_caps()
        caps = self.registry.list()
        self.described = {k: v for k, v in self.registry.describe().items()}
        self.workers = {}
        for i in range(n):
            wid = "%032x" % rng.getrandbits(128)
            mine = [c for c in caps if rng.random() > 0.1 or c in ("shell.exec", "caps.describe")]
            self.workers[wid] = {
                "worker_id": wid, "name": f"worker-{chr(97 + i)}", "band": "b" * 32,
                "description": "Example role description for this machine", "caps": mine,
                "plugins": plugins, "version": "0.1.0", "build": "167.brisk.otter",
                "app_release": {}, "hb": ({"battery": {"percent": 70, "charging": True}} if i % 5 == 0 else {}),
                "last_seen": __import__("time").time() - rng.random() * 30,
            }

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        rid = "%032x" % random.getrandbits(128)
        if cap == "caps.describe":
            prefix = (args or {}).get("prefix") or ""
            res = {k: v for k, v in self.described.items() if k.startswith(prefix)}
            return {"id": rid, "from": target, "ok": True, "result": res}
        if cap == "shell.exec":
            return {"id": rid, "from": target, "ok": True,
                    "result": {"ok": True, "code": 0, "stdout": "", "stderr": ""}}
        return {"id": rid, "from": target, "ok": True, "result": {}}


def _body(r):
    if r.headers["content-type"].startswith("application/json"):
        return r.json()
    return json.loads(next(line[5:] for line in r.text.splitlines() if line.startswith("data:")))


@asynccontextmanager
async def hub(tmp):
    os.environ["ROOK_KNOWLEDGE"] = "1"
    os.environ["ROOK_KNOWLEDGE_DB"] = os.path.join(tmp, "knowledge.db")
    from rook.band_mcp.server import build_server
    mcp, _store = build_server(SyntheticBand(), public_url="https://mcp.example.com",
                               persist_path=os.path.join(tmp, "tokens.json"), static_token=STATIC,
                               journal_path=os.path.join(tmp, "journal.db"))
    k = mcp._rook_knowledge
    words = "deploy model driver cuda postgres migration backup network storage".split()
    for i in range(30):
        body = " ".join(random.Random(i).choice(words) for _ in range(400))
        await k.dispatch("create", kind="knowledge", data={"title": f"Procedure {i} for deploy", "body": body},
                         request_id=f"seed-{i}", actor={"id": "seed", "kind": "human", "label": "seed"})
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream",
                     "Authorization": "Bearer " + STATIC}) as http:
        r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "budget", "version": "1"}}})
        http.headers["mcp-session-id"] = r.headers["mcp-session-id"]
        await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        async def rpc(method, params):
            r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            return _body(r)["result"]
        yield _body(r)["result"], rpc


def _sizes(res):
    wire = len(json.dumps(res, separators=(",", ":"), ensure_ascii=False))
    text = sum(len(c.get("text", "")) for c in res.get("content", []))
    return {"wire": wire, "text": text}


async def measure() -> dict:
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as tmp:
        async with hub(tmp) as (init, rpc):
            out: dict = {}
            out["initialize.instructions"] = {"text": len(init.get("instructions") or "")}
            tools = (await rpc("tools/list", {}))["tools"]
            out["tools/list"] = {"wire": len(json.dumps({"tools": tools}, separators=(",", ":"), ensure_ascii=False)),
                                 "tools": len(tools)}
            per = sorted(((len(json.dumps(t, separators=(",", ":"))), t["name"]) for t in tools), reverse=True)
            out["tools/list.largest"] = [{"tool": n, "wire": s} for s, n in per[:6]]

            async def call(name, arguments):
                return await rpc("tools/call", {"name": name, "arguments": arguments})
            first = await call("rook_call", {"cap": "shell.exec", "worker": "worker-a", "args": {"cmd": "true"}})
            out["rook_call shell.exec empty (1st)"] = _sizes(first)
            again = await call("rook_call", {"cap": "shell.exec", "worker": "worker-a", "args": {"cmd": "true"}})
            out["rook_call shell.exec empty (repeat)"] = _sizes(again)
            out["rook_call shell.exec empty (repeat).text"] = again["content"][0]["text"]
            out["rook_call shell.exec text=true"] = _sizes(await call(
                "rook_call", {"cap": "shell.exec", "worker": "worker-a", "args": {"cmd": "true"}, "text": True}))
            out["rook_workers()"] = _sizes(await call("rook_workers", {}))
            out["rook_workers(name=worker-a)"] = _sizes(await call("rook_workers", {"name": "worker-a"}))
            out["rook_caps()"] = _sizes(await call("rook_caps", {}))
            out["rook_caps(prefix=shell.)"] = _sizes(await call("rook_caps", {"prefix": "shell."}))
            out["rook_call caps.describe"] = _sizes(await call("rook_call", {"cap": "caps.describe", "worker": "worker-a"}))
            out["rook_call caps.describe prefix=shell."] = _sizes(await call(
                "rook_call", {"cap": "caps.describe", "worker": "worker-a", "args": {"prefix": "shell."}}))
            out["rook_knowledge search"] = _sizes(await call("rook_knowledge", {"action": "search", "query": "deploy"}))
            return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = asyncio.run(measure())
    if a.json:
        print(json.dumps(out, indent=2))
        return
    for k, v in out.items():
        if k == "tools/list.largest":
            print(f"  largest tools: " + ", ".join(f"{x['tool']} {x['wire']}" for x in v))
        elif isinstance(v, dict):
            print(f"{k:40s} " + "  ".join(f"{kk}={vv:,}" for kk, vv in v.items()))
        else:
            print(f"{k:40s} {v}")


if __name__ == "__main__":
    main()
