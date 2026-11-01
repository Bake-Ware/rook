"""token.create / token.list / token.revoke (the Tokens page): an agent
mints an MCP token for itself or another agent, acting for its person; new
agents survive a restart; revoking stops a token at once; the first owner's
token can't be revoked. A hub runs in-process (loopback band); every call
goes through its HTTP door under a hard timeout."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check door tokens."


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def config(tmp_path):
    return Config(data=tmp_path / "data", band=BAND,
                  users={"bake": {"preset": "owner"}, "kim": {"preset": "adult"}},
                  agents={"claude": {"grants": ["*"]}, "narrow": {"grants": ["hub.*"]}},
                  doors={"http": {"bind": "127.0.0.1:0"},
                         "mcp": {"bind": "127.0.0.1:0", "oauth": "https://castling.example"}})


async def call(hub, token, cap, args=None):
    port = next(d.port for d in hub.doors if d.name == "http")

    def go():
        body = json.dumps({"worker": "rook", "cap": cap, "args": args or {}, "why": WHY}).encode()
        r = urllib.request.Request(f"http://127.0.0.1:{port}/call", data=body, method="POST",
                                   headers={"Content-Type": "application/json",
                                            "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read() or b"{}")}
    return await asyncio.to_thread(go)


def test_an_agent_mints_tokens_and_they_work(tmp_path):
    async def go():
        cfg = config(tmp_path)
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            claude = hub.add_token(Caller(person="person:bake", agent="agent:claude"))
            out = await call(hub, claude, "token.create", {"agent": "claude-web", "label": "claude.ai"})
            assert out["ok"], out
            made = out["result"]
            assert made["agent"] == "claude-web" and made["person"] == "bake" and made["new_agent"]
            assert made["mcp_url"] == "https://castling.example/mcp"
            assert made["token"] in made["claude_code"]
            # the new token works at once, as claude-web acting for bake
            out = await call(hub, made["token"], "hub.status")
            assert out["ok"], out
            # only the hash is stored, with the new agent's rights
            stored = json.loads((cfg.data / "tokens.json").read_text())
            assert made["token"] not in json.dumps(stored)
            rec = next(r for d, r in stored.items() if d.startswith(made["id"]))
            assert rec["agent"] == "agent:claude-web" and rec["rights"] == {"grants": ["*"]}
            assert rec["by"] == "agent:claude"
            # an agent with narrower rights hands down only its own
            narrow = hub.add_token(Caller(person="person:bake", agent="agent:narrow"))
            hub.identities.agents["narrow"] = {"grants": ["hub.*", "token.*"]}
            out = await call(hub, narrow, "token.create", {"agent": "helper"})
            assert out["ok"], out
            assert hub.identities.agents["helper"] == {"grants": ["hub.*", "token.*"]}
            # minting for itself (a known agent) adds no identity
            out = await call(hub, claude, "token.create", {"agent": "claude"})
            assert out["ok"] and not out["result"]["new_agent"]
            listed = (await call(hub, claude, "token.list"))["result"]["tokens"]
            assert {t["agent"] for t in listed} >= {"claude", "claude-web"}
            assert all("token" not in t for t in listed)
        finally:
            await hub.stop()

        # a minted agent survives a restart
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            out = await call(hub, made["token"], "hub.status")
            assert out["ok"], out
            # revoke: it stops working at once
            owner = (cfg.data / "owner.token").read_text().strip()
            out = await call(hub, owner, "token.revoke", {"id": made["id"]})
            assert out["ok"], out
            out = await call(hub, made["token"], "hub.status")
            assert not out.get("ok")
        finally:
            await hub.stop()
    run(go())


def test_who_may_mint_list_and_revoke(tmp_path):
    async def go():
        cfg = config(tmp_path)
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            owner = (cfg.data / "owner.token").read_text().strip()
            alone = hub.add_token(Caller(agent="agent:claude"))
            kim = hub.add_token(Caller(person="person:kim"), label="kim")
            out = await call(hub, alone, "token.create", {"agent": "x"})
            assert not out["ok"] and "acts for a person" in out["error"]
            out = await call(hub, owner, "token.create", {"agent": "Bad Name"})
            assert not out["ok"]
            # kim sees only her own; can't revoke bake's
            listed = (await call(hub, owner, "token.list"))["result"]["tokens"]
            first = next(t for t in listed if t["label"] == "first owner")
            mine = await call(hub, kim, "token.list")
            if mine["ok"]:
                assert all(t["person"] == "kim" for t in mine["result"]["tokens"])
                out = await call(hub, kim, "token.revoke", {"id": first["id"]})
                assert not out["ok"]
            # the first owner's token can't be revoked (no lock-out)
            out = await call(hub, owner, "token.revoke", {"id": first["id"]})
            assert not out["ok"] and "first owner" in out["error"]
            assert (await call(hub, owner, "hub.status"))["ok"]
        finally:
            await hub.stop()
    run(go())
