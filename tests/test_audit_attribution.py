"""Audit attribution on the band MCP: attribute every call to the existing
bearer token; deny only a missing/invalid token; never let a broken checker
deny. Includes regressions for the 349e3eb knowledge-guard outage."""
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from rook.band_mcp import attribution
from rook.band_mcp.server import build_server

STATIC = "static-token-0123456789abcdef"


class FakeBand:
    """Stand-in band client. Workers deliberately sit on assorted band labels
    that match no enrollment record — attribution must not care."""

    def __init__(self):
        self.calls = []
        self.workers = {
            "w1": {"worker_id": "w1", "name": "kaiju", "band": "deadbeef",
                   "caps": ["info.host", "file.list", "shell.exec"], "last_seen": 0},
            "w2": {"worker_id": "w2", "name": "soundwave", "band": "?",
                   "caps": ["info.host", "shell.exec"], "last_seen": 0},
        }

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append({"cap": cap, "target": target, "identity": identity})
        return {"id": f"c{len(self.calls)}", "from": target, "ok": True,
                "result": {"cap": cap}}


@asynccontextmanager
async def mcp_http(tmp_path):
    band = FakeBand()
    mcp, store = build_server(band, public_url="https://mcp.example.com",
                              persist_path=str(tmp_path / "tokens.json"),
                              static_token=STATIC,
                              journal_path=str(tmp_path / "journal.db"))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        yield SimpleNamespace(http=http, band=band, store=store,
                              journal=str(tmp_path / "journal.db"))


class Session:
    def __init__(self, http, token):
        self.http, self.token, self.sid, self.n = http, token, None, 0

    def headers(self, token=None):
        h = {"Authorization": f"Bearer {token or self.token}"}
        if self.sid:
            h["mcp-session-id"] = self.sid
        return h

    async def open(self):
        r = await self.http.post("/mcp", headers=self.headers(), json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200, r.text
        self.sid = r.headers["mcp-session-id"]
        await self.http.post("/mcp", headers=self.headers(), json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    async def tool(self, name, args=None, token=None):
        self.n += 1
        r = await self.http.post("/mcp", headers=self.headers(token), json={
            "jsonrpc": "2.0", "id": self.n, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}}})
        return r

    async def result(self, name, args=None):
        r = await self.tool(name, args)
        assert r.status_code == 200, r.text
        body = r.json() if r.headers["content-type"].startswith("application/json") else \
            json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))
        return body["result"]


def rows(path, where="1=1"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(f"SELECT * FROM calls WHERE {where} ORDER BY seq")]


# --- row 1: missing / invalid token → that call is denied -------------------

@pytest.mark.asyncio
async def test_missing_or_invalid_token_is_rejected_by_existing_layer(tmp_path):
    async with mcp_http(tmp_path) as env:
        r = await env.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401
        r = await env.http.post("/mcp", headers={"Authorization": "Bearer nope-not-a-token"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401
        assert env.band.calls == []


@pytest.mark.asyncio
async def test_token_revoked_mid_session_denies_only_that_call(tmp_path):
    async with mcp_http(tmp_path) as env:
        minted = env.store.mint_api_token("codex")
        s = await Session(env.http, minted["token"]).open()
        ok = await s.result("rook_call", {"cap": "info.host", "worker": "kaiju"})
        assert not ok.get("isError")
        # Revoke; the HTTP layer rejects the next request outright...
        env.store.revoke_api_token(minted["id"])
        assert (await s.tool("rook_call", {"cap": "info.host", "worker": "kaiju"})).status_code == 401
        assert len(env.band.calls) == 1
        # ...and another caller on the same server is unaffected.
        other = await Session(env.http, STATIC).open()
        res = await other.result("rook_call", {"cap": "shell.exec", "worker": "soundwave"})
        assert not res.get("isError")
        assert len(env.band.calls) == 2


@pytest.mark.asyncio
async def test_tool_level_recheck_denies_invalid_token_and_journals_it(tmp_path, monkeypatch):
    """If a request somehow reaches a tool with an invalid token (e.g. revoked
    between the HTTP check and dispatch), the call is denied — not run as
    'anonymous' — and the denial is on the record."""
    async with mcp_http(tmp_path) as env:
        s = await Session(env.http, STATIC).open()
        monkeypatch.setattr(env.store, "principal_for", lambda raw: None)
        res = await s.result("rook_call", {"cap": "shell.exec", "worker": "kaiju"})
        assert res["isError"]
        assert "unauthenticated" in res["content"][0]["text"]
        assert env.band.calls == []
        denied = rows(env.journal, "cap='audit.denied'")
        assert len(denied) == 1 and denied[0]["auth"] == "denied"
        assert json.loads(denied[0]["reply"])["tool"] == "rook_call"


# --- row 2: checker broken → let through, alert, never deny -----------------

@pytest.mark.asyncio
async def test_token_store_failure_lets_call_through_and_alerts(tmp_path, monkeypatch, caplog):
    async with mcp_http(tmp_path) as env:
        s = await Session(env.http, STATIC).open()

        def boom(raw):
            raise RuntimeError("token store unavailable")
        monkeypatch.setattr(env.store, "principal_for", boom)
        with caplog.at_level(logging.ERROR, logger="rook.band_mcp.attribution"):
            for _ in range(3):
                res = await s.result("rook_call", {"cap": "shell.exec", "worker": "kaiju"})
                assert not res.get("isError")
        assert len(env.band.calls) == 3
        assert env.band.calls[0]["identity"] == "unverified"
        alerts = [r for r in caplog.records if "ROOK AUDIT ALERT" in r.getMessage()]
        assert len(alerts) == 1  # rate-limited, but loud
        assert len(rows(env.journal, "cap='audit.unverified'")) == 3
        assert {r["auth"] for r in rows(env.journal, "cap='shell.exec'")} == {"unverified"}


@pytest.mark.asyncio
async def test_attribution_crash_never_denies(tmp_path, monkeypatch):
    async with mcp_http(tmp_path) as env:
        s = await Session(env.http, STATIC).open()

        def explode(*a, **k):
            raise AssertionError("bug in resolve")
        monkeypatch.setattr(attribution, "resolve", explode)
        res = await s.result("rook_call", {"cap": "info.host", "worker": "kaiju"})
        assert not res.get("isError")
        assert len(env.band.calls) == 1


# --- attribution content -----------------------------------------------------

@pytest.mark.asyncio
async def test_named_key_attribution_reaches_journal_worker_and_whoami(tmp_path):
    async with mcp_http(tmp_path) as env:
        minted = env.store.mint_api_token("claude code")
        s = await Session(env.http, minted["token"]).open()
        s.token = minted["token"]
        env.http.headers["X-Rook-Host"] = "cachyrig"
        who = json.loads((await s.result("rook_whoami"))["content"][0]["text"])
        assert who == {"identity": "agent:claude code_cachyrig", "kind": "agent",
                       "label": "claude code", "agent_id": minted["agent_id"],
                       "key_id": minted["id"], "verified": True}
        assert minted["token"] not in json.dumps(who)
        await s.result("rook_call", {"cap": "shell.exec", "worker": "kaiju"})
        assert env.band.calls[-1]["identity"] == "agent:claude code_cachyrig"
        row = rows(env.journal, "cap='shell.exec'")[-1]
        assert (row["agent_id"], row["key_id"], row["auth"]) == (minted["agent_id"], minted["id"], "agent")


@pytest.mark.asyncio
async def test_static_token_is_allowed_and_attributed_as_shared(tmp_path):
    async with mcp_http(tmp_path) as env:
        s = await Session(env.http, STATIC).open()
        who = json.loads((await s.result("rook_whoami"))["content"][0]["text"])
        assert who["kind"] == "shared" and who["identity"] == "agent:static"
        assert "agent_id" not in who


@pytest.mark.asyncio
async def test_agent_id_survives_key_rotation(tmp_path):
    async with mcp_http(tmp_path) as env:
        first = env.store.mint_api_token("codex")
        rotated = env.store.rotate_api_token(first["id"])
        assert rotated["agent_id"] == first["agent_id"] and rotated["id"] != first["id"]
        assert env.store.principal_for(first["token"]) is None
        assert env.store.principal_for(rotated["token"])["agent_id"] == first["agent_id"]


# --- 349e3eb regressions: no second authorization layer ----------------------

@pytest.mark.asyncio
async def test_no_attempt_gate_reads_and_writes_dispatch_on_any_band(tmp_path):
    async with mcp_http(tmp_path) as env:
        s = await Session(env.http, STATIC).open()
        for cap, worker in [("info.host", "kaiju"), ("file.list", "kaiju"),
                            ("shell.exec", "kaiju"), ("shell.exec", "soundwave")]:
            res = await s.result("rook_call", {"cap": cap, "worker": worker})
            assert not res.get("isError"), res
            assert "attempt_id" not in res["content"][0]["text"]
        assert [c["cap"] for c in env.band.calls] == ["info.host", "file.list", "shell.exec", "shell.exec"]
        s.n += 1
        r = await env.http.post("/mcp", headers=s.headers(), json={
            "jsonrpc": "2.0", "id": s.n, "method": "tools/list"})
        listing = r.json() if r.headers["content-type"].startswith("application/json") else \
            json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))
        tools = {t["name"]: t for t in listing["result"]["tools"]}
        assert "rook_attempt" not in tools
        assert not {"attempt_id", "request_id"} & set(tools["rook_call"]["inputSchema"]["properties"])


# --- resolve() unit cases ----------------------------------------------------

class Store:
    def principal_for(self, raw):
        return {"kind": "agent", "agent_id": "agent_1", "key_id": "k1", "label": "a"} if raw == "good" else None


def req(auth=None):
    return SimpleNamespace(headers={"authorization": auth} if auth else {})


def test_resolve_falls_back_to_request_header_when_context_missing():
    att = attribution.resolve(Store(), lambda: None, lambda: req("Bearer good"))
    assert att.verified and att.agent_id == "agent_1"


def test_resolve_missing_header_is_unauthenticated():
    with pytest.raises(attribution.Unauthenticated):
        attribution.resolve(Store(), lambda: None, lambda: req())
    with pytest.raises(attribution.Unauthenticated):
        attribution.resolve(Store(), lambda: None, lambda: req("Basic Zm9vOmJhcg=="))
    with pytest.raises(attribution.Unauthenticated):
        attribution.resolve(Store(), lambda: "bad", lambda: req())


def test_resolve_infra_failures_are_unverified_not_denied():
    def boom():
        raise LookupError
    assert not attribution.resolve(Store(), boom, lambda: req()).verified
    assert not attribution.resolve(Store(), lambda: None, boom).verified
    assert not attribution.resolve(Store(), lambda: None, lambda: None).verified
