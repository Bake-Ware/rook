"""The voice agent must not exhaust the hub's MCP session table (2026-09-23
outage): one shared session per process, re-established when the hub drops it,
and capability schemas described only when new, changed or stale."""
import asyncio
import json
import time

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from services.voice import rookmcp, workers
from services.voice.rookmcp import RookMCP
from services.voice.workers import WorkerInventory


def hub():
    mcp = FastMCP('hub')
    @mcp.tool()
    async def rook_workers() -> str:
        return json.dumps([{'name': 'kaiju'}])
    return mcp, mcp.streamable_http_app()


def test_calls_share_one_session_and_recover_when_the_hub_drops_it(monkeypatch):
    mcp, app = hub()
    inits = []
    Real = httpx.AsyncClient
    class Client(Real):
        def __init__(self, **kw):
            super().__init__(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000', **kw)
        async def post(self, url, **kw):
            if kw['json'].get('method') == 'initialize':
                inits.append(1)
            return await super().post(url, **kw)
    monkeypatch.setattr(rookmcp.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(RookMCP, '_sid', None)
    async def scenario():
        async with app.router.lifespan_context(app):
            for _ in range(5):
                assert json.loads(await RookMCP(url='http://localhost:8000/mcp').call('rook_workers', {}))[0]['name'] == 'kaiju'
            assert len(inits) == 1
            assert len(mcp.session_manager._server_instances) == 1
            # The hub forgets the session (restart / idle expiry / eviction): one re-initialize, call still succeeds.
            async with Real(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000') as raw:
                await raw.delete('/mcp', headers={'mcp-session-id': RookMCP._sid, 'Accept': 'application/json, text/event-stream'})
            await asyncio.sleep(.01)
            assert json.loads(await RookMCP(url='http://localhost:8000/mcp').call('rook_workers', {}))[0]['name'] == 'kaiju'
            assert len(inits) == 2
            # Concurrent calls still open only one session.
            RookMCP._sid = None
            await asyncio.gather(*(RookMCP(url='http://localhost:8000/mcp').call('rook_workers', {}) for _ in range(8)))
            assert len(inits) == 3
    asyncio.run(scenario())


def test_schemas_are_described_only_when_new_changed_or_stale(monkeypatch):
    pytest.importorskip('numpy')   # refresh_schemas imports providers (audio deps); runs on the voice host
    roster = [{'name': 'kaiju', 'caps': ['info.host']}, {'name': 'bakephone', 'caps': ['battery.status']}]
    described, broken = [], set()
    class MCP:
        async def call(self, tool, args):
            if tool == 'rook_workers':
                return json.dumps(roster)
            described.append(args['worker'])
            if args['worker'] in broken:
                return json.dumps({'ok': False, 'error': 'offline'})
            return json.dumps({'ok': True, 'result': {'info.host': {'params': []}, 'battery.status': {'params': []}}})
    monkeypatch.setattr(workers, 'RookMCP', MCP)
    inv = WorkerInventory(ttl=0, schema_ttl=3600, retry_seconds=600)
    async def scenario():
        await inv.refresh_schemas()
        assert sorted(described) == ['bakephone', 'kaiju'] and 'info.host' in inv.schemas['kaiju']
        described.clear()
        for _ in range(10):                      # the 60s maintenance loop: nothing new, no traffic
            await inv.refresh_schemas()
        assert described == []
        roster[1]['caps'] = ['battery.status', 'sms.list']   # a worker's caps change: only it is re-described
        roster.append({'name': 'piratepi', 'caps': ['info.host']})
        broken.add('piratepi')
        await inv.refresh_schemas()
        assert sorted(described) == ['bakephone', 'piratepi']
        described.clear()
        await inv.refresh_schemas()
        assert described == []                   # a failed describe waits retry_seconds
        inv.schema_due['piratepi'] = time.monotonic() - 1
        broken.clear()
        await inv.refresh_schemas()
        assert described == ['piratepi'] and 'info.host' in inv.schemas['piratepi']
        described.clear()
        for name in inv.schema_due: inv.schema_due[name] = time.monotonic() - 1   # an hour later: all refreshed once
        await inv.refresh_schemas()
        assert sorted(described) == ['bakephone', 'kaiju', 'piratepi']
    asyncio.run(scenario())
