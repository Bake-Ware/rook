"""The voice agent must not exhaust the hub's MCP session table (2026-09-23
outage): one shared session per process, re-established when the hub drops it."""
import asyncio
import json

import httpx
from mcp.server.fastmcp import FastMCP

from services.voice import rookmcp
from services.voice.rookmcp import RookMCP


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

